"""
Charlie · 交互式语音CLI
绕过浏览器麦克风权限，命令行实时语音对话
按回车开始录音，说完再按回车停止 → ASR→大脑(6MCP)→TTS → 播放
"""
import os, sys, subprocess, re, tempfile
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from voice_agent import DATA_DIR, voice_loop, write_audio_file


def cli_audio_path(filename):
    return os.path.join(DATA_DIR, filename)


def find_airpods():
    """自动检测AirPods麦克风设备号(avfoundation)"""
    try:
        out = subprocess.run(["ffmpeg","-f","avfoundation","-list_devices","true","-i",""],
                             capture_output=True, text=True, timeout=5).stderr
        for m in re.finditer(r"\[(\d+)\] (.+)", out):
            if "airpod" in m.group(2).lower() or "蓝牙" in m.group(2):
                return m.group(1), m.group(2)
        # fallback: 第一个非BlackHole/Iriun的音频设备
        for m in re.finditer(r"\[(\d+)\] (.+)", out):
            n = m.group(2)
            if "blackhole" not in n.lower() and "iriun" not in n.lower() and "screen" not in n.lower():
                return m.group(1), n
    except Exception as e:
        print("检测麦克风设备失败:", e)
    return "2", "默认设备"

def record(device, output=None):
    """开始录音(后台)，返回Popen; 调用方按回车后terminate"""
    target = output or cli_audio_path("cli_mic.wav")
    directory = os.path.dirname(os.path.abspath(target)) or "."
    fd, temp_output = tempfile.mkstemp(prefix=".cli_mic.", suffix=".recording.wav", dir=directory)
    os.close(fd)
    cmd = ["ffmpeg","-f","avfoundation","-i",f":{device}","-ar","16000","-ac","1","-y",temp_output]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            os.unlink(temp_output)
        except OSError:
            pass
        raise
    proc._cli_target_path = target
    proc._cli_temp_path = temp_output
    return proc


def stop_recording(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def discard_recording(proc):
    """Stop ffmpeg and discard an in-progress temporary recording."""
    temp_path = getattr(proc, "_cli_temp_path", None)
    stop_recording(proc)
    if temp_path:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def commit_recording(proc, min_size=2500):
    """Stop ffmpeg and publish its temporary WAV only after a usable file exists."""
    target = getattr(proc, "_cli_target_path", None)
    temp_path = getattr(proc, "_cli_temp_path", None)
    stop_recording(proc)
    if not target or not temp_path:
        return None
    try:
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) < min_size:
            return None
        os.replace(temp_path, target)
        return target
    finally:
        try:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass

def play(path):
    subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    dev, name = find_airpods()
    print("="*46)
    print("  Charlie · 交互式语音对话")
    print("="*46)
    print(f"🎤 麦克风: [{dev}] {name}")
    print(f"🧠 大脑: deepseek-v4-flash + MCP")
    print("操作: 按回车开始说话 → 说完按回车停止 → 白泽回答")
    print("退出: Ctrl+C\n")
    while True:
        try:
            input("…按回车开始录音…")
        except (EOFError, KeyboardInterrupt):
            print("\n再见👋"); break
        proc = record(dev)
        print("🔴 录音中…说完按回车停止")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            discard_recording(proc)
            print("\n再见👋")
            break
        try:
            mic_path = commit_recording(proc)
        except OSError as e:
            print(f"保存录音失败: {e}\n")
            continue
        if not mic_path:
            print("(没录到声音，重试)\n"); continue
        print("🧠 处理中(ASR→大脑调MCP→TTS)…")
        try:
            with open(mic_path,"rb") as f: audio_data = f.read()
            text, reply, audio = voice_loop(audio_data, "wav")
        except Exception as e:
            print(f"处理失败: {e}\n"); continue
        print(f"\n🗣️ 你说: {text}")
        print(f"🤖 白泽: {reply}\n")
        if audio and len(audio) > 100:
            reply_path = cli_audio_path("cli_reply.wav")
            try:
                write_audio_file(reply_path, audio)
                print("🔊 播放回复…")
                play(reply_path)
            except OSError as e:
                print(f"保存回复音频失败: {e}\n")
        else:
            print("(语音合成失败，仅文字回复)")

if __name__ == "__main__":
    main()
