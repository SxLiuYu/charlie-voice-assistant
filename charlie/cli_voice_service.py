"""
Charlie · 持续语音监听服务
USB 麦克风实时录音 → VAD 自动断句 → ASR→大脑→TTS → 扬声器播放
功能与 ESP32 硬件终端一致，纯软件实现。
"""
import os, sys, subprocess, re, tempfile, time, struct, wave, signal, threading
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from voice_agent import DATA_DIR, voice_loop, write_audio_file


def _play_audio(path: str):
    """跨平台音频播放（macOS afplay / Windows winsound / Linux aplay）"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
        else:
            subprocess.run(["aplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ── 配置 ──────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_DURATION_MS = 30  # 每帧 30ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_DURATION_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

# VAD 参数（自适应阈值）
SIG_RATIO = 2.0        # 语音阈值 = 噪声基底 * SIG_RATIO
SILENCE_RMS_DEFAULT = 500  # 初始阈值（噪声基底未建立时用）
SILENCE_FRAMES = 40     # 连续 40 帧静音 = 1.2s 尾静音 → 端点
MIN_SPEECH_FRAMES = 4   # 最少 4 帧活跃才认为是有效语音
MAX_SPEECH_FRAMES = 600 # 最长 18s 强制端点
HEAD_START_FRAMES = 6   # 语音开始前保留的帧数

# ── 音频工具 ──────────────────────────────────────────

def detect_mic():
    """自动检测 USB 麦克风设备号"""
    try:
        out = subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True, timeout=5).stderr
        for m in re.finditer(r"\[(\d+)\] (.+)", out):
            name = m.group(2).lower()
            if "blackhole" not in name and "iriun" not in name and "screen" not in name:
                return int(m.group(1)), m.group(2)
    except Exception as e:
        print(f"检测麦克风失败: {e}")
    return 2, "默认设备"


def rms(data: bytes) -> float:
    """计算 16-bit PCM 的 RMS 值"""
    if len(data) < 2:
        return 0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[:n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


def frames_to_wav(frames: list) -> bytes:
    """将 PCM 帧列表编码为 WAV"""
    bio = wave.open(io := tempfile.SpooledTemporaryFile(), "wb")
    bio.setnchannels(CHANNELS)
    bio.setsampwidth(SAMPLE_WIDTH)
    bio.setframerate(SAMPLE_RATE)
    for f in frames:
        bio.writeframes(f)
    bio.close()
    io.seek(0)
    return io.read()


def msec() -> float:
    return time.monotonic() * 1000


# ── VAD 录音循环 ──────────────────────────────────────

def vad_loop(device: int, stop_event: threading.Event):
    """连续录音 + VAD 自动断句，返回 (wav_bytes, text, reply, audio)"""
    # 启动 ffmpeg 实时音频流
    cmd = ["ffmpeg", "-f", "avfoundation", "-i", f":{device}",
           "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
           "-f", "s16le", "-", "-y"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=FRAME_BYTES * 64)
    except Exception as e:
        print(f"启动录音失败: {e}")
        return

    tail = []           # 语音前缓存
    buf = []            # 当前语音缓冲区
    speech_count = 0
    silence_count = 0
    utterance_active = False
    frame_count = 0
    noise_floor = 0.0   # 自适应噪声基底

    def reset():
        nonlocal tail, buf, speech_count, silence_count, utterance_active
        tail.clear()
        buf.clear()
        speech_count = 0
        silence_count = 0
        utterance_active = False

    print(f"🎤 麦克风: [{device}] {mic_name}")
    print("🔊 开始持续监听，说话即可识别...")
    print("退出: Ctrl+C")

    while not stop_event.is_set():
        raw = proc.stdout.read(FRAME_BYTES)
        if not raw or len(raw) < FRAME_BYTES:
            break

        frame_count += 1
        rms_val = rms(raw)

        # 简单固定阈值：取前 30 帧最低值+余量，之后固定
        if frame_count < 30:
            if rms_val < noise_floor or noise_floor <= 0:
                noise_floor = rms_val
        thr = noise_floor + 500
        

        if not utterance_active:
            # 保持滚动 tail
            tail.append(raw)
            if len(tail) > HEAD_START_FRAMES:
                tail.pop(0)

            if rms_val >= thr:
                speech_count += 1
                if speech_count >= MIN_SPEECH_FRAMES:
                    # 语音开始
                    utterance_active = True
                    buf = list(tail)
                    buf.append(raw)
                    silence_count = 0
                    tail.clear()
            else:
                speech_count = 0
        else:
            buf.append(raw)
            if rms_val >= thr:
                speech_count += 1
                silence_count = 0
            else:
                silence_count += 1

            # 端点检测
            capped = len(buf) >= MAX_SPEECH_FRAMES
            if (speech_count >= MIN_SPEECH_FRAMES and silence_count >= SILENCE_FRAMES) or capped:
                print(f"\n🔊 检测到语音结束 ({len(buf)}帧/{len(buf)*FRAME_DURATION_MS/1000:.1f}s)", flush=True)
                wav = frames_to_wav(buf)
                reset()
                yield wav
                print("🔊 继续监听...", flush=True)

    proc.terminate()
    proc.wait()


# ── 主循环 ────────────────────────────────────────────

def main():
    global mic_name
    dev, mic_name = detect_mic()
    stop_event = threading.Event()

    def handle_sigint(sig, frame):
        stop_event.set()
        print("\n👋 再见")

    signal.signal(signal.SIGINT, handle_sigint)

    print("=" * 46)
    print("  Charlie · 持续语音对话")
    print("=" * 46)
    print(f"🎤 麦克风: [{dev}] {mic_name}")
    print(f"🧠 大脑: Unisound u2 + MCP")
    print(f"🔊 输出: {'afplay' if sys.platform == 'darwin' else 'winsound' if os.name == 'nt' else 'aplay'} (扬声器)")
    print()

    for wav in vad_loop(dev, stop_event):
        try:
            print("🧠 处理中(ASR→大脑→TTS)…", flush=True)
            t0 = time.time()
            text, reply, audio = voice_loop(wav, "wav")
            elapsed = time.time() - t0
            print(f"\n🗣️ 你说: {text}")
            print(f"🤖 回复: {reply}")
            print(f"⏱️ 耗时: {elapsed:.1f}s", flush=True)

            if audio and len(audio) > 100:
                reply_path = os.path.join(DATA_DIR, "cli_reply.wav")
                write_audio_file(reply_path, audio)
                _play_audio(reply_path)
            else:
                print("(语音合成失败，仅显示文字)")
        except Exception as e:
            print(f"❌ 处理失败: {e}", flush=True)


if __name__ == "__main__":
    main()