"""
魔幻手机 · 交互式语音CLI
绕过浏览器麦克风权限，命令行实时语音对话
按回车开始录音，说完再按回车停止 → ASR→大脑(6MCP)→TTS → 播放
"""
import os, sys, subprocess, re
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from voice_agent import voice_loop

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

def record(device, output="/tmp/cli_mic.wav"):
    """开始录音(后台)，返回Popen; 调用方按回车后terminate"""
    proc = subprocess.Popen(
        ["ffmpeg","-f","avfoundation","-i",f":{device}","-ar","16000","-ac","1","-y",output],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc

def play(path):
    subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    dev, name = find_airpods()
    print("="*46)
    print("  魔幻手机 · 交互式语音对话")
    print("="*46)
    print(f"🎤 麦克风: [{dev}] {name}")
    print(f"🧠 大脑: GLM-5.2 + 6个MCP")
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
            proc.terminate(); print("\n再见👋"); break
        proc.terminate()
        try: proc.wait(timeout=2)
        except: proc.kill()
        if not os.path.exists("/tmp/cli_mic.wav") or os.path.getsize("/tmp/cli_mic.wav") < 2500:
            print("(没录到声音，重试)\n"); continue
        print("🧠 处理中(ASR→大脑调MCP→TTS)…")
        try:
            text, reply, audio = voice_loop(open("/tmp/cli_mic.wav","rb").read(), "wav")
        except Exception as e:
            print(f"处理失败: {e}\n"); continue
        print(f"\n🗣️ 你说: {text}")
        print(f"🤖 白泽: {reply}\n")
        open("/tmp/cli_reply.wav","wb").write(audio)
        print("🔊 播放回复…")
        play("/tmp/cli_reply.wav")

if __name__ == "__main__":
    main()
