"""音频处理: 格式转换(to_wav) + MP3压缩(_wav_to_mp3)

依赖 ffmpeg(系统命令). 失败时静默降级返回原始数据, 不抛异常.
"""
import os, subprocess, tempfile

MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 10MB 音频上限

def to_wav(data: bytes, ext: str) -> bytes:
    if ext in ("wav", "wave"):
        return data
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
    """WAV音频转MP3(语音级32kbps,约6x压缩), 减少网络传输"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data); inp = f.name
    out = inp.replace(".wav", ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-b:a", bitrate, "-ac", "1", out],
                       capture_output=True, timeout=10)
        with open(out, "rb") as f: return f.read()
    except Exception:
        return wav_data  # 失败返回原始WAV
    finally:
        for p in (inp, out):
            try: os.unlink(p)
            except: pass
