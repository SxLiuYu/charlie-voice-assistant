#!/usr/bin/env python3
"""
Charlie Voice Bridge — Mac 端唤醒监听 + 音频桥接

功能：
  - 方式 A（语音）：用麦克风检测用户说话 → 发给 Charlie → TTS 通过 AirPlay 播到天猫精灵
  - 方式 B（打字）：直接输入文字 → Charlie 回复通过 AirPlay 播到天猫精灵

依赖：
  pip install sounddevice websockets

使用前提：
  - Charlie 服务需在 localhost:8000 运行
  - 有可用麦克风（Mac mini 无自带麦克风，需外接 USB 麦克风或连接 AirPods）
  - Mac 系统设置 → 声音 → 输入 中选择了麦克风设备
  - 天猫精灵在局域网内支持 AirPlay（大部分支持）

Usage:
  python3 voice_bridge.py                     # 打字模式
  python3 voice_bridge.py --voice             # 语音模式（需麦克风）
  python3 voice_bridge.py --list-devices     # 列出所有音频设备
  python3 voice_bridge.py --output "天猫精灵"  # 指定 AirPlay 输出设备名
"""
import argparse
import asyncio
import base64
import io
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import wave
import os

# WebSocket
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("voice_bridge")

# ---------------------------------------------------------------------------
# 音频设备
# ---------------------------------------------------------------------------

def list_devices():
    import sounddevice as sd
    print("\n=== 输入设备（麦克风）===")
    for dev in sd.query_devices():
        if dev["max_input_channels"] > 0:
            idx = dev["index"]
            name = dev["name"]
            ch = dev["max_input_channels"]
            sr = dev["default_samplerate"]
            print(f"  [{idx}] {name} ({ch}ch, {sr}Hz)")
    print("\n=== 输出设备（扬声器/AirPlay）===")
    for dev in sd.query_devices():
        if dev["max_output_channels"] > 0:
            idx = dev["index"]
            name = dev["name"]
            ch = dev["max_output_channels"]
            sr = dev["default_samplerate"]
            print(f"  [{idx}] {name} ({ch}ch, {sr}Hz)")
    print()

def get_default_input():
    import sounddevice as sd
    try:
        dev = sd.query_devices(kind="input")
        if dev and dev["max_input_channels"] > 0:
            return dev["index"]
    except Exception:
        pass
    return None

def get_default_output():
    import sounddevice as sd
    try:
        dev = sd.query_devices(kind="output")
        if dev and dev["max_output_channels"] > 0:
            return dev["index"]
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# 音频录制（VAD 端点检测）
# ---------------------------------------------------------------------------

def record_with_vad(input_device, sample_rate=16000, silence_thresh=500, silence_frames=25):
    """
    监听麦克风，检测到语音时开始录音，沉默后停止。
    返回 PCM bytes (16kHz mono int16)。
    """
    import sounddevice as sd
    frames = []
    triggered = False
    silence_count = 0

    q = queue.Queue()

    def callback(indata, frames_count, time_info, status):
        if status:
            log.debug(f"录音状态: {status}")
        # 转换并降采样到 16kHz（如果原始是 48kHz）
        audio = indata[:, 0]  # 取第一通道
        if sample_rate != 48000:
            import numpy as np
            step = 48000 // sample_rate
            audio = audio[::step]
        int16_data = (audio * 32767).astype("int16")
        q.put(int16_data.tobytes())

    stream = sd.InputStream(
        device=input_device,
        samplerate=48000,  # macOS 原生采样率，代码内降
        channels=1,
        dtype="float32",
        blocksize=1024,
        callback=callback,
    )

    with stream:
        log.info(f"开始监听（设备 {input_device}，48kHz → 16kHz）...")
        while True:
            chunk = q.get()
            # 能量检测
            energy = sum(abs(c) for c in chunk) // len(chunk)
            if energy > silence_thresh:
                triggered = True
                silence_count = 0
            if triggered:
                frames.append(chunk)
                if energy < silence_thresh:
                    silence_count += 1
                    if silence_count >= silence_frames:
                        log.info(f"检测到说完，停止录音（能量={energy}）")
                        break
                else:
                    silence_count = 0
            # 防爆：最多录 30 秒
            if len(frames) > 3000:
                log.info("录音超时 30s，强制停止")
                break

    audio_pcm = b"".join(frames)
    log.info(f"录音: {len(audio_pcm)//32:.1f}s ({len(audio_pcm)} bytes)")
    return audio_pcm

# ---------------------------------------------------------------------------
# PCM → WAV 转换
# ---------------------------------------------------------------------------

def pcm_to_wav(audio_pcm: bytes, sample_rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_pcm)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# WebSocket 通信
# ---------------------------------------------------------------------------

async def ws_text_and_stream(text: str, ws_url: str, session_id: str) -> list:
    """发送文字给 Charlie，收集 TTS 音频块。"""
    tts_chunks = []
    try:
        async with websockets.connect(ws_url, ping_interval=30, max_size=10*1024*1024) as ws:
            # 等待连接确认
            ack = await asyncio.wait_for(ws.recv(), timeout=5)
            log.debug(f"Charlie 连接: {ack[:60]}")

            # 发送文字消息
            await ws.send(json.dumps({
                "type": "text",
                "message": text,
                "session_id": session_id,
            }))
            log.info(f"已发送: {text[:40]}...")

            # 收集 TTS 音频块
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    log.warning("等待回复超时")
                    break

                if isinstance(msg, str):
                    ev = json.loads(msg)
                    t = ev.get("type")
                    if t == "text":
                        print(f"  Charlie: {ev.get('text','')}", end="", flush=True)
                    elif t == "audio":
                        chunk_b64 = ev.get("data", "")
                        if chunk_b64:
                            chunk = base64.b64decode(chunk_b64)
                            tts_chunks.append(chunk)
                    elif t == "done":
                        print()  # 换行
                        break
                    elif t == "error":
                        log.error(f"Charlie 错误: {ev.get('message','')}")
                        break
                    elif t == "interrupted":
                        log.info("对话被打断")
                        break
                elif isinstance(msg, bytes):
                    tts_chunks.append(msg)
    except Exception as e:
        log.error(f"WebSocket 错误: {e}")
    return tts_chunks

async def ws_audio_and_stream(audio_wav: bytes, ws_url: str, session_id: str) -> list:
    """发送音频给 Charlie，收集 TTS 音频块。"""
    tts_chunks = []
    try:
        async with websockets.connect(ws_url, ping_interval=30, max_size=10*1024*1024) as ws:
            ack = await asyncio.wait_for(ws.recv(), timeout=5)
            log.debug(f"Charlie 连接: {ack[:60]}")

            audio_b64 = base64.b64encode(audio_wav).decode()
            await ws.send(json.dumps({
                "type": "audio",
                "data": audio_b64,
                "format": "wav",
                "session_id": session_id,
            }))
            log.info(f"已发送音频: {len(audio_wav)} bytes")

            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    log.warning("等待回复超时")
                    break

                if isinstance(msg, str):
                    ev = json.loads(msg)
                    t = ev.get("type")
                    if t == "asr":
                        print(f"  识别: {ev.get('text','')}", end="", flush=True)
                    elif t == "text":
                        print(f"  Charlie: {ev.get('text','')}", end="", flush=True)
                    elif t == "audio":
                        chunk_b64 = ev.get("data", "")
                        if chunk_b64:
                            chunk = base64.b64decode(chunk_b64)
                            tts_chunks.append(chunk)
                    elif t == "done":
                        print()
                        break
                    elif t == "error":
                        log.error(f"Charlie 错误: {ev.get('message','')}")
                        break
                elif isinstance(msg, bytes):
                    tts_chunks.append(msg)
    except Exception as e:
        log.error(f"WebSocket 错误: {e}")
    return tts_chunks

# ---------------------------------------------------------------------------
# TTS 播放
# ---------------------------------------------------------------------------

def play_tts_chunks(chunks: list, output_name: str = None):
    """将 TTS 音频块写入临时文件并通过 afplay 播放。"""
    if not chunks:
        return
    tmp = "/tmp/charlie_tts_output.wav"
    with wave.open(tmp, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        for chunk in chunks:
            if chunk:
                wf.writeframes(chunk)
    try:
        if output_name:
            # macOS afplay 用 -j 参数匹配设备名（部分 AirPlay 设备名）
            subprocess.run(["afplay", "-j", "1", tmp], check=True, timeout=60)
        else:
            subprocess.run(["afplay", tmp], check=True, timeout=60)
        log.info("TTS 播放完成")
    except subprocess.TimeoutExpired:
        log.warning("TTS 播放超时")
    except subprocess.CalledProcessError as e:
        log.warning(f"播放失败（设备可能离线）: {e}")

# ---------------------------------------------------------------------------
# AirPlay 设备发现
# ---------------------------------------------------------------------------

def get_airplay_devices():
    """通过 Bonjour 发现局域网 AirPlay 设备。"""
    try:
        from zeroconf import Zeroconf
        zc = Zeroconf()
        browser = zc.ServiceBrowser(zc, "_airplay._tcp.local.")
        time.sleep(1.5)
        devices = []
        for service in zc.cache.values():
            name = getattr(service, "name", None)
            if name and "_airplay" in str(service):
                devices.append(name)
        return list(set(devices))
    except Exception as e:
        log.debug(f"AirPlay 发现失败: {e}")
        return []

# ---------------------------------------------------------------------------
# Charlie 服务健康检查
# ---------------------------------------------------------------------------

def check_charlie(ws_url: str) -> bool:
    http_url = ws_url.replace("ws://", "http://").replace("/ws", "")
    try:
        import urllib.request
        urllib.request.urlopen(http_url, timeout=2)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# 打字对话模式（不需要麦克风）
# ---------------------------------------------------------------------------

def run_text_mode(ws_url: str, session_id: str, output_device: str = None):
    log.info("=" * 50)
    log.info("Charlie Voice Bridge — 打字模式")
    log.info("Charlie 服务: " + ws_url)
    log.info("输入 Ctrl+C 或 quit 退出")
    log.info("=" * 50)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            text = input("\n你说: ").strip()
            if not text:
                continue
            if text.lower() in ("quit", "exit", "退出"):
                break
            if text.lower() in ("airplay", "设备"):
                devs = get_airplay_devices()
                print("发现 AirPlay 设备:", devs if devs else "无")
                continue

            tts_chunks = loop.run_until_complete(
                ws_text_and_stream(text, ws_url, session_id)
            )
            if tts_chunks:
                print(f"  → TTS {len(tts_chunks)} 块，播放中...")
                play_tts_chunks(tts_chunks, output_device)
            else:
                print("  → 无 TTS 输出")
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"错误: {e}")

    loop.close()
    log.info("退出")

# ---------------------------------------------------------------------------
# 语音模式（需要麦克风）
# ---------------------------------------------------------------------------

def run_voice_mode(ws_url: str, session_id: str, input_device: int, output_device: str = None):
    import sounddevice as sd

    # 确认麦克风可用
    if input_device is None:
        log.error("找不到麦克风设备，请先连接 USB 麦克风或 AirPods")
        log.info("提示: Mac mini 无自带麦克风，需要外接设备")
        return

    dev_info = sd.query_devices()[input_device]
    log.info(f"麦克风: {dev_info['name']}")

    log.info("=" * 50)
    log.info("Charlie Voice Bridge — 语音模式")
    log.info("说'查理'触发（或直接说话）")
    log.info("输入 Ctrl+C 退出")
    log.info("=" * 50)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        while True:
            print("\n等待说话...")
            audio_pcm = record_with_vad(input_device)
            if len(audio_pcm) < 3200:  # < 100ms
                log.info("录音太短，跳过")
                continue
            audio_wav = pcm_to_wav(audio_pcm)
            tts_chunks = loop.run_until_complete(
                ws_audio_and_stream(audio_wav, ws_url, session_id)
            )
            if tts_chunks:
                play_tts_chunks(tts_chunks, output_device)
    except KeyboardInterrupt:
        pass

    loop.close()
    log.info("退出")

# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Charlie Voice Bridge")
    parser.add_argument("--voice", action="store_true", help="语音模式（需麦克风）")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8000/ws",
                        help="Charlie WebSocket 地址")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备")
    parser.add_argument("--input-device", type=int, default=None,
                        help="输入设备索引（麦克风）")
    parser.add_argument("--output-device", type=str, default=None,
                        help="输出设备名（AirPlay 设备名或部分匹配）")
    parser.add_argument("--session-id", default="bridge_mac",
                        help="会话 ID")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    # 健康检查
    if not check_charlie(args.ws_url):
        log.error(f"无法连接到 Charlie 服务: {args.ws_url}")
        log.error("请确认 Charlie 已在 localhost:8000 运行")
        return

    # 发现 AirPlay 设备
    if args.output_device:
        devs = get_airplay_devices()
        if devs:
            print(f"发现 AirPlay 设备: {devs}")
    elif args.voice:
        devs = get_airplay_devices()
        if devs:
            print(f"局域网 AirPlay 设备: {devs}")
            print("提示: 通过 '系统设置 → 声音 → 输出' 选择目标设备，afplay 将输出到选定设备")

    input_dev = args.input_device
    if args.voice:
        if input_dev is None:
            input_dev = get_default_input()
        if input_dev is None:
            log.error("错误: 没有可用的麦克风设备")
            log.info("Mac mini 无自带麦克风，请连接 USB 麦克风或 AirPods")
            log.info("或者使用打字模式: python3 voice_bridge.py")
            return
        run_voice_mode(args.ws_url, args.session_id, input_dev, args.output_device)
    else:
        run_text_mode(args.ws_url, args.session_id, args.output_device)

if __name__ == "__main__":
    main()
