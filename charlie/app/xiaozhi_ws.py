"""Xiaozhi-compatible WebSocket endpoint (v2.1.0 firmware compatible).

The v2.1.0 firmware streams continuous 60ms Opus frames after listen start and
does NOT send listen stop reliably in auto mode. The server performs endpointing:
it decodes frames, measures energy, and when ~1.2s of silence follows speech,
it processes the utterance (ASR→Brain→TTS) and streams back Opus audio.
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import threading
import subprocess
import time
import uuid
import audioop
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.audio import likely_empty_audio
from app.auth import AUTH_TOKEN
from app.xiaozhi_codec import (
    opus_decode_to_wav, mp3_to_opus_packets, mp3_to_ogg_opus,
    url_to_opus_packets,
)

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    try:
        import opuslib
    except ImportError:
        opuslib = None
        logging.getLogger(__name__).warning("opuslib not installed; xiaozhi Opus decode unavailable")

log = logging.getLogger(__name__)

DOWNLINK_SAMPLE_RATE = 24000
UPLINK_SAMPLE_RATE = 16000
OPUS_FRAME_DURATION_MS = 60
HEAD_START_FRAMES = 4
FRAME_SAMPLES = UPLINK_SAMPLE_RATE * OPUS_FRAME_DURATION_MS // 1000  # 960

# Endpointing: if energy below the (adaptive) speech threshold for
# SILENCE_FRAMES consecutive frames after at least SPEECH_FRAMES of audio,
# consider utterance complete. Speech RMS ~200-300, noise ~20-60.
SILENCE_FRAMES = 20   # 20 * 60ms = 1.2s (RMS fallback when Silero unavailable)
# Silero VAD (神经网络语音检测, 比RMS能量阈值准→静音确认可更短, 砍尾静音延迟)
SILENCE_FRAMES_VAD = int(os.getenv("XIAOZHI_VAD_SILENCE_FRAMES", "6"))  # 6*60ms=0.36s
VAD_THRESHOLD = float(os.getenv("XIAOZHI_VAD_THRESHOLD", "0.5"))  # Silero 语音概率阈值
VAD_ENABLED = os.getenv("XIAOZHI_VAD_ENABLED", "1") == "1"  # 0 强制 RMS 降级
# Min hot frames for a valid utterance. Kept low (12) so short commands like
# "几点"/"开灯" (~0.7s) still endpoint; ambient noise is filtered separately
# by NOISE_DROP_FRAMES below.
MIN_SPEECH_FRAMES = 12
MAX_UTTERANCE_FRAMES = 600   # ~36s hard cap before forced endpoint
BARGE_IN_FRAMES = 3   # consecutive hot frames needed to trigger barge-in/speech
# If the buffer grows this large without ever reaching MIN_SPEECH_FRAMES, it's
# probably ambient noise — drop silently instead of waiting out the 36s cap.
NOISE_DROP_FRAMES = 100   # ~6s
# Adaptive speech threshold: threshold = SIG_RATIO * running noise floor, so
# quiet rooms (noise ~20) still trigger at ~50 while loud rooms (~120) need
# ~300 — both less error-prone than the old fixed 150.
SIG_RATIO = 2.5
SILENCE_RMS_DEFAULT = 150   # fallback before a noise baseline exists

# Wake-gating: after the device is woken (listen/detect) or enters listening
# (listen/start), we only accept utterances for the next ARM_WINDOW seconds.
# Anything heard outside that window (ambient conversation, TV, music) is
# dropped silently so the device never talks on its own. Receiving a valid
# command refreshes the window, keeping a normal back-and-forth alive.
# 0 disables the gate.
# After a wake (listen/detect) the device's own speaker beep + the wake word
# itself can momentarily re-trigger our endpointing. Ignore fresh hot frames
# for this long so the acknowledgement tone and echo settle before the mic
# starts a new utterance. (KWS echo protection / wake cooldown, see below.)
# Must stay short (~beep length + a little reverb) so a user who speaks right
# after the wake word isn't dropped.
WAKE_ECHO_COOLDOWN = 0.5
_FIRST_SENTENCE_TIMEOUT = float(os.getenv("XIAOZHI_FIRST_SENTENCE_TIMEOUT", "15.0"))


def _get_arm_window() -> float:
    """返回当前角色对应的 ARM_WINDOW（对话连续窗口秒数）。

    默认 120s（支持主动服务和连续对话），可通过环境变量覆盖：
    - XIAOZHI_ARM_WINDOW：全局默认窗口（0=禁用断开，保持长连接供主动推送）
    - XIAOZHI_JARVIS_ARM_WINDOW：jarvis 角色专用覆盖
    角色切换后，下一次窗口判断即时生效（每次调用读取当前角色）。
    """
    try:
        from agent.roles import get_current_role
        role = get_current_role()
        if role == "jarvis":
            return float(os.getenv("XIAOZHI_JARVIS_ARM_WINDOW", "300.0"))
    except Exception:
        pass
    return float(os.getenv("XIAOZHI_ARM_WINDOW", "120.0"))


_prewarm_started = False
_prewarm_lock = threading.Lock()

# 独立 brain collect 线程池，避免卡死时耗尽共享 _io_pool（ASR/TTS/opus 等）
_brain_pool = ThreadPoolExecutor(max_workers=int(os.getenv("BRAIN_POOL_SIZE", "8")), thread_name_prefix="brain-collect")
_dead_link_count = 0
_rebuild_last_time = 0.0
_REBUILD_MIN_INTERVAL = 10.0
_tts_streaming = False
_tts_streaming_lock = threading.Lock()
_rebuild_lock = threading.Lock()


def is_xiaozhi_streaming() -> bool:
    """Return True if a conversation is currently streaming TTS to the device."""
    with _tts_streaming_lock:
        return _tts_streaming


def _set_xiaozhi_streaming(val: bool) -> None:
    global _tts_streaming
    with _tts_streaming_lock:
        _tts_streaming = val


def rebuild_brain_pool() -> None:
    """重建 _brain_pool，释放被死链接占用的 worker 线程。

    死链接场景：MCP server hang、网络中断导致 brain.run 永久阻塞；
    此时 _brain_pool 中的 worker 被卡死不释放，新请求无法得到执行。
    重建后旧池的线程随进程退出回收，新池立即可用。
    """
    global _brain_pool, _dead_link_count, _rebuild_last_time
    _BRAIN_POOL_SIZE = int(os.getenv("BRAIN_POOL_SIZE", "8"))
    with _rebuild_lock:
        now = time.monotonic()
        if now - _rebuild_last_time < _REBUILD_MIN_INTERVAL:
            log.warning("[xiaozhi] brain_pool 重建过于频繁，跳过(距上次%.1fs)",
                        now - _rebuild_last_time)
            return
        _rebuild_last_time = now
        old = _brain_pool
        _brain_pool = ThreadPoolExecutor(max_workers=_BRAIN_POOL_SIZE, thread_name_prefix="brain-collect")
        old.shutdown(wait=False, cancel_futures=True)
        _dead_link_count += 1
        log.warning("[xiaozhi] brain_pool 重建(累计死链%d次), 旧池线程随进程退出回收",
                    _dead_link_count)

# ── Silero VAD (懒加载, 复用 local_wake 的模型单例避免重复加载) ──
_silero_vad = None
_silero_lock = threading.Lock()

def _load_silero_vad():
    """加载 Silero VAD 模型(懒加载, 失败返回 None→端点降级 RMS)"""
    global _silero_vad
    if _silero_vad is not None:
        return _silero_vad
    if not VAD_ENABLED:
        return None
    with _silero_lock:
        if _silero_vad is not None:
            return _silero_vad
        try:
            from silero_vad import load_silero_vad
            _silero_vad = load_silero_vad()
            log.info("[xiaozhi] Silero VAD loaded (端点检测: 0.48s vs RMS 1.2s)")
        except Exception as e:
            log.warning("[xiaozhi] Silero VAD load fail, fallback to RMS endpointing: %s", e)
            _silero_vad = None  # 显式 None, 降级 RMS
        return _silero_vad

def _vad_speech_prob(pcm_bytes: bytes) -> float:
    """Silero VAD 语音概率 0-1; 返回 -1 表示 VAD 不可用(降级 RMS)"""
    model = _load_silero_vad()
    if model is None:
        return -1.0
    try:
        import numpy as np
        import torch
        a = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        # Silero 要求 512 samples (16kHz, 32ms); 60ms 帧=960 samples, 取前 512
        if len(a) < 512:
            a = np.pad(a, (0, 512 - len(a)))
        t = torch.from_numpy(a[:512])
        return float(model(t, UPLINK_SAMPLE_RATE).item())
    except Exception:
        return -1.0

def _is_speech_vad(pcm_bytes: bytes, rms: int, rms_threshold: float) -> bool:
    """语音判定: VAD 优先(概率>=阈值), 不可用降级 RMS(>=阈值)"""
    prob = _vad_speech_prob(pcm_bytes)
    if prob >= 0:
        return prob >= VAD_THRESHOLD
    # 降级 RMS
    return rms >= rms_threshold

_beep_mp3 = None
_beep_lock = threading.Lock()


def _synth_wake_beep() -> bytes:
    """Synthesize a short ~150ms acknowledgement tone once, then reuse it.

    Pure sine at 880Hz, MP3, quiet enough that the device's mic won't hear it
    as speech. Serves as instant audible feedback on wake (状态机: 唤醒→反馈音).
    """
    global _beep_mp3
    if _beep_mp3:
        return _beep_mp3
    with _beep_lock:
        if _beep_mp3:
            return _beep_mp3
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 "sine=frequency=880:duration=0.15",
                 "-b:a", "32k", "-ac", "1", "-ar", "24000", "-f", "mp3",
                 "pipe:1"],
                capture_output=True, timeout=10)
            if r.stdout and len(r.stdout) > 100:
                _beep_mp3 = r.stdout
                log.info("[xiaozhi] wake beep synthesized (%d B)", len(r.stdout))
        except Exception as e:
            log.warning("[xiaozhi] wake beep synth fail: %s", e)
            _beep_mp3 = b""
        return _beep_mp3


_finna_cooldown_until = 0.0  # Finna 429 冷却截止时间戳（限流后60s内跳过Finna）


def _synth_mp3(sentence: str, *, _cache_get=None, _cache_put=None) -> bytes:
    """TTS via Baidu → Finna (SSE streaming) → StepFun → say. Falls back gracefully.

    Cached in the shared TTS mp3 cache so repeated hints / replies skip the
    network round-trip entirely (seconds → instant). Cache writes happen
    here so prewarm and live replies share one path.

    _cache_get/_cache_put are injection points for tests to verify cache keys
    include the role voice (avoiding cross-role cache pollution).
    """
    from agent.asr_tts import _tts_cache_get, _tts_cache_put
    from agent.asr_tts import get_effective_tts_config, TTS_MODEL as _TTS_MODEL
    from agent.asr_tts import resolve_voice
    _role_voice, _role_speed = get_effective_tts_config()
    _cg = _cache_get or _tts_cache_get
    _cp = _cache_put or _tts_cache_put
    # 测试注入的 fake_get/fake_put 可能不接收 speed 参数；仅对真实缓存函数传递 speed
    if _cache_get is not None:
        cached = _cg(sentence, _role_voice, _TTS_MODEL)
    else:
        cached = _tts_cache_get(sentence, _role_voice, _TTS_MODEL, speed=_role_speed)
    if cached:
        return cached
    api_key = os.getenv("FINNA_API_KEY", "")
    global _finna_cooldown_until
    # Finna 只支持 "Ethan" 音色；用 resolve_voice 判断映射结果是否为 Ethan
    _finna_voice = resolve_voice(_role_voice, "finna")
    finna_ok = api_key and (_finna_voice == "Ethan" or not _role_voice)
    if finna_ok and time.time() < _finna_cooldown_until:
        finna_ok = False

    # Baidu first (faster ~50ms, supports multi-voice via per mapping)
    try:
        from agent.asr_tts import _tts_baidu
        raw = _tts_baidu(sentence)
        if raw and len(raw) > 100:
            # 百度已返回 mp3，直接缓存，跳过冗余 ffmpeg 重编码
            # opus 编码器能处理任意码率 mp3（先解码到 PCM 再编码）
            if _cache_put is not None:
                _cp(sentence, raw, _role_voice, _TTS_MODEL)
            else:
                _cp(sentence, raw, _role_voice, _TTS_MODEL, speed=_role_speed)
            return raw
    except Exception as e:
        log.warning("[xiaozhi] baidu TTS fail: %s", e)

    # Finna second (SSE streaming, only for Ethan voice)
    if finna_ok:
        try:
            import requests as _r
            resp = _r.post(
                "https://www.finna.com.cn/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "apikey": api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": "qwen3-tts-flash",
                    "input": sentence,
                    "voice": _finna_voice,
                    "response_format": "mp3",
                    "speed": _role_speed,
                    "stream": True,
                },
                timeout=20,
                stream=True,
            )
            resp.raise_for_status()
            wav_parts = []
            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        b64 = obj.get("audio") or obj.get("data") or ""
                        if b64:
                            wav_parts.append(base64.b64decode(b64))
            finally:
                resp.close()
            if wav_parts:
                wav = b"".join(wav_parts)
                # convert WAV to MP3 for opus encoder
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k",
                     "-ac", "1", "-f", "mp3", "pipe:1"],
                    input=wav, capture_output=True, timeout=15)
                if r.stdout and len(r.stdout) > 100:
                    if _cache_put is not None:
                        _cp(sentence, r.stdout, _role_voice, _TTS_MODEL)
                    else:
                        _cp(sentence, r.stdout, _role_voice, _TTS_MODEL, speed=_role_speed)
                    return r.stdout
            if _cache_put is not None:
                _cp(sentence, wav, _role_voice, _TTS_MODEL)
            else:
                _cp(sentence, wav, _role_voice, _TTS_MODEL, speed=_role_speed)
            return wav
        except Exception as e:
            log.warning("[xiaozhi] finna TTS fail: %s", e)
            if "429" in str(e) or "Too Many" in str(e):
                _finna_cooldown_until = time.time() + 60
                log.warning("[xiaozhi] finna 限流，冷却60s直接走百度")

    # StepFun third (cloud TTS, no known rate-limit)
    try:
        from agent.asr_tts import _tts_stepfun
        raw = _tts_stepfun(sentence)
        if raw and len(raw) > 100:
            if _cache_put is not None:
                _cp(sentence, raw, _role_voice, _TTS_MODEL)
            else:
                _cp(sentence, raw, _role_voice, _TTS_MODEL, speed=_role_speed)
            return raw
    except Exception as e:
        log.warning("[xiaozhi] stepfun TTS fail: %s", e)

    # Last-resort offline TTS via macOS `say` → mp3, so the device still talks
    # even when every network TTS is down (ASR already worked, net may be flaky).
    try:
        raw = subprocess.run(
            ["say", "-o", "-", "--data-format=LEI16@24000", sentence],
            capture_output=True, timeout=20)
        if raw.returncode == 0 and raw.stdout:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k",
                 "-ac", "1", "-f", "mp3", "pipe:1"],
                input=raw.stdout, capture_output=True, timeout=15)
            if r.stdout and len(r.stdout) > 100:
                log.info("[xiaozhi] offline say TTS fallback used")
                if _cache_put is not None:
                    _cp(sentence, r.stdout, _role_voice, _TTS_MODEL)
                else:
                    _cp(sentence, r.stdout, _role_voice, _TTS_MODEL, speed=_role_speed)
                return r.stdout
    except Exception as e:
        log.warning("[xiaozhi] offline say TTS fail: %s", e)
    raise RuntimeError("no TTS available")


def register_xiaozhi_routes(app: FastAPI):

    # Pre-synthesize the common hint phrases once per process so the first
    # "say it again" / "got it" reply skips the network TTS round-trip.
    def _prewarm_hints():
        global _prewarm_started
        with _prewarm_lock:
            if _prewarm_started:
                return
            _prewarm_started = True
        from agent.intent import LOW_INTENT_ASR_REPLY
        try:
            for phrase in ("你好，有什么需要帮忙的", LOW_INTENT_ASR_REPLY):
                try:
                    mp3 = _synth_mp3(phrase)
                    if mp3:
                        log.info("[xiaozhi] prewarmed TTS: %.30s", phrase)
                except Exception as e:
                    log.warning("[xiaozhi] prewarm %r fail: %s", phrase, e)
        except Exception as e:
            log.warning("[xiaozhi] prewarm thread err: %s", e)

    threading.Thread(target=_prewarm_hints, daemon=True).start()

    # ---- session metrics (module-level, thread-safe counters) ----
    metrics = {
        "connections_total": 0,
        "ws_active": 0,
        "utterances_total": 0,
        "barges_total": 0,
        "resumes_total": 0,
        "asr_failures": 0,
        "tts_sentences": 0,
        "tts_failures": 0,
        "opus_cache_hits": 0,
        "url_cache_mem_hits": 0,
        "url_cache_disk_hits": 0,
        "music_plays": 0,
    }
    metrics_lock = threading.Lock()

    def bump_metric(name: str, delta: int = 1):
        with metrics_lock:
            metrics[name] = metrics.get(name, 0) + delta

    @app.get("/api/xiaozhi/status")
    def xiaozhi_status():
        """Expose ESP32-terminal health counters for dashboards / watchdog."""
        try:
            from app.xiaozhi_codec import _cache_stats as _cs
            stats = _cs()
        except Exception:
            stats = {}
        with metrics_lock:
            snap = dict(metrics)
        snap["cache"] = stats
        try:
            from voice_agent import brain_status
            snap["brain"] = brain_status()
        except Exception:
            pass
        return snap

    @app.websocket("/ws/xiaozhi")
    async def xiaozhi_websocket(ws: WebSocket):
        # 与 /ws 一致的鉴权策略：本地直连放行，否则校验 query token
        peer_ip = ws.client.host if ws.client else ""
        has_proxy_headers = bool(ws.headers.get("x-forwarded-for") or ws.headers.get("x-real-ip"))
        is_local = (not has_proxy_headers) and (peer_ip in ("127.0.0.1", "localhost", "::1", "", "testclient"))
        if AUTH_TOKEN and not is_local:
            token = ws.query_params.get("token", "")
            if not hmac.compare_digest(token or "", AUTH_TOKEN or ""):
                await ws.close(code=1008)
                return
        await ws.accept()
        log.info("[xiaozhi] client connected from %s", ws.client)
        bump_metric("connections_total")
        bump_metric("ws_active", 1)

        session_id = uuid.uuid4().hex[:16]
        device_key = session_id      # stable across reconnects once hello arrives
        conn_interrupted = ""         # partial reply saved on barge-in, for "继续"
        pending_sentences: list[str] = []   # sentences produced so far (replayable)
        resume_played = 0             # how many pending_sentences fully played
        stream_task: Optional[asyncio.Task] = None
        _stream_cancelled = threading.Event()  # 当前流的取消标志槽位（每流换新事件）

        # 注册到全局连接表（主动推送用）+ flush 待推送队列
        from app.state import register_xiaozhi_client, unregister_xiaozhi_client
        _loop = asyncio.get_running_loop()
        _pending = register_xiaozhi_client(session_id, ws, _loop)
        # flush 暂存的推送
        if _pending:
            async def _flush():
                from app.xiaozhi_codec import mp3_to_opus_packets
                for item in _pending:
                    try:
                        packets = await _loop.run_in_executor(None, mp3_to_opus_packets, item["mp3"])
                        await ws.send_text(json.dumps({"type": "tts", "state": "start"}))
                        await ws.send_text(json.dumps({"type": "tts", "state": "sentence_start", "text": item["text"]}))
                        for pkt in packets:
                            await ws.send_bytes(pkt)
                            await asyncio.sleep(0.06)  # 60ms 帧间隔
                        await ws.send_text(json.dumps({"type": "tts", "state": "stop"}))
                        log.info(f"[xiaozhi] 补发推送: {item['text'][:30]}")
                    except Exception as e:
                        log.warning(f"[xiaozhi] 补发失败: {e}")
            asyncio.ensure_future(_flush(), loop=_loop)

        async def cancel_stream():
            nonlocal stream_task
            _stream_cancelled.set()  # 通知 collect 线程停止合成
            if stream_task and not stream_task.done():
                stream_task.cancel()
                try:
                    await stream_task
                except (asyncio.CancelledError, Exception):
                    pass
            stream_task = None

        async def send_json(obj: dict):
            try:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

        async def stream_tts_reply(text: str, interrupted_reply: str = "",
                                   prebuilt: Optional[list] = None):
            """Send TTS Opus audio + llm message for a brain reply.

            prebuilt: if given, replay a fixed sentence list (skip the brain)
            — used to resume an interrupted reply from already-synthesized TTS.
            """
            from voice_agent import brain_stream_sentences
            loop = asyncio.get_running_loop()
            t0_start = loop.time()
            nonlocal conn_interrupted, resume_played, cur_volume_db, armed_until, noise_floor, stream_task
            nonlocal _stream_cancelled

            # 每流独立取消事件：collect 通过默认参数绑定本流的事件对象，
            # 新流开始不复位旧标志——被打断的 producer 检查到自己的事件
            # 仍为 set，会尽快退出而不是复活继续合成
            cancel_evt = threading.Event()
            _stream_cancelled = cancel_evt
            _set_xiaozhi_streaming(True)

            # True sentence-by-sentence pipeline: run the brain generator in a
            # background thread and feed each yielded sentence into an async
            # queue. The consumer below starts TTS on the first sentence the
            # moment it is produced (instead of waiting for the FULL reply),
            # then keeps synthesizing+playing while the brain still thinks.
            #
            # To eliminate inter-sentence gaps (playback underrun when the
            # device's decode queue drains before the next mp3 is ready), the
            # producer thread ALSO runs the network TTS synth (`_synth_mp3`)
            # while the consumer is busy playing the previous sentence. The
            # queue therefore carries (label, mp3) pairs; the consumer only
            # does fast local opus transcoding + paced sending.
            q: "asyncio.Queue[Optional[Tuple[str, bytes]]]" = asyncio.Queue(
                maxsize=16)
            _SENTINEL = object()  # end-of-stream marker
            _fallback_sent = False  # 防止兜底语重复发送

            def _put_blocking(item):
                # The consumer (pacing) is the bottleneck; block here so a long
                # reply never overflows the queue. This only stretches the
                # producer thread, never loses a sentence. Bail early if the
                # owning loop shuts down (WS disconnect / barge-in) or the
                # queue stays full for >50 iterations (~1s) instead of spinning
                # forever on a dead/orphaned consumer.
                _full_count = 0
                while True:
                    if not loop.is_running() or not ws.client_state == WebSocketState.CONNECTED:
                        return
                    try:
                        # asyncio.Queue 非线程安全：跨线程 put_nowait 后消费者
                        # 的唤醒依赖 call_soon，不会写自管道，loop 空闲时可能
                        # 迟迟不调度。put 本身 GIL 原子可直调，再补一发
                        # call_soon_threadsafe 空操作把 selector 叫醒。
                        q.put_nowait(item)
                        loop.call_soon_threadsafe(lambda: None)
                        return
                    except RuntimeError:
                        return  # loop 正在关闭
                    except asyncio.QueueFull:
                        _full_count += 1
                        if _full_count >= 50:
                            log.warning("[xiaozhi] _put_blocking 放弃: 队列持续满50次(~1s)")
                            return
                        time.sleep(0.02)

            def collect(cancel_evt=cancel_evt):
                # cancel_evt 默认参数在 def 时绑定本流的事件对象，
                # 旧 collect 不会因连接级槽位换新而误读新流状态
                nonlocal conn_interrupted, pending_sentences, _fallback_sent
                try:
                    if prebuilt is not None:
                        # Resume path: replay the not-yet-played sentences that
                        # were synthesized before the barge-in. Each is TTS-cached,
                        # so no network round-trip and no new brain inference.
                        for s in prebuilt:
                            if not s:
                                continue
                            if s.startswith("__MUSIC__"):
                                _put_blocking((s, None))
                                continue
                            if cancel_evt.is_set():
                                log.info("[xiaozhi] collect(prebuilt): consumer 已取消，停止生成")
                                break
                            mp3 = _synth_mp3(s)
                            if mp3 and len(mp3) > 100:
                                pending_sentences.append(s)
                                _put_blocking((s, mp3))
                        return
                    _ws_t0 = time.monotonic()
                    try:
                        from app.audit_log import audit_log
                        audit_log("ws:brain", input_data=text,
                                  action="start", session_id=f"xiaozhi-{device_key}")
                    except Exception:
                        pass
                    _brain_t0 = time.monotonic()
                    try:
                        _brain_iter = iter(brain_stream_sentences(
                            text, session_id="xiaozhi-" + device_key,
                            interrupted_reply=interrupted_reply,
                        ))
                    except Exception as e:
                        log.error("[xiaozhi] brain iter fail: %s", e)
                        # 往队列放兜底句子，让 consumer 正常走 TTS start→播报→stop
                        if not _fallback_sent:
                            try:
                                fallback = "抱歉，出了点问题，请再说一遍"
                                fb_mp3 = _synth_mp3(fallback)
                                if fb_mp3 and len(fb_mp3) > 100:
                                    _put_blocking((fallback, fb_mp3))
                                    _fallback_sent = True
                            except Exception:
                                pass
                        _brain_iter = iter(())
                    try:
                        for s, full in _brain_iter:
                            if cancel_evt.is_set():
                                log.info("[xiaozhi] collect: consumer 已取消，停止生成")
                                break
                            if full:
                                conn_interrupted = full
                            if not s:
                                continue
                            if s in ("__MUSIC__", "__MUSIC_STOP__") \
                                    or s.startswith("__MUSIC__"):
                                _put_blocking((s, None))
                                continue
                            mp3 = _synth_mp3(s)
                            if mp3 and len(mp3) > 100:
                                pending_sentences.append(s)
                                _put_blocking((s, mp3))
                            # 首句已产出，后续不再计时
                            _brain_t0 = None
                    except Exception as e:
                        log.error("[xiaozhi] brain error: %s", e)
                        # 往队列放兜底句子，让 consumer 正常走 TTS start→播报→stop
                        if not _fallback_sent:
                            try:
                                fallback = "抱歉，出了点问题，请再说一遍"
                                fb_mp3 = _synth_mp3(fallback)
                                if fb_mp3 and len(fb_mp3) > 100:
                                    _put_blocking((fallback, fb_mp3))
                                    _fallback_sent = True
                                else:
                                    # 无法合成兜底语音：放一个标记让 consumer 发文本提示
                                    _fallback_sent = True
                                    _put_blocking(("__TTS_FAIL__", b""))
                            except Exception:
                                pass
                    finally:
                        # 首句超时兜底：生产者线程内检测，无需消费者侧重复计时
                        if _brain_t0 is not None and (time.monotonic() - _brain_t0) > _FIRST_SENTENCE_TIMEOUT:
                            if not _fallback_sent:
                                try:
                                    fallback = "抱歉，反应慢了，请再说一遍"
                                    fb_mp3 = _synth_mp3(fallback)
                                    if fb_mp3 and len(fb_mp3) > 100:
                                        _put_blocking((fallback, fb_mp3))
                                        _fallback_sent = True
                                except Exception:
                                    pass
                finally:
                    # (None, _SENTINEL) sentinel so consumer's tuple unpack works.
                    try:
                        asyncio.run_coroutine_threadsafe(
                            q.put((None, _SENTINEL)), loop)
                    except (RuntimeError, AttributeError):
                        pass  # loop 已关闭

            producer = loop.run_in_executor(_brain_pool, collect)

# Consume sentences one at a time. The first sentence triggers
            # tts/start + playback right away; subsequent sentences are
            # synthesized and appended to the stream while the brain still
            # produces the rest. This is what makes the first-audio latency
            # track the FIRST sentence, not the ENTIRE reply.
            #
            # Device only sends audio while in speaking state: must send
            # tts start before the packets and tts stop after.
            #
            # CRITICAL: firmware decode queue is capped at
            # MAX_DECODE_PACKETS_IN_QUEUE = 40 packets = 2.4s (audio_service.h)
            # and PushPacketToDecodeQueue(wait=false) silently DROPS any
            # excess (audio_service.cc). Bursting all packets at once cuts
            # any reply longer than 2.4s. So pace each frame at ~real-time
            # cadence (60ms per 60ms frame) so the queue never overflows;
            # a small head-start of a few frames keeps playback glitch-free
            # without exceeding the 40-packet budget.
            frame_s = OPUS_FRAME_DURATION_MS / 1000.0
            sent = 0
            first_send = None
            started = False
            first_sentence_logged = False
            t_reply_start = loop.time()
            t_0 = t_reply_start
            t_brain_first = None
            t_opus = None  # 可能因 sentinel/超时而从未赋值
            try:
                while True:
                    if not first_sentence_logged:
                        try:
                            s, mp3 = await asyncio.wait_for(
                                q.get(), timeout=_FIRST_SENTENCE_TIMEOUT)
                        except asyncio.TimeoutError:
                            log.warning("[xiaozhi] 首句超时(>%.0fs)，视为死链",
                                        _FIRST_SENTENCE_TIMEOUT)
                            cancel_evt.set()
                            if not _fallback_sent:
                                _fallback_sent = True
                                # 直接合成并发送兜底语，避免递归调用 brain（此时 brain 已死链）
                                # 注意：不提前发 tts stop，否则客户端/测试会误认为 TTS 结束而停止收消息
                                try:
                                    _fb = "抱歉，反应慢了，请再说一遍"
                                    _fb_mp3 = await loop.run_in_executor(
                                        None, _synth_mp3, _fb)
                                    if _fb_mp3 and len(_fb_mp3) > 100:
                                        await send_json({"type": "tts", "state": "start"})
                                        await send_json({"type": "tts", "state": "sentence_start", "text": _fb})
                                        _pkts = await loop.run_in_executor(
                                            None, mp3_to_opus_packets, _fb_mp3,
                                            DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS,
                                            cur_volume_db)
                                        for _pkt in _pkts:
                                            if ws.client_state != WebSocketState.CONNECTED:
                                                break
                                            await ws.send_bytes(_pkt)
                                            await asyncio.sleep(0.06)
                                        await send_json({"type": "tts", "state": "stop"})
                                    else:
                                        # TTS 合成失败：发文本状态消息，不要只发 tts stop
                                        await send_json({"type": "stt", "text": "抱歉，语音合成暂时不可用"})
                                except Exception as _e:
                                    log.warning("[xiaozhi] 兜底TTS失败: %s", _e)
                                    try:
                                        await send_json({"type": "stt", "text": "抱歉，语音合成暂时不可用"})
                                    except Exception:
                                        pass
                            rebuild_brain_pool()
                            break
                    else:
                        try:
                            s, mp3 = await asyncio.wait_for(q.get(), timeout=30.0)
                        except asyncio.TimeoutError:
                            log.warning("[xiaozhi] 非首句q.get()超时(>30s)，视为producer死链，停止收尾")
                            cancel_evt.set()
                            rebuild_brain_pool()
                            break
                    if mp3 is _SENTINEL or s is _SENTINEL:
                        break
                    if s == "__TTS_FAIL__":
                        await send_json({"type": "stt", "text": "抱歉，语音合成暂时不可用"})
                        continue
                    if not first_sentence_logged:
                        first_sentence_logged = True
                        t_brain_first = loop.time()
                        log.info("[xiaozhi] latency: brain首句+TTSSynth=%.0fms text=%.60s",
                                 (t_brain_first - t_reply_start) * 1000, s)
                    if s is not None and s.startswith("__MUSIC__"):
                        # __MUSIC__url__name__artist → download the song and play
                        # its audio instead of TTS-ing the URL text.
                        url = s[len("__MUSIC__"):].split("__")[0].strip()
                        label = s[len("__MUSIC__"):].split("__", 1)[1].replace("__", " · ") if "__" in s[len("__MUSIC__"):] else "音乐"
                        if not url.startswith("http"):
                            continue
                        try:
                            pkts = await loop.run_in_executor(
                                None, url_to_opus_packets, url,
                                DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS)
                        except Exception as mx:
                            log.warning("[xiaozhi] music fetch fail: %s", mx)
                            pkts = []
                        if not pkts:
                            continue
                        bump_metric("music_plays")
                        label = f"♪ {label}"
                        is_tts = False
                    elif mp3:
                        pkts = await loop.run_in_executor(
                            None, mp3_to_opus_packets, mp3,
                            DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS,
                            cur_volume_db)
                        if not pkts:
                            continue
                        label = s
                        is_tts = True
                    else:
                        continue
                    if not started:
                        await send_json({"type": "tts", "state": "start"})
                        started = True
                        first_send = loop.time()
                    bump_metric("tts_sentences")
                    await send_json({
                        "type": "tts", "state": "sentence_start", "text": label})
                    t_opus = loop.time()
                    sizes = len(pkts)
                    for pkt in pkts:
                        if ws.client_state != WebSocketState.CONNECTED:
                            break
                        await ws.send_bytes(pkt)
                        sent += 1
                        target = sent - HEAD_START_FRAMES
                        if target > 0:
                            wait = target * frame_s - (loop.time() - first_send)
                            if wait > 0:
                                await asyncio.sleep(wait)
                    t_after_send = loop.time()
                    if is_tts and resume_played < len(pending_sentences):
                        resume_played += 1
                    log.info("[xiaozhi] latency: 句'%s' queue→opus=%.0fms 发送=%d帧/%.2fs (%.1f帧/秒)",
                             label[:50],
                             (t_opus - t_0) * 1000,
                             sizes, t_after_send - t_opus,
                             sizes / (t_after_send - t_opus) if (t_after_send - t_opus) > 0 else 0)
                # trailing buffer: 根据最后一批帧数动态计算，短回复不再等 800ms
                _last_pkt_count = sizes if 'sizes' in dir() and sizes else 0
                _trailing_wait = min(0.8, max(0.15, _last_pkt_count * 0.06 + 0.1))
                _set_xiaozhi_streaming(False)
                await asyncio.sleep(_trailing_wait)
                if started:
                    await send_json({"type": "tts", "state": "stop"})
                # Reply done: re-open the listening window so the user can ask
                # follow-ups without re-saying the wake word (iFLYOS-style
                # continuous conversation). The window expires after the
                # role-appropriate ARM_WINDOW of idle, after which a new wake
                # word is required. Also reset the noise baseline because our
                # own TTS just elevated it and would otherwise suppress the
                # next (quieter) utterance.
                _arm_win = _get_arm_window()
                if started and _arm_win > 0:
                    armed_until = time.monotonic() + _arm_win
                    noise_floor = 0.0
                    log.info("[xiaozhi] reply finished, re-armed for %.0fs follow-up",
                             _arm_win)
                t_tts_end = loop.time()
                t_opus_s = (t_tts_end - t_opus) * 1000 if t_opus is not None else 0
                log.info("[xiaozhi] latency: TTS总=%.0fms brain首句=%.0fms 发送=%d帧/%.2fs",
                         (t_tts_end - t_reply_start) * 1000,
                         (t_brain_first - t_reply_start) * 1000 if t_brain_first else 0,
                         sent, t_opus_s / 1000.0)
            except Exception as e:
                log.warning("[xiaozhi] TTS fail: %s", e)
                bump_metric("tts_failures")
                cancel_evt.set()  # consumer 异常退出，通知 collect 线程停止
            finally:
                _set_xiaozhi_streaming(False)
                try:
                    await asyncio.wait_for(asyncio.shield(producer), timeout=2.0)
                except asyncio.TimeoutError:
                    log.warning("[xiaozhi] producer未在2s内结束，放弃等待")
                except Exception:
                    pass

        async def process_utterance(frames: list):
            nonlocal stream_task
            t0 = time.monotonic()
            if not frames:
                log.info("[xiaozhi] empty utterance, ignored silently")
                return
            bump_metric("utterances_total")
            try:
                wav = opus_decode_to_wav(frames, UPLINK_SAMPLE_RATE)
            except Exception as e:
                log.error("[xiaozhi] decode fail: %s", e)
                return
            t_decode = time.monotonic()
            if not wav or likely_empty_audio(wav):
                log.info("[xiaozhi] empty audio, ignored silently")
                return

            from voice_agent import asr, is_low_intent_asr, is_garbled_asr
            from agent.intent import LOW_INTENT_ASR_REPLY, strip_wake_word
            try:
                asr_text = await asyncio.wait_for(
                    asyncio.to_thread(asr, wav, "wav"), timeout=30)
            except Exception as e:
                log.error("[xiaozhi] ASR fail: %s", e)
                bump_metric("asr_failures")
                # Network/TTS hiccup — say so instead of leaving the user
                # waiting in silence. (Dynamic hint, not cached speech.)
                await cancel_stream()
                stream_task = asyncio.create_task(
                    _safe_stream("语音识别失败了，请再试一次"))
                return
            asr_text = (asr_text or "").strip()
            # Wake words ("你好小智"/"小智"/"charlie") often ride the front of
            # the ASR result; strip them so brain/intent see only the command.
            if asr_text:
                stripped = strip_wake_word(asr_text)
                if stripped:
                    asr_text = stripped
                elif stripped == "" and asr_text:
                    # ASR heard ONLY a wake word & nothing else — stay silent.
                    log.info("[xiaozhi] ASR wake-only, ignored: %r", asr_text)
                    await send_json({"type": "stt", "text": ""})
                    return
            t_asr = time.monotonic()
            log.info("[xiaozhi] latency: 音频=%d帧 decode=%.0fms ASR=%.0fms | text=%s",
                     len(frames),
                     (t_decode - t0) * 1000,
                     (t_asr - t_decode) * 1000,
                     asr_text)
            try:
                from app.audit_log import audit_log
                audit_log("ws:asr", input_data=f"{len(frames)}frames",
                          output_data=asr_text, action="recognize",
                          session_id=f"xiaozhi-{device_key}",
                          duration_ms=(t_asr - t_decode) * 1000)
            except Exception:
                pass
            if not asr_text or is_garbled_asr(asr_text):
                log.info("[xiaozhi] ASR filtered as garbled: %r", asr_text)
                await send_json({"type": "stt", "text": ""})
                return
            # pure filler (嗯/hmm/huh...) → light acknowledgment, no brain round-trip
            if is_low_intent_asr(asr_text):
                log.info("[xiaozhi] ASR low-intent, ack only: %r", asr_text)
                await send_json({"type": "stt", "text": asr_text})
                await cancel_stream()
                stream_task = asyncio.create_task(
                    _safe_stream(LOW_INTENT_ASR_REPLY))
                return
            await send_json({"type": "stt", "text": asr_text})
            await cancel_stream()
            # "继续/接着说/讲下去" after a barge-in: resume the interrupted
            # reply by feeding its partial text back to the brain so the
            # reply continues from where playback was cut off.
            resume_kw = ("继续", "接着说", "接着讲", "接下去", "继续讲",
                         "讲下去", "接着说下去", "继续刚才", "继续说")
            if any(kw in asr_text for kw in resume_kw):
                unplayed = pending_sentences[resume_played:]
                if unplayed:
                    log.info("[xiaozhi] resume %.60s: replay %d cached sentence(s)",
                             asr_text[:30], len(unplayed))
                    bump_metric("resumes_total")
                    stream_task = asyncio.create_task(
                        _safe_stream(asr_text, prebuilt=unplayed))
                    return
            interrupted = conn_interrupted if conn_interrupted else None
            if interrupted and any(kw in asr_text for kw in resume_kw):
                log.info("[xiaozhi] resume-from-interrupt: %r",
                         asr_text[:30])
                stream_task = asyncio.create_task(
                    _safe_stream(asr_text, interrupted_reply=interrupted))
                return
            stream_task = asyncio.create_task(_safe_stream(asr_text))

        async def _safe_stream(text, interrupted_reply: str = "",
                                  prebuilt: Optional[list] = None):
            try:
                await stream_tts_reply(text, interrupted_reply=interrupted_reply,
                                       prebuilt=prebuilt)
            except Exception as e:
                log.error("[xiaozhi] stream_tts_reply error: %s", e, exc_info=True)

        async def _play_wake_beep():
            """Play the short acknowledgement tone right when the KWS fires, so
            the user knows we heard them without waiting for ASR+brain+TTS."""
            beep = _synth_wake_beep()
            if not beep:
                return
            try:
                bkts = await asyncio.get_running_loop().run_in_executor(
                    None, mp3_to_opus_packets, beep,
                    DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS, 0.0)
            except Exception as e:
                log.warning("[xiaozhi] wake beep encode fail: %s", e)
                return
            if not bkts:
                return
            await send_json({"type": "tts", "state": "start"})
            await send_json({"type": "tts", "state": "sentence_start", "text": "叮"})
            for pkt in bkts:
                if ws.client_state != WebSocketState.CONNECTED:
                    break
                await ws.send_bytes(pkt)
            await asyncio.sleep(0.05)
            await send_json({"type": "tts", "state": "stop"})
            log.info("[audio] wake beep played (%d pkts)", len(bkts))

        if opuslib is None:
            log.error("[xiaozhi] opuslib not available, closing connection")
            await ws.close(code=1011)
            return
        decoder = opuslib.Decoder(UPLINK_SAMPLE_RATE, 1)
        _load_silero_vad()  # 预加载 Silero VAD, 避免首帧推理延迟
        tail = deque(maxlen=HEAD_START_FRAMES)  # rolling pre-speech frames
        buf_frames = []
        speech_count = 0
        silence_count = 0
        utterance_active = False
        frame_count = 0
        armed_until = 0.0
        wake_echo_until = 0.0   # wake ack-tone window: don't start a new utterance
        noise_floor = 0.0   # adaptive baseline tracked from quiet periods
        cur_volume_db = 0.0  # TTS loudness boost derived from current noise floor
        hot_frames = 0       # consecutive active frames while not yet utterance

        def update_volume_from_noise():
            nonlocal cur_volume_db
            if noise_floor > 200:
                cur_volume_db = 6.0
            elif noise_floor > 140:
                cur_volume_db = 3.0
            else:
                cur_volume_db = 0.0

        def speech_threshold() -> float:
            """Adaptive endpointer threshold = SIG_RATIO * rolling noise floor."""
            if noise_floor <= 0:
                return SILENCE_RMS_DEFAULT
            return max(SILENCE_RMS_DEFAULT * 0.5, noise_floor * SIG_RATIO)

        def reset_utterance():
            nonlocal buf_frames, speech_count, silence_count, utterance_active, hot_frames
            tail.clear()
            buf_frames = []
            speech_count = 0
            silence_count = 0
            utterance_active = False
            hot_frames = 0

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=120.0)
                    # 标记 ESP32 最近活跃（调度器据此判断是否推语音通知）
                    import app.schedulers as _sc
                    _sc._touch_esp32()
                except asyncio.TimeoutError:
                    # 120s 无数据：发 ping 探活，失败则关闭半开连接
                    log.info("[xiaozhi] 120s 无数据, 发送 ping 探活")
                    try:
                        await send_json({"type": "ping"})
                        msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
                    except (asyncio.TimeoutError, Exception) as pe:
                        log.info("[xiaozhi] ping 探活失败, 关闭半开连接: %s", pe)
                        break
                    if msg["type"] == "websocket.disconnect":
                        break
                if msg["type"] == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    pkt = msg["bytes"]
                    frame_count += 1
                    if frame_count % 100 == 0:
                        log.info("[xiaozhi] received %d audio frames so far", frame_count)
                        # Firmware v2.1 keeps the WS open (and disables its local
                        # KWS) after a conversation until the server says goodbye.
                        # If the role-appropriate follow-up window has expired
                        # with nothing playing and no active utterance, close the
                        # session so the device returns to wake-word listening.
                        # Without this the device streams forever and can never
                        # be re-woken.
                        _arm_win = _get_arm_window()
                        if (_arm_win > 0 and armed_until > 0.0
                                and time.monotonic() > armed_until
                                and not (stream_task and not stream_task.done())
                                and not utterance_active):
                            log.info("[xiaozhi] idle past arm window, sending goodbye "
                                     "so device returns to wake-word mode (role_window=%.0fs)",
                                     _arm_win)
                            try:
                                await send_json({"type": "goodbye"})
                                await ws.close()
                            except Exception:
                                pass
                            break
                    # Decode + energy once per frame (also fills the rolling tail)
                    pcm = b""
                    try:
                        pcm = decoder.decode(pkt, FRAME_SAMPLES)
                        rms = audioop.rms(pcm, 2)
                    except Exception as dex:
                        rms = 0
                        if frame_count % 50 == 0:
                            log.info("[xiaozhi] opus decode err: %s", dex)
                    if frame_count % 300 == 0:
                        log.info("[xiaozhi] debug rms=%d speech=%d silence=%d pktlen=%d active=%s",
                                 rms, speech_count, silence_count, len(pkt), utterance_active)

                    # Skip VAD if decode failed (pcm is empty)
                    if not pcm:
                        continue

                    if not utterance_active:
                        # Not speaking yet: keep a short rolling tail, discard the
                        # rest so pre-speech silence never pollutes the buffer.
                        tail.append(pkt)
                        # Wake cooldown: for the brief window right after the
                        # wake ack-tone, don't treat any loud frame as real
                        # speech (it's the device's own beep + wake-word echo).
                        if time.monotonic() < wake_echo_until:
                            continue
                        # Update the noise baseline from quiet frames (only when
                        # clearly not speech) so the adaptive threshold tracks
                        # room conditions.
                        thr = speech_threshold()
                        if rms < thr * 0.8:
                            if noise_floor <= 0:
                                noise_floor = float(rms)
                            else:
                                noise_floor = 0.92 * noise_floor + 0.08 * rms
                        if _is_speech_vad(pcm, rms, thr):
                            # Debounce: require a few consecutive hot frames so a
                            # single transient (own-TTS echo, door click, music)
                            # doesn't false-trigger a barge-in or start of speech.
                            hot_frames += 1
                            if hot_frames < BARGE_IN_FRAMES:
                                continue
                            # Barge-in: user started talking while we were still
                            # playing TTS — cut the audio so we can listen.
                            if stream_task and not stream_task.done():
                                log.info("[xiaozhi] barge-in, cancelling current TTS")
                                bump_metric("barges_total")
                                await cancel_stream()
                                await send_json({"type": "tts", "state": "stop"})
                            utterance_active = True
                            buf_frames = list(tail)
                            speech_count = 1
                            silence_count = 0
                            _hot_at_start = hot_frames
                            hot_frames = 0
                            log.info("[xiaozhi] speech start (%d tail frames carried, thr=%.0f, hot=%d)",
                                     len(buf_frames), thr, _hot_at_start)
                        else:
                            hot_frames = 0
                        continue

                    # utterance_active: collect everything until silence.
                    buf_frames.append(pkt)
                    thr = speech_threshold()
                    if _is_speech_vad(pcm, rms, thr):
                        speech_count += 1
                        silence_count = 0
                    else:
                        silence_count += 1
                    # VAD 可用时静音确认更短(0.48s vs RMS 1.2s), 砍尾静音延迟
                    silence_limit = SILENCE_FRAMES_VAD if _silero_vad is not None else SILENCE_FRAMES
                    capped = len(buf_frames) >= MAX_UTTERANCE_FRAMES
                    # Background-noise guard: if a long time passes without ever
                    # reaching enough real speech, silently drop the buffer rather
                    # than waiting out the full 36s cap (avoids wasteful buffering
                    # and repeated "didn't hear you" hints on ambient noise).
                    noise_timeout = len(buf_frames) >= NOISE_DROP_FRAMES \
                        and speech_count < MIN_SPEECH_FRAMES
                    if ((speech_count >= MIN_SPEECH_FRAMES
                            and silence_count >= silence_limit)
                            or capped
                            or noise_timeout):
                        had_speech = speech_count >= MIN_SPEECH_FRAMES
                        frames = list(buf_frames)
                        reset_utterance()
                        update_volume_from_noise()
                        log.info("[xiaozhi] endpoint: %d frames (capped=%s noise_drop=%s, vol=%.1f dB)",
                                 len(frames), capped, noise_timeout, cur_volume_db)
                        if not had_speech:
                            # No real speech captured (ambient noise / device's
                            # own echo). Stay silent — don't nag the user.
                            log.info("[xiaozhi] no clear speech, ignoring silently")
                            continue
                        _arm_win = _get_arm_window()
                        if _arm_win > 0 and time.monotonic() > armed_until:
                            # Heard real speech but outside the wake window —
                            # ambient conversation/TV. Don't talk on our own.
                            log.info("[xiaozhi] outside arm window, ignoring (%.1fs)",
                                     time.monotonic() - armed_until)
                            continue
                        # Heard speech inside the wake window: this is a real
                        # command. Refresh the window so a natural back-and-forth
                        # (user replies to our answer without re-waking) stays hot.
                        if _arm_win > 0:
                            armed_until = time.monotonic() + _arm_win
                        asyncio.create_task(process_utterance(frames))
                    continue

                text = msg.get("text")
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type", "")
                state = data.get("state", "")
                log.info("[xiaozhi] recv %s/%s", mtype, state)

                if mtype == "hello":
                    for dev_key in ("client_id", "device_id", "device"):
                        cand = data.get(dev_key)
                        if isinstance(cand, dict):
                            cand = (cand.get("device_id") or cand.get("mac")
                                    or cand.get("client_id") or cand.get("name"))
                        if cand:
                            device_key = re.sub(r"[^0-9A-Za-z_-]", "", str(cand))[:32] or device_key
                            break
                    await send_json({
                        "type": "hello",
                        "id": session_id,
                        "session_id": session_id,
                        "transport": "websocket",
                        "protocol_version": "1",
                        "audio_params": {
                            "format": "opus",
                            "sample_rate": DOWNLINK_SAMPLE_RATE,
                            "channels": 1,
                            "frame_duration": OPUS_FRAME_DURATION_MS,
                        },
                    })
                    log.info("[xiaozhi] hello done, session=%s device=%s", session_id, device_key)

                elif mtype == "listen":
                    if state == "detect":
                        log.info("[xiaozhi] wake: %s", data.get("text", ""))
                        await cancel_stream()
                        reset_utterance()
                        _arm_win = _get_arm_window()
                        if _arm_win > 0:
                            armed_until = time.monotonic() + _arm_win
                        # wake feedback: instant ack + short echo guard so the
                        # beep/own-KWS-echo isn't mistaken for a fresh command.
                        wake_echo_until = time.monotonic() + WAKE_ECHO_COOLDOWN
                        if _synth_wake_beep():
                            stream_task = asyncio.create_task(_play_wake_beep())
                    elif state == "start":
                        reset_utterance()
                        _arm_win = _get_arm_window()
                        if _arm_win > 0:
                            armed_until = time.monotonic() + _arm_win
                        log.info("[xiaozhi] listening start")
                    elif state == "stop":
                        frames = list(buf_frames)
                        had_speech = speech_count >= MIN_SPEECH_FRAMES
                        reset_utterance()
                        log.info("[xiaozhi] listen stop, %d frames", len(frames))
                        if not frames:
                            log.info("[xiaozhi] listen stop, no frames, ignored")
                            await cancel_stream()
                        elif not had_speech and len(frames) < MIN_SPEECH_FRAMES:
                            # Device stopped listening with nothing meaningful —
                            # stay silent rather than replying.
                            log.info("[xiaozhi] listen stop, silence, ignored")
                            await cancel_stream()
                        else:
                            _arm_win = _get_arm_window()
                            if _arm_win > 0 and time.monotonic() > armed_until:
                                log.info("[xiaozhi] listen stop outside arm window, ignored")
                                await cancel_stream()
                            else:
                                if _arm_win > 0:
                                    # Valid in-window utterance: keep the hot window.
                                    armed_until = time.monotonic() + _arm_win
                                asyncio.create_task(process_utterance(frames))

                elif mtype == "abort":
                    await cancel_stream()
                    reset_utterance()
                    await send_json({"type": "tts", "state": "stop"})
                elif mtype == "ping":
                    await send_json({"type": "pong"})

        except WebSocketDisconnect:
            log.info("[xiaozhi] disconnected")
        except Exception as e:
            log.error("[xiaozhi] error: %s", e, exc_info=True)
        finally:
            bump_metric("ws_active", -1)
            unregister_xiaozhi_client(session_id)
            try:
                await send_json({"type": "tts", "state": "stop"})
            except Exception:
                pass
            try:
                await send_json({"type": "goodbye"})
            except Exception:
                pass
            await cancel_stream()
            log.info("[xiaozhi] session %s cleaned up", session_id)
