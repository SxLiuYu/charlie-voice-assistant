"""Xiaozhi-compatible WebSocket endpoint (v2.1.0 firmware compatible).

The v2.1.0 firmware streams continuous 60ms Opus frames after listen start and
does NOT send listen stop reliably in auto mode. The server performs endpointing:
it decodes frames, measures energy, and when ~1.2s of silence follows speech,
it processes the utterance (ASR→Brain→TTS) and streams back Opus audio.
"""

import asyncio
import base64
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
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.audio import likely_empty_audio
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
SILENCE_FRAMES_VAD = int(os.getenv("XIAOZHI_VAD_SILENCE_FRAMES", "8"))  # 8*60ms=0.48s
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
NOISE_DROP_FRAMES = 200   # ~12s
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
ARM_WINDOW = 30.0  # 对话连续窗口（主动推送由MQTT信令触发，不绑架此值）
# After a wake (listen/detect) the device's own speaker beep + the wake word
# itself can momentarily re-trigger our endpointing. Ignore fresh hot frames
# for this long so the acknowledgement tone and echo settle before the mic
# starts a new utterance. (KWS echo protection / wake cooldown, see below.)
# Must stay short (~beep length + a little reverb) so a user who speaks right
# after the wake word isn't dropped.
WAKE_ECHO_COOLDOWN = 0.5

_prewarm_started = False
_prewarm_lock = threading.Lock()

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


def _synth_mp3(sentence: str) -> bytes:
    """TTS via Finna (SSE streaming) → MP3. Falls back to Baidu.

    Cached in the shared TTS mp3 cache so repeated hints / replies skip the
    network round-trip entirely (seconds → instant). Cache writes happen
    here so prewarm and live replies share one path.
    """
    from agent.asr_tts import _tts_cache_get, _tts_cache_put, TTS_VOICE as _TTS_VOICE
    cached = _tts_cache_get(sentence)
    if cached:
        return cached
    api_key = os.getenv("FINNA_API_KEY", "")
    if api_key:
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
                    "voice": _TTS_VOICE or "Ethan",
                    "response_format": "mp3",
                    "speed": float(os.getenv("TTS_SPEED", "1.0")),
                    "stream": True,
                },
                timeout=20,
                stream=True,
            )
            resp.raise_for_status()
            wav_parts = []
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
            if wav_parts:
                wav = b"".join(wav_parts)
                # convert WAV to MP3 for opus encoder
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k",
                     "-ac", "1", "-f", "mp3", "pipe:1"],
                    input=wav, capture_output=True, timeout=15)
                if r.stdout and len(r.stdout) > 100:
                    _tts_cache_put(sentence, r.stdout)
                    return r.stdout
                _tts_cache_put(sentence, wav)
                return wav
        except Exception as e:
            log.warning("[xiaozhi] finna TTS fail: %s", e)
    # Fallback: Baidu
    try:
        from agent.asr_tts import _tts_baidu
        raw = _tts_baidu(sentence)
        if raw and len(raw) > 100:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", "pipe:0", "-b:a", "32k",
                 "-ac", "1", "-f", "mp3", "pipe:1"],
                input=raw, capture_output=True, timeout=10)
            if r.stdout and len(r.stdout) > 100:
                _tts_cache_put(sentence, r.stdout)
                return r.stdout
            _tts_cache_put(sentence, raw)
            return raw
    except Exception as e:
        log.warning("[xiaozhi] baidu TTS fail: %s", e)
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
                _tts_cache_put(sentence, r.stdout)
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
                        await ws.send_text(json.dumps({"type": "tts", "state": "stop"}))
                        log.info(f"[xiaozhi] 补发推送: {item['text'][:30]}")
                    except Exception as e:
                        log.warning(f"[xiaozhi] 补发失败: {e}")
            asyncio.ensure_future(_flush(), loop=_loop)

        async def cancel_stream():
            nonlocal stream_task
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
            nonlocal conn_interrupted, resume_played, cur_volume_db, armed_until, noise_floor

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

            def _put_blocking(item):
                # The consumer (pacing) is the bottleneck; block here so a long
                # reply never overflows the queue. This only stretches the
                # producer thread, never loses a sentence. Bail early if the
                # owning loop shuts down (WS disconnect / barge-in) instead of
                # spinning until a QueueFull forever.
                while True:
                    if not loop.is_running() or not ws.client_state == WebSocketState.CONNECTED:
                        return
                    try:
                        q.put_nowait(item)
                        return
                    except asyncio.QueueFull:
                        time.sleep(0.02)

            def collect():
                nonlocal conn_interrupted, pending_sentences
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
                            mp3 = _synth_mp3(s)
                            if mp3 and len(mp3) > 100:
                                pending_sentences.append(s)
                                _put_blocking((s, mp3))
                        return
                    for s, full in brain_stream_sentences(
                        text, session_id="xiaozhi-" + device_key,
                        interrupted_reply=interrupted_reply,
                    ):
                        if full:
                            conn_interrupted = full
                        if not s:
                            continue
                        if s in ("__MUSIC__", "__MUSIC_STOP__") \
                                or s.startswith("__MUSIC__"):
                            # Music markers carry a URL, not TTS text — hand
                            # straight through without synthesis. Playback
                            # keeps the tail-addressed order via the same queue.
                            _put_blocking((s, None))
                            continue
                        mp3 = _synth_mp3(s)
                        if mp3 and len(mp3) > 100:
                            pending_sentences.append(s)
                            _put_blocking((s, mp3))
                except Exception as e:
                    log.error("[xiaozhi] brain error: %s", e)
                finally:
                    # (None, _SENTINEL) sentinel so consumer's tuple unpack works.
                    asyncio.run_coroutine_threadsafe(
                        q.put((None, _SENTINEL)), loop)

            producer = loop.run_in_executor(None, collect)

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
            try:
                while True:
                    s, mp3 = await q.get()
                    if mp3 is _SENTINEL or s is _SENTINEL:
                        break
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
                # trailing buffer so the last frames actually play out
                await asyncio.sleep(0.8)
                if started:
                    await send_json({"type": "tts", "state": "stop"})
                # Reply done: re-open the listening window so the user can ask
                # follow-ups without re-saying the wake word (iFLYOS-style
                # continuous conversation). The window expires after ARM_WINDOW
                # of idle, after which a new wake word is required. Also reset
                # the noise baseline because our own TTS just elevated it and
                # would otherwise suppress the next (quieter) utterance.
                if started and ARM_WINDOW > 0:
                    armed_until = time.monotonic() + ARM_WINDOW
                    noise_floor = 0.0
                    log.info("[xiaozhi] reply finished, re-armed for %.0fs follow-up",
                             ARM_WINDOW)
                t_tts_end = loop.time()
                log.info("[xiaozhi] latency: TTS总=%.0fms brain首句=%.0fms 发送=%d帧/%.2fs",
                         (t_tts_end - t_reply_start) * 1000,
                         (t_brain_first - t_reply_start) * 1000 if t_brain_first else 0,
                         sent, t_tts_end - t_opus)
            except Exception as e:
                log.warning("[xiaozhi] TTS fail: %s", e)
                bump_metric("tts_failures")
            finally:
                await producer

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
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    pkt = msg["bytes"]
                    frame_count += 1
                    if frame_count % 100 == 0:
                        log.info("[xiaozhi] received %d audio frames so far", frame_count)
                        # Firmware v2.1 keeps the WS open (and disables its local
                        # KWS) after a conversation until the server says goodbye.
                        # If the 30s follow-up window has expired with nothing
                        # playing and no active utterance, close the session so
                        # the device returns to wake-word listening. Without this
                        # the device streams forever and can never be re-woken.
                        if (ARM_WINDOW > 0 and armed_until > 0.0
                                and time.monotonic() > armed_until
                                and not (stream_task and not stream_task.done())
                                and not utterance_active):
                            log.info("[xiaozhi] idle past arm window, sending goodbye "
                                     "so device returns to wake-word mode")
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
                        if ARM_WINDOW > 0 and time.monotonic() > armed_until:
                            # Heard real speech but outside the wake window —
                            # ambient conversation/TV. Don't talk on our own.
                            log.info("[xiaozhi] outside arm window, ignoring (%.1fs)",
                                     time.monotonic() - armed_until)
                            continue
                        # Heard speech inside the wake window: this is a real
                        # command. Refresh the window so a natural back-and-forth
                        # (user replies to our answer without re-waking) stays hot.
                        if ARM_WINDOW > 0:
                            armed_until = time.monotonic() + ARM_WINDOW
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
                        if ARM_WINDOW > 0:
                            armed_until = time.monotonic() + ARM_WINDOW
                        # wake feedback: instant ack + short echo guard so the
                        # beep/own-KWS-echo isn't mistaken for a fresh command.
                        wake_echo_until = time.monotonic() + WAKE_ECHO_COOLDOWN
                        if _synth_wake_beep():
                            stream_task = asyncio.create_task(_play_wake_beep())
                    elif state == "start":
                        reset_utterance()
                        if ARM_WINDOW > 0:
                            armed_until = time.monotonic() + ARM_WINDOW
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
                        elif ARM_WINDOW > 0 and time.monotonic() > armed_until:
                            log.info("[xiaozhi] listen stop outside arm window, ignored")
                            await cancel_stream()
                        else:
                            if ARM_WINDOW > 0:
                                # Valid in-window utterance: keep the hot window.
                                armed_until = time.monotonic() + ARM_WINDOW
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
            await cancel_stream()
            log.info("[xiaozhi] session %s cleaned up", session_id)
