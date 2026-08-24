#!/usr/bin/env python3
# 实验性代码：未集成到主服务，仅用于独立调试和原型验证。
"""
Charlie · 面壁 MiniCPM-o-4.5-Realtime 实时语音对话

本机麦克风/扬声器 <-> wss://api.modelbest.cn/v1/realtime 双向流式音频。
服务端 VAD 自动断句, ASR -> 大脑 -> TTS 全在面壁侧端到端完成;
本地只负责收音、推流、收响应并流式播放, 支持说话打断(barge-in)。

依赖(已在 charlie/.venv): websockets, sounddevice, numpy, python-dotenv
外部二进制: 无需 ffmpeg(sounddevice 直接走 CoreAudio/PortAudio)

用法:
    python realtime_voice.py                 # 默认麦克风/扬声器, 直接对话
    python realtime_voice.py --list-devices  # 列出音频设备
    python realtime_voice.py --input 2 --output 3
    python realtime_voice.py --smoke         # 只连一次, 打印前若干事件后退出(协议自检)
    python realtime_voice.py --instructions "你是 Charlie, 智能家居助手"

环境变量(charlie/.env):
    MODELBEST_API_KEY   面壁实时 Key, 形如 map-api-key.sk-live-...
    MODELBEST_MODEL     可选, 默认 MiniCPM-o-4.5-Realtime
    MODELBEST_VOICE     可选音色(未知可省略)
    MODELBEST_URL       可选, 默认官方地址
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# .env 加载(不强制要求 dotenv 安装, 装了更好)
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

try:
    import websockets
except Exception as e:  # pragma: no cover
    print(f"[ERROR] 缺少 websockets: {e}\n  pip install websockets", file=sys.stderr)
    sys.exit(1)

DEFAULT_URL = "wss://api.modelbest.cn/v1/realtime"
DEFAULT_MODEL = "MiniCPM-o-4.5-Realtime"
DEFAULT_INSTRUCTIONS = (
    "你是 Charlie, 一个住在用户家里的中文语音助手。用简短、口语化的中文回答, "
    "一次只说一两句, 像真人对话一样自然。可以控制家电、查天气、定提醒、闲聊。"
)
SAMPLE_RATE = 24000        # PCM16 24kHz mono(OpenAI/面壁 realtime 默认)
IN_BLOCK = 960             # 上行 40ms / 帧
OUT_BLOCK = 480            # 下行 20ms / 帧
LEAD_IN_SEC = 0.25         # 开始播放前缓冲 250ms, 避免开头爆音/underrun
IDLE_STOP_BLOCKS = 150     # 连续 ~3s 空闲则关输出流, 释放设备


# ── 扬声器流式播放 ──────────────────────────────────────────
class AudioPlayer:
    """低延迟流式播放: 收到音频 delta 即攒入缓冲, 达到 lead-in 后启动输出流;
    callback 边拉边放; 响应结束(drain)或长时间空闲则自动停流。"""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None
        self._sd = None
        self._started = False
        self._draining = False
        self._muted = False      # 打断后丢弃旧响应残余 delta, 直到新 response.created
        self._idle = 0
        self.lead_bytes = int(SAMPLE_RATE * LEAD_IN_SEC) * 2

    def feed(self, pcm: bytes):
        with self._lock:
            if self._muted:
                return
            self._buf.extend(pcm)
            need = (not self._started) and len(self._buf) >= self.lead_bytes
        if need:
            self._start()

    def _start(self):
        try:
            import sounddevice as sd
            self._sd = sd
            self._started = True
            self._draining = False
            self._idle = 0
            self._stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE, dtype="int16", channels=1,
                blocksize=OUT_BLOCK, callback=self._cb,
                finished_callback=self._finished,
            )
            self._stream.start()
        except Exception as e:
            print(f"[ERROR] 打开扬声器失败: {e}", file=sys.stderr)
            self._started = False

    def _cb(self, outdata, frames, time_info, status):
        n = frames * 2  # int16 mono -> 字节数
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            empty = len(self._buf) == 0
            draining = self._draining
            if self._muted:
                chunk = b""
        if len(chunk) < n:
            chunk += b"\x00" * (n - len(chunk))   # 暂时不足, 补静音避免杂音
        outdata[:] = chunk
        if empty:
            self._idle += 1
            if draining or self._idle >= IDLE_STOP_BLOCKS:
                raise self._sd.CallbackStop
        else:
            self._idle = 0

    def _finished(self):
        with self._lock:
            self._stream = None
            self._started = False
            self._draining = False
            self._idle = 0

    def drain(self):
        """响应结束: 把残余缓冲放完后自动停流。"""
        with self._lock:
            self._draining = True

    def interrupt(self):
        """用户开始说话: 清空缓冲, 静音, 等下个 response.created 再解除。"""
        with self._lock:
            del self._buf[:]
            self._muted = True
            self._idle = 0

    def unmute(self):
        with self._lock:
            self._muted = False

    def stop(self):
        with self._lock:
            self._draining = True
            st = self._stream
        if st:
            try:
                st.stop(); st.close()
            except Exception:
                pass
        with self._lock:
            self._stream = None
            self._started = False


# ── 麦克风推流 ─────────────────────────────────────────────
class MicStreamer:
    def __init__(self, loop: asyncio.AbstractEventLoop, send_q: asyncio.Queue):
        self._loop = loop
        self._q = send_q
        self._stream = None

    def start(self, device=None):
        try:
            import sounddevice as sd
        except Exception as e:
            print(f"[ERROR] 缺少 sounddevice: {e}", file=sys.stderr)
            return False
        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, dtype="int16", channels=1,
                blocksize=IN_BLOCK, device=device, callback=self._cb,
            )
            self._stream.start()
            return True
        except Exception as e:
            print(f"[ERROR] 打开麦克风失败: {e}", file=sys.stderr)
            print("  用 --list-devices 查设备, 用 --input N 指定输入设备", file=sys.stderr)
            return False

    def _cb(self, indata, frames, time_info, status):
        if status and getattr(status, "input_overflow", False):
            return
        b64 = base64.b64encode(bytes(indata)).decode("ascii")
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, b64)
        except RuntimeError:
            pass  # 事件循环已关闭

    def stop(self):
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None


# ── 协议事件处理 ───────────────────────────────────────────
class RealtimeSession:
    def __init__(self, args, player: AudioPlayer):
        self.args = args
        self.player = player
        self._resp_header = False

    async def handle(self, ws, ev: dict):
        t = ev.get("type", "")
        if t == "session.created":
            print("[i] 会话已建立")
        elif t == "session.updated":
            print("[i] 会话配置已生效 -> 开始聆听")
        elif t == "input_audio_buffer.speech_started":
            self.player.interrupt()
            self._resp_header = False
            print("\n🧑 (聆听中…)")
        elif t == "input_audio_buffer.speech_stopped":
            pass
        elif t == "input_audio_buffer.committed":
            pass
        elif t == "conversation.item.input_audio_transcription.completed":
            txt = ev.get("transcript", "")
            if txt:
                print(f"\n🧑 {txt}")
        elif t == "response.created":
            self.player.unmute()
        elif t == "response.audio.delta":
            d = ev.get("delta")
            if d:
                self.player.feed(base64.b64decode(d))
        elif t == "response.audio_transcript.delta":
            if not self._resp_header:
                sys.stdout.write("\n🤖 "); sys.stdout.flush(); self._resp_header = True
            sys.stdout.write(ev.get("delta", "")); sys.stdout.flush()
        elif t == "response.audio_transcript.done":
            if self._resp_header:
                sys.stdout.write("\n"); sys.stdout.flush(); self._resp_header = False
        elif t == "response.text.delta":
            if not self._resp_header:
                sys.stdout.write("\n🤖 "); sys.stdout.flush(); self._resp_header = True
            sys.stdout.write(ev.get("delta", "")); sys.stdout.flush()
        elif t == "response.text.done":
            if self._resp_header:
                sys.stdout.write("\n"); sys.stdout.flush(); self._resp_header = False
        elif t == "response.audio.done":
            pass
        elif t == "response.done":
            self.player.drain()
        elif t == "error":
            err = ev.get("error", {})
            print(f"\n[ERROR] {err.get('code','?')}: {err.get('message', ev)}", file=sys.stderr)
        else:
            # 未知事件: 打印类型 + 截断内容, 便于核对面壁协议差异
            snippet = json.dumps(ev, ensure_ascii=False)
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            print(f"[event] {t}  {snippet}")


def build_session_update(args):
    session = {
        "modalities": ["audio", "text"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
        },
        "instructions": args.instructions,
    }
    if args.voice:
        session["voice"] = args.voice
    return json.dumps({"type": "session.update", "session": session})


def connect_url(args):
    base = args.url or os.getenv("MODELBEST_URL", DEFAULT_URL)
    return f"{base}?mode=audio&model={args.model}"


def auth_subprotocols():
    key = os.getenv("MODELBEST_API_KEY", "").strip()
    if not key:
        print("[ERROR] 未配置 MODELBEST_API_KEY\n  请在 charlie/.env 设置: MODELBEST_API_KEY=map-api-key.sk-...", file=sys.stderr)
        sys.exit(2)
    if not key.startswith("map-api-key."):
        key = "map-api-key." + key
    return ["map.realtime", key]


async def send_loop(ws, q: asyncio.Queue):
    while True:
        b64 = await q.get()
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
        except Exception as e:
            print(f"[WARN] 推流失败: {e}", file=sys.stderr)
            break


async def smoke(args):
    """协议自检: 连上 -> 发 session.update -> 打印前若干事件 -> 退出。"""
    print(f"[smoke] 连接 {connect_url(args)}")
    async with websockets.connect(connect_url(args), subprotocols=auth_subprotocols(),
                                  open_timeout=15, close_timeout=5) as ws:
        await ws.send(build_session_update(args))
        deadline = time.time() + (args.smoke_sec or 8)
        count = 0
        async for raw in ws:
            try:
                ev = json.loads(raw)
            except Exception:
                print("[smoke] non-json:", raw[:200]); continue
            t = ev.get("type", "?")
            snippet = json.dumps(ev, ensure_ascii=False)
            if len(snippet) > 240:
                snippet = snippet[:240] + "…"
            print(f"[smoke] {t}  {snippet}")
            count += 1
            if time.time() > deadline or count >= (args.smoke_max or 20):
                print(f"[smoke] 结束 (共 {count} 个事件)")
                break


async def run(args):
    loop = asyncio.get_running_loop()
    send_q: asyncio.Queue = asyncio.Queue()
    player = AudioPlayer()
    mic = MicStreamer(loop, send_q)
    session = RealtimeSession(args, player)
    ready = asyncio.Event()

    print(f"[i] 连接 {connect_url(args)}")
    async with websockets.connect(connect_url(args), subprotocols=auth_subprotocols(),
                                  open_timeout=15, close_timeout=5) as ws:
        await ws.send(build_session_update(args))

        async def receiver():
            try:
                async for raw in ws:
                    try:
                        await session.handle(ws, json.loads(raw))
                    except Exception as e:
                        print(f"[WARN] 处理事件异常: {e}", file=sys.stderr)
                    if not ready.is_set():
                        ready.set()  # 收到任意事件即放开收音(兼容服务端事件名差异)
            except Exception as e:
                print(f"[ERROR] 连接断开: {e}", file=sys.stderr)

        async def start_mic_when_ready():
            try:
                await asyncio.wait_for(ready.wait(), timeout=4)
            except asyncio.TimeoutError:
                print("[WARN] 4s 未收到事件, 仍开始收音", file=sys.stderr)
            ok = mic.start(args.input)
            if ok:
                print("\n" + "=" * 46)
                print("  🎤 正在聆听, 直接说话即可 (Ctrl-C 退出)")
                print("=" * 46 + "\n")

        tasks = [
            asyncio.create_task(receiver()),
            asyncio.create_task(start_mic_when_ready()),
            asyncio.create_task(send_loop(ws, send_q)),
        ]
        try:
            await tasks[0]  # 连接断开即结束
        except asyncio.CancelledError:
            pass
        finally:
            for tk in tasks:
                tk.cancel()
            mic.stop()
            player.stop()


def list_devices():
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"[ERROR] 缺少 sounddevice: {e}", file=sys.stderr)
        return
    print("音频设备列表 (input/output):")
    for i, d in enumerate(sd.query_devices()):
        ch = f"in{d['max_input_channels']}/out{d['max_output_channels']}"
        print(f"  [{i:>2}] {ch:>14}  {d['name']}")


def main():
    ap = argparse.ArgumentParser(description="面壁 MiniCPM-o-4.5-Realtime 实时语音对话")
    ap.add_argument("--model", default=os.getenv("MODELBEST_MODEL", DEFAULT_MODEL))
    ap.add_argument("--url", default=None, help="实时地址, 默认官方")
    ap.add_argument("--voice", default=os.getenv("MODELBEST_VOICE"), help="可选音色")
    ap.add_argument("--instructions", default=os.getenv("MODELBEST_INSTRUCTIONS", DEFAULT_INSTRUCTIONS))
    ap.add_argument("--input", type=int, default=None, help="麦克风设备号(--list-devices 查)")
    ap.add_argument("--output", type=int, default=None, help="扬声器设备号(当前用默认)")
    ap.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    ap.add_argument("--smoke", action="store_true", help="只做协议自检: 连上打印事件后退出")
    ap.add_argument("--smoke-sec", type=int, default=8)
    ap.add_argument("--smoke-max", type=int, default=20)
    args = ap.parse_args()

    if args.list_devices:
        list_devices(); return
    if args.smoke:
        asyncio.run(smoke(args)); return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[i] 已退出")


if __name__ == "__main__":
    main()
