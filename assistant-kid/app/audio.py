"""音频处理: 格式转换(to_wav) + MP3压缩(_wav_to_mp3)

依赖 ffmpeg(系统命令). 失败时静默降级返回原始数据, 不抛异常.
管道模式: stdin/stdout 直传, 省临时文件 I/O (~300ms+).
"""
import io, os, subprocess, tempfile, wave
import warnings

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"'audioop' is deprecated",
        category=DeprecationWarning,
    )
    import audioop

MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 10MB 音频上限
EMPTY_AUDIO_MIN_SECONDS = 3.0
EMPTY_AUDIO_FRAME_MS = 20
EMPTY_AUDIO_RMS_THRESHOLD = 120
EMPTY_AUDIO_MIN_ACTIVE_SECONDS = 0.25
EMPTY_AUDIO_MIN_ACTIVE_RATIO = 0.08


def likely_empty_audio(data: bytes) -> bool:
    """保守判断长静音/噪声，供远端 ASR 前快速短路。

    只明确识别 16-bit PCM WAV 中的长段低能量音频；任何解析失败、格式不支持或
    短录音都返回 False，交给 ASR 继续判断，避免误杀真实小声说话。
    """
    if not data:
        return False
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            sample_width = wav.getsampwidth()
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            if sample_width != 2 or channels < 1 or sample_rate <= 0 or frame_count <= 0:
                return False
            duration = frame_count / float(sample_rate)
            if duration < EMPTY_AUDIO_MIN_SECONDS:
                return False

            chunk_frames = max(1, int(sample_rate * EMPTY_AUDIO_FRAME_MS / 1000))
            bytes_per_frame = channels * sample_width
            active_frames = 0
            while True:
                chunk = wav.readframes(chunk_frames)
                if not chunk:
                    break
                rms = audioop.rms(chunk, sample_width)
                if rms >= EMPTY_AUDIO_RMS_THRESHOLD:
                    active_frames += len(chunk) // bytes_per_frame

            active_seconds = active_frames / float(sample_rate)
            active_ratio = active_frames / float(frame_count)
            return (
                active_seconds < EMPTY_AUDIO_MIN_ACTIVE_SECONDS
                and active_ratio < EMPTY_AUDIO_MIN_ACTIVE_RATIO
            )
    except Exception:
        return False

def to_wav(data: bytes, ext: str) -> bytes:
    if ext in ("wav", "wave"):
        return data
    # 管道模式: data→stdin→ffmpeg→stdout, 无临时文件
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", ext, "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=data, capture_output=True, timeout=15)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        # 管道失败, 回退临时文件模式
        return _to_wav_tempfile(data, ext)
    except Exception:
        return data

def _to_wav_tempfile(data: bytes, ext: str) -> bytes:
    """回退: 临时文件模式(管道失败时用)"""
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(data)
        inp = f.name
    out = inp + ".wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-ar", "16000", "-ac", "1", "-f", "wav", out],
                       capture_output=True, timeout=15)
        with open(out, "rb") as f: return f.read()
    except Exception:
        return data
    finally:
        for p in (inp, out):
            try: os.unlink(p)
            except: pass

def _wav_to_mp3(wav_data: bytes, bitrate: str = "32k") -> bytes:
    """WAV音频转MP3(语音级32kbps,约6x压缩), 管道模式"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0", "-b:a", bitrate, "-ac", "1", "-f", "mp3", "pipe:1"],
            input=wav_data, capture_output=True, timeout=10)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        return _wav_to_mp3_tempfile(wav_data, bitrate)
    except Exception:
        return wav_data  # 失败返回原始WAV

def _wav_to_mp3_tempfile(wav_data: bytes, bitrate: str = "32k") -> bytes:
    """回退: 临时文件模式"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data); inp = f.name
    out = inp.replace(".wav", ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-b:a", bitrate, "-ac", "1", out],
                       capture_output=True, timeout=10)
        with open(out, "rb") as f: return f.read()
    except Exception:
        return wav_data
    finally:
        for p in (inp, out):
            try: os.unlink(p)
            except: pass
