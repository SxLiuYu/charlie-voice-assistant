"""Xiaozhi-compatible WebSocket endpoint (v2.1.0 firmware compatible).

The v2.1.0 firmware streams continuous 60ms Opus frames after listen start and
does NOT send listen stop reliably in auto mode. The server performs endpointing:
it decodes frames, measures energy, and when ~1.2s of silence follows speech,
it processes the utterance (ASR→Brain→TTS) and streams back Opus audio.
"""

import asyncio
import base64
import contextvars
import datetime
import functools
import hmac
import json
import logging
import os
import re
import struct
import threading
import subprocess
import time
import uuid
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.audio import likely_empty_audio
from app.auth import AUTH_TOKEN, XIAOZHI_DEVICE_TOKEN
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
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
OPUS_FRAME_DURATION_MS = 60
HEAD_START_FRAMES = 4
FRAME_SAMPLES = UPLINK_SAMPLE_RATE * OPUS_FRAME_DURATION_MS // 1000  # 960

# Endpointing: if energy below the (adaptive) speech threshold for
# SILENCE_FRAMES consecutive frames after at least SPEECH_FRAMES of audio,
# consider utterance complete. Speech RMS ~200-300, noise ~20-60.
SILENCE_FRAMES = 20   # 20 * 60ms = 1.2s (RMS fallback when Silero unavailable)
# Silero VAD (神经网络语音检测, 比RMS能量阈值准→静音确认可更短, 砍尾静音延迟)
SILENCE_FRAMES_VAD = int(os.getenv("XIAOZHI_VAD_SILENCE_FRAMES", "6"))  # 6*60ms=0.36s
VAD_THRESHOLD = float(os.getenv("XIAOZHI_VAD_THRESHOLD", "0.35"))  # Silero 语音概率阈值
# LC-S3 麦克风实测：用户说话 Silero 概率常在 0.3~0.5 抖动，0.5 会整句漏检
# （11:25 rms=139 仍未触发 speech start）。0.35 + 高能量兜底。
VAD_ENABLED = os.getenv("XIAOZHI_VAD_ENABLED", "1") == "1"  # 0 强制 RMS 降级
# Min hot frames for a valid utterance. Kept low (12) so short commands like
# "几点"/"开灯" (~0.7s) still endpoint; ambient noise is filtered separately
# by NOISE_DROP_FRAMES below.
MIN_SPEECH_FRAMES = 4   # LC-S3 远场语音 Silero 常只有 4-8 帧过阈，12 会整句丢
MAX_UTTERANCE_FRAMES = 600   # ~36s hard cap before forced endpoint
BARGE_IN_FRAMES = 3   # consecutive hot frames needed to trigger barge-in/speech
# 一次唤醒后最多连续自动应答几轮。超出即停止续窗，必须重新喊唤醒词。
# 电视/环境人声会被 VAD 当成连续提问，实测 20s 内触发 8 轮全量 LLM + 播报。
AUTO_TURN_LIMIT = int(os.getenv("XIAOZHI_AUTO_TURN_LIMIT", "30"))
# 单次回复最多播多少帧（60ms/帧）。100 帧 ≈ 6s；超出部分留给"继续"。
MAX_REPLY_FRAMES = int(os.getenv("XIAOZHI_MAX_REPLY_FRAMES", "100"))
# 语音首句字符上限：nmcloud 实测 ≈0.18s/字（16k 64kbps），
# 22 字 ≈ 4.0s 播放，正好卡在"用户开始以为卡住"之前。
_MAX_VOICE_CHARS = int(os.getenv("XIAOZHI_MAX_VOICE_CHARS", "18"))


_VOICE_PREFIX_RE = re.compile(
    r"^(?:Sir|sir|先生|好的先生)\s*[，,]?\s*"
    r"|^(?:随时(?:待命|为您效劳|听候吩咐)|有何吩咐|听候吩咐|请指示)\s*[。！？]?\s*")
_VOICE_TAIL_RE = re.compile(
    r"(?:需要我.{0,12}[吗么]?[？?。]?"
    r"|有什么(?:可以|能|需要)[^。！？]{0,10}[？?。]?"
    r"|随时(?:待命|为您效劳|听候吩咐)[。！]?"
    r"|为您效劳[。！]?"
    r"|有何吩咐[？?。]?"
    r"|听候吩咐[。！]?"
    r"|请指示[。！]?"
    r")+$")
# 工具进度提示（"正在处理.../正在查新闻..."）。可以播，给长工具一个反馈，
# 但不占用语音单句名额——否则真答案全被丢掉（实测提醒/新闻/菜谱/音量
# 只听到"正在处理..."就停了）。
_TOOL_PROGRESS_RE = re.compile(r"^正在[\u4e00-\u9fff]{1,8}\.{3}$")
# 纯应答/前缀句（"Sir，""好的，Sir。""嗯。"）。播出来浪费 1s，还会把
# 单句名额吃掉，导致后面的真答案被丢（实测翻译只听到"Sir，"）。
_ACK_ONLY_RE = re.compile(
    r"^(?:Sir|sir|先生|好的先生|好的|嗯|好|是的|当然|没问题|明白了|收到|在)"
    r"[，,。！!？?～~\s]*$")


def _shorten_voice(text: str, limit: int) -> str:
    """去掉客套前后缀后再限长：比硬截更自然。

    JARVIS 人格会稳定输出 "Sir，…，您的智能助手。需要我做什么吗？"，
    剥掉 "Sir，" 和结尾客套句后往往就落进限制内，不用切句子。
    """
    t = (text or "").strip()
    _orig = t
    # Sir/先生 前缀无论长短都剥（"Sir，我在" 这种 6 字句也播了一秒洋腔）
    for _ in range(3):
        _new = _VOICE_PREFIX_RE.sub("", t).strip()
        if _new == t or not _new:
            break
        t = _new
    if len(t) <= limit:
        # 剥空/只剩标点就回退原句
        if not t or len(t.rstrip("，。！？、,.!? ")) < 2:
            return _orig
        return t
    # 结尾客套句可能叠两句（"随时待命。有何吩咐？"），循环剥
    for _ in range(3):
        _new = _VOICE_TAIL_RE.sub("", t).strip()
        if _new == t or not _new:
            break
        t = _new
    # 剥掉的都是客套时可能只剩残片（"Sir，"、"J.A.R.V.I.S.。"），
    # 统一在剥完后校验一次：必须还剩一句像样的中文，否则回退原句
    _core = t.rstrip("，。！？、,;；…~～ ")
    if len(_core) < 4 or not any("\u4e00" <= c <= "\u9fff" for c in _core):
        t = _orig
    return _truncate_voice(t, limit)


def _truncate_voice(text: str, limit: int) -> str:
    """把超长首句截断到 limit 字以内，尽量在自然边界切。

    优先级：逗号/顿号/分号 > 空格 > 硬截。切点落在英文缩写内部
    （"J.A.R.V.I.S." 被切成 "J.A.R.V"）会念得莫名其妙，所以硬截时
    往前退到最近的非 ASCII 字符之后。
    """
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    # 切点不早于 limit 的 60%（"Sir，我是。"只切 7 字太碎，念出来更怪）
    _min_cut = max(6, int(limit * 0.6))
    # 1) 自然标点
    for i in range(len(head) - 1, _min_cut - 1, -1):
        if head[i] in "，、；,;":
            return head[:i] + "。"
    # 2) 空格边界
    for i in range(len(head) - 1, _min_cut - 1, -1):
        if head[i] == " ":
            return head[:i].rstrip() + "。"
    # 3) 硬截前避开英文缩写/数字内部
    cut = limit
    while cut > 6 and (head[cut - 1] in ".·" or head[cut - 1].isascii()
                       and head[cut - 1].isalnum()):
        cut -= 1
    return head[:cut].rstrip("，、；,; .") + "。"
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
# Hot beep plays ~3 packets × 60ms = ~180ms; 0.25s cooldown gives ~70ms
# headroom for the acknowledgement tone and wake-word echo to settle before
# the mic starts a fresh utterance. Previously 0.5s dropped first words from
# fast speakers (269-320ms after wake).
WAKE_ECHO_COOLDOWN = 0.25
_FIRST_SENTENCE_TIMEOUT = float(os.getenv("XIAOZHI_FIRST_SENTENCE_TIMEOUT", "12.0"))


def _pcm_rms(data: bytes) -> int:
    """替代 audioop.rms (Python 3.13 弃用)。"""
    if len(data) < 2:
        return 0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    return int((sum(s * s for s in samples) / n) ** 0.5)


def _next_reminder_in() -> float | None:
    """最近一条未完成提醒还有多少秒响；没有返回 None。

    只看 0–900s 内的（天猫闹钟保持长连接的窗口）。更远的到点前再续；
    超过 1 小时的陈年过期项不挡 goodbye。
    """
    try:
        import datetime as _dt
        from app.reminders import _load_reminders
        now = _dt.datetime.now()
        best: float | None = None
        for r in _load_reminders():
            if r.get("done"):
                continue
            due = r.get("due") or ""
            if not due:
                continue
            try:
                d = _dt.datetime.fromisoformat(str(due))
            except Exception:
                continue
            delta = (d - now).total_seconds()
            if delta < -3600:
                continue
            if delta < 0:
                delta = 0.0
            if delta > 900:
                continue
            if best is None or delta < best:
                best = delta
        return best
    except Exception:
        return None


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
# Silero VAD torch 前向专用小池：WS 收包循环每帧(~17fps)调一次，
# 同步跑在事件循环里会占住主循环、拖慢同进程所有 WS/HTTP 连接
_vad_pool = ThreadPoolExecutor(max_workers=int(os.getenv("VAD_POOL_SIZE", "2")), thread_name_prefix="vad")
_dead_link_count = 0
_rebuild_last_time = 0.0
_REBUILD_MIN_INTERVAL = 10.0
_tts_streaming = False
_tts_streaming_lock = threading.Lock()
_rebuild_lock = threading.Lock()

# session metrics (module-level, thread-safe counters)
_xiaozhi_metrics = {
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
_xiaozhi_metrics_lock = threading.Lock()


def bump_metric(name: str, delta: int = 1):
    """Increment a session metric counter (thread-safe)."""
    with _xiaozhi_metrics_lock:
        _xiaozhi_metrics[name] = _xiaozhi_metrics.get(name, 0) + delta


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
_silero_failed = False  # import 失败后置 True，热路径(每音频帧)不再重复尝试
_silero_lock = threading.Lock()

def _load_silero_vad():
    """加载 Silero VAD 模型(懒加载, 失败返回 None→端点降级 RMS)"""
    global _silero_vad, _silero_failed
    if _silero_vad is not None or _silero_failed:
        return _silero_vad
    if not VAD_ENABLED:
        return None
    with _silero_lock:
        if _silero_vad is not None or _silero_failed:
            return _silero_vad
        try:
            from silero_vad import load_silero_vad
            _silero_vad = load_silero_vad()
            log.info("[xiaozhi] Silero VAD loaded (端点检测: 0.48s vs RMS 1.2s)")
        except Exception as e:
            log.warning("[xiaozhi] Silero VAD load fail, fallback to RMS endpointing: %s", e)
            _silero_vad = None
            _silero_failed = True  # 只警告一次；装包后重启进程重试
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
    """语音判定: VAD 优先(概率>=阈值), 高 RMS 兜底, 不可用降级 RMS(>=阈值)

    LC-S3 实测 Silero 对远场/低码率 opus 语音概率偏低，单靠 0.35 仍会漏。
    rms >= thr*1.4 视为明显有声（正常说话 rms 100+，噪音地板 ~20-60）。
    """
    prob = _vad_speech_prob(pcm_bytes)
    if prob >= 0:
        if prob >= VAD_THRESHOLD:
            return True
        # 信任 Silero：LC-S3 房间噪音地板实测 rms 140-160 且 vad=0.00，
        # rms>=80 的宽松兜底会把连续噪音当语音（36s cap + barge-in 风暴）。
        # 干净语音 Silero p=1.000；高能量只留给近场大声（rms>=250）。
        return rms >= 250
    # 降级 RMS
    return rms >= rms_threshold


async def _is_speech_vad_async(pcm_bytes: bytes, rms: int, rms_threshold: float) -> bool:
    """_is_speech_vad 的异步版：torch 前向挪到 _vad_pool，不阻塞事件循环"""
    return await asyncio.get_running_loop().run_in_executor(
        _vad_pool, _is_speech_vad, pcm_bytes, rms, rms_threshold)


async def _ctx_run_in_executor(loop, executor, fn, *args):
    """`loop.run_in_executor` 的上下文传播版（FR-14）。

    裸 `run_in_executor` **不继承** ContextVar（见 `agent/user_context.py`
    的模块 docstring），线程池里的 `resolve_user()` 会掉回 env/default，
    表现为"妈妈听到老于的音色"。

    每次调用独立 `copy_context()`：两个用户并发时各拷一份，
    互不污染（`asyncio.gather` 并发场景已验证）。
    """
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(executor, functools.partial(ctx.run, fn, *args))

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


def _synth_mp3(sentence: str, *, _cache_get=None, _cache_put=None, cancel_evt=None) -> bytes:
    """TTS via Baidu → Finna (SSE streaming) → StepFun → say. Falls back gracefully.

    Cached in the shared TTS mp3 cache so repeated hints / replies skip the
    network round-trip entirely (seconds → instant). Cache writes happen
    here so prewarm and live replies share one path.

    _cache_get/_cache_put are injection points for tests to verify cache keys
    include the role voice (avoiding cross-role cache pollution).

    cancel_evt: optional threading.Event; when set, aborts synthesis immediately
    and returns b"" without invoking any network TTS backend.
    """
    if cancel_evt is not None and cancel_evt.is_set():
        return b""
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
    if cancel_evt is not None and cancel_evt.is_set():
        return b""
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
    if cancel_evt is not None and cancel_evt.is_set():
        return b""

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
    if cancel_evt is not None and cancel_evt.is_set():
        return b""

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
    if cancel_evt is not None and cancel_evt.is_set():
        return b""

    # nanoMuse 云 TTS（TTS_PRIORITY=nmcloud，与 /api/tts 同源）。
    # 上面百度/StepFun 未配 key 时这里兜底，避免 Windows 上直接落到不存在的 `say`。
    try:
        from agent.asr_tts import _tts_nmcloud
        raw = _tts_nmcloud(sentence)
        if raw and len(raw) > 100:
            if _cache_put is not None:
                _cp(sentence, raw, _role_voice, _TTS_MODEL)
            else:
                _cp(sentence, raw, _role_voice, _TTS_MODEL, speed=_role_speed)
            return raw
    except Exception as e:
        log.warning("[xiaozhi] nmcloud TTS fail: %s", e)
    if cancel_evt is not None and cancel_evt.is_set():
        return b""

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


def _decode_check_asr(frames: list) -> tuple:
    """Decode opus frames → wav, check for silence, run ASR race.

    All three steps run in a single thread call to eliminate the ~590ms
    overhead of three separate asyncio.to_thread round-trips.

    Returns:
        (None, "", "")      – empty / silent audio (caller should return)
        (wav, text, provider, duration_ms) – normal result with ASR text,
            provider name, and measured processing duration in ms.
    """
    t_start = time.perf_counter()
    t0 = time.perf_counter()
    wav = opus_decode_to_wav(frames, UPLINK_SAMPLE_RATE)
    decode_ms = (time.perf_counter() - t0) * 1000

    if not wav or likely_empty_audio(wav):
        log.info("[xiaozhi] latency: decode=%.0fms empty/silent → skip ASR", decode_ms)
        return None, "", "", 0.0

    from voice_agent import asr_race
    t1 = time.perf_counter()
    try:
        text, provider = asr_race(wav, "wav")
    except Exception as e:
        log.warning("[xiaozhi] latency: decode=%.0fms ASR error: %s", decode_ms, e)
        raise
    asr_ms = (time.perf_counter() - t1) * 1000
    total_ms = (time.perf_counter() - t_start) * 1000
    log.info("[xiaozhi] latency: decode=%.0fms ASR=%.0fms total=%.0fms provider=%s",
             decode_ms, asr_ms, total_ms, provider)
    return wav, text, provider, total_ms


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
            for phrase in ("你好，有什么需要帮忙的", LOW_INTENT_ASR_REPLY,
                           "嗯？", "没听清，再说一遍？"):
                try:
                    mp3 = _synth_mp3(phrase)
                    if mp3:
                        log.info("[xiaozhi] prewarmed TTS: %.30s", phrase)
                except Exception as e:
                    log.warning("[xiaozhi] prewarm %r fail: %s", phrase, e)
            # 唤醒反馈音也预热：首次唤醒若在事件循环里同步跑 ffmpeg（上限10s）
            # 会冻结整个服务
            _synth_wake_beep()
            # 预热 beep 的 opus 包（否则每次重启后的第一次唤醒要现场跑
            # ffmpeg 转码 ~400ms，之后才命中 opus 缓存降到 ~50ms）
            try:
                _beep = _synth_wake_beep()
                if _beep:
                    mp3_to_opus_packets(_beep, DOWNLINK_SAMPLE_RATE,
                                        OPUS_FRAME_DURATION_MS, 0.0)
                    log.info("[xiaozhi] wake beep opus 预热完成")
            except Exception as _be:
                log.debug(f"[xiaozhi] wake beep opus 预热失败(不影响功能): {_be}")
            # 罐头应答音预热（空回复/音乐失败兜底用，进 TTS 缓存后零延迟）
            for _canned in ("嗯？", "抱歉，音乐加载失败"):
                try:
                    _synth_mp3(_canned)
                except Exception as _ce:
                    log.debug(f"[xiaozhi] 罐头音预热失败({_canned}): {_ce}")
            # Silero VAD 预加载：首帧推理否则 ~1.4s 延迟（torch 懒加载）
            try:
                _load_silero_vad()
                log.info("[xiaozhi] Silero VAD 预热完成")
            except Exception as _vad:
                log.debug(f"[xiaozhi] Silero VAD 预热失败(不影响功能): {_vad}")
            # SenseVoice 预加载：否则第一句 ASR 落到云端 400-900ms
            # （本地 200-300ms，但懒加载首次 ~1.3s）
            try:
                from agent.asr_tts import _load_sense_voice
                _load_sense_voice()
                log.info("[xiaozhi] SenseVoice 预热完成")
            except Exception as _sv:
                log.debug(f"[xiaozhi] SenseVoice 预热失败(不影响功能): {_sv}")
            # 天气预热：Open-Meteo 首查 geocode+forecast ~2.1s，缓存 TTL 600s
            _prewarm_city = "北京"
            try:
                from app.geo import locate
                _loc = locate()
                if _loc and _loc.get("city"):
                    _prewarm_city = _loc["city"]
            except Exception:
                pass
            try:
                from app.weather import get_weather_text
                get_weather_text(_prewarm_city)
                log.info("[xiaozhi] 天气预热完成: %s", _prewarm_city)
            except Exception as _we:
                log.debug(f"[xiaozhi] 天气预热失败(不影响功能): {_we}")
            # 闲聊通道预热：SKIP_BACKGROUND=1 时 _warmup_brain() 不跑，
            # 语音闲聊首句要多等 ~0.7s 的 TLS 握手 + 首包。这里用一句
            # 不会命中缓存的短句打一次流，把连接和模型都拉热。
            try:
                from agent.llm import _chat_lite_stream
                _t_lite = time.time()
                _got = False
                for _s, _f in _chat_lite_stream("在吗", session_id="_prewarm_lite",
                                                channel="text"):
                    _got = True
                    break
                if _got:
                    log.info("[xiaozhi] 闲聊通道预热完成 (%.2fs)", time.time() - _t_lite)
                else:
                    log.debug("[xiaozhi] 闲聊通道预热无输出")
            except Exception as _le:
                log.debug(f"[xiaozhi] 闲聊通道预热失败(不影响功能): {_le}")
            # 当前分钟的时间 TTS 预热：FastPath 秒出文本，但 "现在X点X分。"
            # 每分钟都是新句子，不预热首问要多等一次 nmcloud (~200-500ms)
            try:
                _now0 = datetime.datetime.now()
                _synth_mp3(f"现在{_now0.strftime('%H点%M分')}。")
                log.info("[xiaozhi] 时间TTS预热完成")
            except Exception as _te:
                log.debug(f"[xiaozhi] 时间TTS预热失败(不影响功能): {_te}")
            # 自定义意图端点预热：本地 jev 首次推理常有加载延迟
            if os.getenv("INTENT_BASE", "").strip() and os.getenv("INTENT_MODEL", "").strip():
                def _prewarm_intent():
                    # _classify_intent 失败时静默 return "none"，不会抛异常，
                    # 所以要看失败计数，否则冷启超时也会被当成"预热完成"。
                    from agent.llm import _classify_intent
                    from app import state as _st
                    for i in range(4):
                        _before = getattr(_st, "intent_failures", 0)
                        t = time.time()
                        try:
                            _classify_intent("你好")
                        except Exception as _ie:
                            log.debug("[xiaozhi] 意图端点预热异常(try=%d): %s", i + 1, _ie)
                        if getattr(_st, "intent_failures", 0) == _before:
                            log.info("[xiaozhi] 意图端点预热完成 (%.2fs, try=%d)",
                                     time.time() - t, i + 1)
                            return
                        log.debug("[xiaozhi] 意图端点预热失败(try=%d) %.2fs", i + 1, time.time() - t)
                        time.sleep(3)
                    log.warning("[xiaozhi] 意图端点预热 4 次均失败，将走 keep-hot 重试")
                threading.Thread(target=_prewarm_intent, daemon=True).start()
            # 保热循环：时间 TTS 每分钟一换（不预热首问 +~200ms）；
            # 天气 TTL 过期前刷新，避免 10 分钟后的第一问又等 2s。
            def _keep_hot(city: str):
                last_min = -1
                last_wx = 0.0
                last_intent = 0.0
                last_news = 0.0
                last_music = time.time() - 840  # 60s 后才第一次预热音乐
                _jev_on = bool(
                    os.getenv("INTENT_BASE", "").strip()
                    and os.getenv("INTENT_MODEL", "").strip())
                while True:
                    # 对齐下一分钟 +2s：用户在 :00~:05 问时间时 TTS 已热
                    now = datetime.datetime.now()
                    time.sleep(max(2, min(20, 60 - now.second + 2)))
                    try:
                        now = datetime.datetime.now()
                        if now.minute != last_min:
                            last_min = now.minute
                            _synth_mp3(f"现在{now.strftime('%H点%M分')}。")
                    except Exception:
                        pass
                    try:
                        if time.time() - last_wx > 300:
                            last_wx = time.time()
                            from app.weather import get_weather_text
                            get_weather_text(city)
                            get_weather_text(city, day_offset=1)  # 明天也热，首问不 2.7s
                    except Exception:
                        pass
                    # jev 冷启 10s+：本地模型 40s ping 压住卸载；
                    # 远程 HTTP（sxliuyu.bond）不需要 40s 心跳，10 分钟一次足够
                    # （实测远程模式 909 次 '你好' 意图调用，纯浪费）。
                    _intent_remote = _jev_on and (
                        "http" in os.getenv("INTENT_BASE", ""))
                    _intent_gap = 40 if not _intent_remote else 600
                    if _jev_on and time.time() - last_intent > _intent_gap:
                        last_intent = time.time()
                        try:
                            from agent.llm import _classify_intent
                            _classify_intent("你好")
                        except Exception:
                            pass
                    # 新闻预热：60s 中文日报 + 提前合成 mp3，首问走 TTS 缓存
                    if time.time() - last_news > 240:
                        last_news = time.time()
                        try:
                            from voice_agent import prefetch_news60, _NEWS60_CACHE
                            if prefetch_news60():
                                _synth_mp3(_NEWS60_CACHE["val"])
                            from voice_agent import _who_fast
                            _who = _who_fast("你是谁")
                            if _who:
                                _synth_mp3(_who)
                        except Exception:
                            pass
                    # 音乐预热：搜一首热门歌下到磁盘缓存，首次"放歌"不再等 15s
                    if time.time() - last_music > 900:
                        last_music = time.time()
                        try:
                            from agent.music import get_warm_song, set_warm_song, netease_search_url
                            if not get_warm_song():
                                hit = netease_search_url("热门歌曲")
                                if hit:
                                    url, name, ars = hit
                                    pkts = url_to_opus_packets(
                                        url, DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS)
                                    if pkts:
                                        set_warm_song(f"__MUSIC__{url}__{name}__{ars}")
                                        log.info("[xiaozhi] 音乐预热: %s - %s (%d pkts)",
                                                 name, ars, len(pkts))
                        except Exception as e:
                            log.debug("[xiaozhi] 音乐预热失败: %s", e)
            threading.Thread(target=_keep_hot, args=(_prewarm_city,),
                             daemon=True).start()
        except Exception as e:
            log.warning("[xiaozhi] prewarm thread err: %s", e)

    threading.Thread(target=_prewarm_hints, daemon=True).start()

    @app.get("/api/xiaozhi/status")
    def xiaozhi_status():
        """Expose ESP32-terminal health counters for dashboards / watchdog."""
        try:
            from app.xiaozhi_codec import _cache_stats as _cs
            stats = _cs()
        except Exception:
            stats = {}
        with _xiaozhi_metrics_lock:
            snap = dict(_xiaozhi_metrics)
        snap["cache"] = stats
        try:
            from voice_agent import brain_status
            snap["brain"] = brain_status()
        except Exception:
            pass
        return snap

    @app.websocket("/xiaozhi/v1")
    async def xiaozhi_websocket_v1(ws: WebSocket):
        # 官方 xiaozhi 固件常硬编码 /xiaozhi/v1/，不一定用 OTA 下发的 path
        await xiaozhi_websocket(ws)

    @app.websocket("/xiaozhi/v1/")
    async def xiaozhi_websocket_v1s(ws: WebSocket):
        await xiaozhi_websocket(ws)

    @app.websocket("/ws/xiaozhi")
    async def xiaozhi_websocket(ws: WebSocket):
        # 与 /ws 一致的鉴权策略：本地直连放行，否则校验 query token
        peer_ip = ws.client.host if ws.client else ""
        has_proxy_headers = bool(ws.headers.get("x-forwarded-for") or ws.headers.get("x-real-ip"))
        is_local = (not has_proxy_headers) and (peer_ip in ("127.0.0.1", "localhost", "::1", "", "testclient"))
        if AUTH_TOKEN and not is_local:
            token = ws.query_params.get("token", "")
            # ESP32 固件预置 token=device；同时接受管理面 AUTH_TOKEN
            if not (hmac.compare_digest(token or "", AUTH_TOKEN or "") or
                    hmac.compare_digest(token or "", XIAOZHI_DEVICE_TOKEN or "")):
                await ws.close(code=1008)
                return
        await ws.accept()
        log.info("[xiaozhi] client connected from %s", ws.client)

        if opuslib is None:
            log.error("[xiaozhi] opuslib not available, closing connection")
            await ws.close(code=1011)
            return

        bump_metric("connections_total")
        bump_metric("ws_active", 1)

        session_id = uuid.uuid4().hex[:16]
        device_key = session_id      # stable across reconnects once hello arrives
        conn_interrupted = ""         # partial reply saved on barge-in, for "继续"
        pending_sentences: list[str] = []   # sentences produced so far (replayable)
        resume_played = 0             # how many pending_sentences fully played
        stream_task: Optional[asyncio.Task] = None
        _stream_cancelled = threading.Event()  # 当前流的取消标志槽位（每流换新事件）
        _connection_tasks: set = set()  # 后台任务追踪，断开时统一取消防止泄漏
        _processing_utterance = False  # ASR→brain→TTS 处理中，抑制 barge-in 误触发
        _barge_times: list = []  # barge-in 时间戳（风暴守卫：10s 内 ≥3 次且无有效语音→goodbye）

        def _track_task(t: asyncio.Task) -> asyncio.Task:
            _connection_tasks.add(t)
            t.add_done_callback(lambda _: _connection_tasks.discard(t))
            return t

        # 注册到全局连接表（主动推送用）+ flush 待推送队列
        from app.state import register_xiaozhi_client, unregister_xiaozhi_client, flush_xiaozhi_pending, snapshot_xiaozhi_clients
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
            _track_task(asyncio.ensure_future(_flush(), loop=_loop))

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

        async def _play_canned_ack(text: str = "嗯？", force: bool = False):
            """轻量语音应答：brain 正常跑完但零句产出时避免 beep 后纯沉默。

            仅在无回复流活跃时播放；乱码 ASR 路径不使用（TTS 声音可能被设备
            麦克风回收造成反馈循环）。罐头音频经 _synth_mp3 缓存，prewarm 已热。

            force=True：调用方已 set stream_task = 本任务，守卫会把"自己"当成
            活跃流而静默什么都不播（实测 15:44 碎片轻应答完全无声）。这种
            显式播放路径必须跳过守卫。
            """
            nonlocal _tts_play_started, _tts_audio_started
            if not force and stream_task and not stream_task.done():
                return
            try:
                mp3 = await _ctx_run_in_executor(_loop, None, _synth_mp3, text)
            except Exception as e:
                log.warning("[xiaozhi] canned ack synth fail: %s | %s", text[:24], e)
                return
            if not mp3 or len(mp3) < 100:
                log.warning("[xiaozhi] canned ack empty mp3: %s | %sB", text[:24], len(mp3 or b""))
                return
            try:
                pkts = await _loop.run_in_executor(
                    None, mp3_to_opus_packets, mp3,
                    DOWNLINK_SAMPLE_RATE, OPUS_FRAME_DURATION_MS, 0.0)
            except Exception as e:
                log.warning("[xiaozhi] canned ack opus fail: %s | %s", text[:24], e)
                return
            if not pkts:
                log.warning("[xiaozhi] canned ack 0 pkts: %s", text[:24])
                return
            await send_json({"type": "tts", "state": "start"})
            await send_json({"type": "tts", "state": "sentence_start", "text": text})
            _tts_play_started = time.monotonic()
            _tts_audio_started = True
            for pkt in pkts:
                if ws.client_state != WebSocketState.CONNECTED:
                    break
                await ws.send_bytes(pkt)
                await asyncio.sleep(0.06)
            await send_json({"type": "tts", "state": "stop"})

        async def _play_then_goodbye(text: str = "好的，下次再叫我"):
            """播一句短应答然后 goodbye，让固件回唤醒词待机。

            之前 LLM 只会念"监听已关闭"却不发 goodbye，设备屏幕还停在
            聆听中（用户实测"让他退出，不退出"）。
            """
            nonlocal _tts_play_started
            try:
                await _play_canned_ack(text, force=True)
            except Exception:
                pass
            try:
                await send_json({"type": "goodbye"})
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

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
            nonlocal _stream_cancelled, _tts_play_started, _tts_audio_started

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

            def _put_blocking(item, cancel_evt=cancel_evt):
                # The consumer (pacing) is the bottleneck; block here so a long
                # reply never overflows the queue and never DROPS a sentence:
                # slots free up as each sentence finishes its paced send, so a
                # steady-state full queue just means playback is slower than
                # synthesis — normal for long replies. Give up only when this
                # stream was cancelled (barge-in), the WS disconnected, or the
                # owning loop shut down / a hard deadline hit (dead consumer).
                deadline = time.monotonic() + 120
                while True:
                    if not loop.is_running() or not ws.client_state == WebSocketState.CONNECTED:
                        return
                    if cancel_evt.is_set():
                        log.info("[xiaozhi] _put_blocking: 本流已取消，停止投递")
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
                        if time.monotonic() > deadline:
                            log.warning("[xiaozhi] _put_blocking 超时(120s)：消费者疑似失联，放弃本句")
                            return
                        time.sleep(0.05)

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
                            mp3 = _synth_mp3(s, cancel_evt=cancel_evt)
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
                            cancel_event=cancel_evt,
                        ))
                    except Exception as e:
                        log.error("[xiaozhi] brain iter fail: %s", e)
                        # 往队列放兜底句子，让 consumer 正常走 TTS start→播报→stop
                        if not _fallback_sent:
                            try:
                                fallback = "抱歉，出了点问题，请再说一遍"
                                fb_mp3 = _synth_mp3(fallback, cancel_evt=cancel_evt)
                                if fb_mp3 and len(fb_mp3) > 100:
                                    _put_blocking((fallback, fb_mp3))
                                    _fallback_sent = True
                            except Exception:
                                pass
                        _brain_iter = iter(())
                    try:
                        _sent_emitted = 0
                        _progress_spoken = False
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
                            # 语音硬闸：只播第一句实质内容，且超长句在标点处截断。
                            # 模型不总是听话（人格强推"Sir，我是 J.A.R.V.I.S.，…"），
                            # 实测 29 字 ≈ 5.2s 播放，用户以为设备卡住。
                            # 进度提示/纯应答不占名额：播完"正在查新闻...""Sir，"
                            # 之后真头条还得出声。
                            _stripped_s = s.strip()
                            _is_progress = bool(_TOOL_PROGRESS_RE.match(_stripped_s))
                            _is_ack = bool(_ACK_ONLY_RE.match(_stripped_s))
                            if _is_progress and _progress_spoken:
                                continue
                            if _is_ack:
                                continue
                            if _sent_emitted > 0 and not _is_progress:
                                log.info("[xiaozhi] 语音单句模式，忽略后续句: %.30s", s)
                                continue
                            # 短句也剥 Sir（"Sir，收到" 洋腔一秒，长句才走 _shorten_voice）
                            _s2 = _VOICE_PREFIX_RE.sub("", _stripped_s).strip()
                            if len(_s2) >= 2:
                                s = _s2
                            if not _is_progress and len(s) > _MAX_VOICE_CHARS:
                                _cut = _shorten_voice(s, _MAX_VOICE_CHARS)
                                if _cut:
                                    log.info("[xiaozhi] 首句 %d 字精简为 %d 字: %.30s",
                                             len(s), len(_cut), _cut)
                                    s = _cut
                            mp3 = _synth_mp3(s, cancel_evt=cancel_evt)
                            if mp3 and len(mp3) > 100:
                                pending_sentences.append(s)
                                _put_blocking((s, mp3))
                                if _is_progress:
                                    _progress_spoken = True
                                else:
                                    _sent_emitted += 1
                            # 首句已产出，后续不再计时
                            _brain_t0 = None
                    except Exception as e:
                        log.error("[xiaozhi] brain error: %s", e)
                        # 往队列放兜底句子，让 consumer 正常走 TTS start→播报→stop
                        if not _fallback_sent:
                            try:
                                fallback = "抱歉，出了点问题，请再说一遍"
                                fb_mp3 = _synth_mp3(fallback, cancel_evt=cancel_evt)
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
                                    fb_mp3 = _synth_mp3(fallback, cancel_evt=cancel_evt)
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
            # 播报时长预算：语音场景长回答只播前 MAX_REPLY_FRAMES 帧，
            # 剩余句子存进 pending_sentences，用户说"继续"再播。
            # 实测 191 帧 = 11.5s 播放，用户会以为设备卡住（15:40）。
            played_frames = 0
            _truncated = False
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
                                    _fb_mp3 = await _ctx_run_in_executor(
                                        loop, None, _synth_mp3, _fb)
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
                        if not started:
                            # brain 正常结束但零句产出：播轻应答避免 beep 后纯沉默
                            # （乱码 ASR 路径保持沉默——TTS 应答可能被设备麦克风
                            #  回收造成反馈循环，故仅在"真说话但没答案"时用）
                            await _play_canned_ack("嗯？")
                        break
                    if s == "__TTS_FAIL__":
                        await send_json({"type": "stt", "text": "抱歉，语音合成暂时不可用"})
                        continue
                    if not first_sentence_logged:
                        first_sentence_logged = True
                        t_brain_first = loop.time()
                        log.info("[xiaozhi] latency: brain首句+TTSSynth=%.0fms text=%.60s",
                                 (t_brain_first - t_reply_start) * 1000, s)
                    if s == "__MUSIC_STOP__":
                        # 停止音乐：一句话都不播，直接收尾。之前这里落到
                        # "__MUSIC__" 分支后被当成 url="" 静默 continue，
                        # 用户说"停止播放"毫无反应（16:39:27 实测）。
                        log.info("[xiaozhi] MUSIC_STOP：停止音乐播放")
                        bump_metric("music_stops")
                        if not started:
                            await send_json({"type": "tts", "state": "start"})
                            started = True
                        await send_json({"type": "tts", "state": "stop"})
                        break
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
                            # 下载/转码失败：给用户一句解释而不是静默跳过
                            try:
                                fb_mp3 = await _ctx_run_in_executor(
                                    loop, None, _synth_mp3, "抱歉，音乐加载失败")
                                if fb_mp3 and len(fb_mp3) > 100:
                                    _put_blocking(("抱歉，音乐加载失败", fb_mp3))
                            except Exception:
                                pass
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
                        _tts_play_started = time.monotonic()
                        _tts_audio_started = True
                    # 预算已用尽：本句及后续都攒着，等用户说"继续"
                    # 音乐不受对话帧预算约束 —— 那是给 TTS 长回答设的上限
                    # （100 帧≈6s），套在歌上会把每首歌都切成 6 秒。
                    if is_tts and (_truncated or played_frames >= MAX_REPLY_FRAMES):
                        if not _truncated:
                            _truncated = True
                            log.info("[xiaozhi] 播报预算用尽(%d帧≈%.1fs)，余下句子留给'继续'",
                                     played_frames, played_frames * frame_s)
                        if mp3:
                            pending_sentences.append(s)
                        continue
                    bump_metric("tts_sentences")
                    await send_json({
                        "type": "tts", "state": "sentence_start", "text": label})
                    t_opus = loop.time()
                    # 单句也可能超预算（实测第一句 148 帧 ≈ 8.9s），
                    # 按帧截断：只播到预算上限，剩下的整句留给"继续"。
                    if is_tts:
                        _budget_left = max(0, MAX_REPLY_FRAMES - played_frames)
                    else:
                        _budget_left = len(pkts)   # 音乐：整首播，不截断
                    _clip = pkts[:_budget_left] if len(pkts) > _budget_left else pkts
                    if len(pkts) > _budget_left:
                        log.info("[xiaozhi] 句内截断：%d帧→%d帧，余下留给'继续'",
                                 len(pkts), _budget_left)
                        pending_sentences.append(s)
                    sizes = len(_clip)
                    for pkt in _clip:
                        if ws.client_state != WebSocketState.CONNECTED:
                            break
                        # 每帧检查**本流**的取消标志：全局 _stream_cancelled 是
                        # "当前流槽位"，新流一来就指向新事件，旧流永远看不到
                        # 自己被取消 —— 音乐这种长段会一直发到结尾（实测说
                        # "停止播放"后仍继续唱 25s）。
                        if cancel_evt.is_set():
                            log.info("[xiaozhi] 播放在第 %d 帧被中断", sent)
                            break
                        await ws.send_bytes(pkt)
                        sent += 1
                        target = sent - HEAD_START_FRAMES
                        if target > 0:
                            wait = target * frame_s - (loop.time() - first_send)
                            if wait > 0:
                                await asyncio.sleep(wait)
                    t_after_send = loop.time()
                    played_frames += sizes
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
                await asyncio.sleep(_trailing_wait)
                if started:
                    await send_json({"type": "tts", "state": "stop"})
                # tts stop 已发送，解除 streaming 标志，允许积压提醒 flush
                _set_xiaozhi_streaming(False)
                # streaming 已结束：若 pending 队列有积压的提醒 TTS，立即 flush
                if ws.client_state == WebSocketState.CONNECTED:
                    try:
                        from app.state import flush_xiaozhi_pending, snapshot_xiaozhi_clients
                        if session_id in snapshot_xiaozhi_clients():
                            _track_task(asyncio.ensure_future(
                                flush_xiaozhi_pending(session_id, ws, loop)))
                    except Exception as _fl:
                        log.debug("[xiaozhi] post-stream flush enqueue failed: %s", _fl)
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
                # TTS 失败也要重新武装对话窗口：否则 ARM_WINDOW 到期后
                # 用户必须重新喊唤醒词才能继续（表现为"说了没反应"）
                _arm_win = _get_arm_window()
                if _arm_win > 0:
                    armed_until = time.monotonic() + _arm_win
                    noise_floor = 0.0
                    log.info("[xiaozhi] TTS fail, re-armed for %.0fs follow-up",
                             _arm_win)
                # 至少给用户一声反馈，不是死寂（"听见了却没回话"的兜底）
                try:
                    await _play_wake_beep()
                except Exception as _be:
                    log.debug("[xiaozhi] TTS fail beep also failed: %s", _be)
            finally:
                _set_xiaozhi_streaming(False)
                # streaming 已结束：若 pending 队列有积压的提醒 TTS，立即 flush
                if ws.client_state == WebSocketState.CONNECTED:
                    try:
                        from app.state import flush_xiaozhi_pending, snapshot_xiaozhi_clients
                        if session_id in snapshot_xiaozhi_clients():
                            _track_task(asyncio.ensure_future(
                                flush_xiaozhi_pending(session_id, ws, loop)))
                    except Exception as _fl:
                        log.debug("[xiaozhi] post-stream flush enqueue failed: %s", _fl)
                try:
                    await asyncio.wait_for(asyncio.shield(producer), timeout=2.0)
                except asyncio.TimeoutError:
                    log.warning("[xiaozhi] producer未在2s内结束，放弃等待")
                    cancel_evt.set()
                    rebuild_brain_pool()
                except Exception:
                    pass

        async def process_utterance(frames: list):
            nonlocal stream_task, _processing_utterance, noise_cooldown_until, _tts_play_started, _tts_audio_started
            if not frames:
                log.info("[xiaozhi] empty utterance, ignored silently")
                return
            _processing_utterance = True  # 抑制 barge-in 误触发
            _tts_audio_started = False  # 本轮还没真的出声
            bump_metric("utterances_total")
            try:
                wav, asr_text, asr_provider, asr_duration_ms = await asyncio.wait_for(
                    asyncio.to_thread(_decode_check_asr, frames), timeout=30)
            except asyncio.TimeoutError:
                log.error("[xiaozhi] ASR pipeline timeout(>30s)")
                bump_metric("asr_failures")
                await cancel_stream()
                stream_task = asyncio.create_task(
                    _safe_stream("语音识别超时了，请稍后再试"))
                _processing_utterance = False
                return
            except Exception as e:
                log.error("[xiaozhi] decode/ASR fail: %s", e)
                bump_metric("asr_failures")
                await cancel_stream()
                stream_task = asyncio.create_task(
                    _safe_stream("语音识别失败了，请再试一次"))
                _processing_utterance = False
                return
            # _decode_check_asr returns (None, "", "", 0.0) for empty/silent audio
            if wav is None:
                noise_cooldown_until = time.monotonic() + 2.0
                _processing_utterance = False
                return
            from voice_agent import is_low_intent_asr, is_garbled_asr
            from agent.intent import LOW_INTENT_ASR_REPLY, strip_wake_word
            # 退出/再见先于乱码闸门：'拜拜' 曾被 2 字白名单漏掉整句吞掉
            _t0_exit = (asr_text or "").strip()
            if (len(_t0_exit) <= 14 and any(k in _t0_exit for k in (
                    "退出", "关闭监听", "停止监听", "下线", "再见", "拜拜",
                    "关机", "休眠", "我走了", "你休息", "休息吧", "晚安"))):
                log.info("[xiaozhi] 退出指令 %r → goodbye", _t0_exit)
                await send_json({"type": "stt", "text": _t0_exit})
                await cancel_stream()
                _processing_utterance = False
                _tts_play_started = time.monotonic()
                stream_task = asyncio.create_task(_play_then_goodbye())
                return
            try:
                from app.audit_log import audit_log
                audit_log("ws:asr", input_data=f"{len(frames)}frames",
                          output_data=asr_text, action="recognize",
                          session_id=f"xiaozhi-{device_key}",
                          duration_ms=asr_duration_ms)
            except Exception:
                pass
            if os.getenv("ASR_DUMP_WAV", "0") == "1" and wav:
                try:
                    dump_dir = os.path.join(DATA_DIR, "asr_dump")
                    os.makedirs(dump_dir, exist_ok=True)
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_text = re.sub(r'[^\w\u4e00-\u9fff-]', '', (asr_text or ""))[:20]
                    fname = f"{ts}_{asr_provider}_{safe_text}.wav"
                    with open(os.path.join(dump_dir, fname), "wb") as f:
                        f.write(wav)
                except Exception as e:
                    log.debug("[xiaozhi] ASR dump failed: %s", e)
            if not asr_text or is_garbled_asr(asr_text):
                log.info("[xiaozhi] ASR filtered as garbled: %r", asr_text)
                noise_cooldown_until = time.monotonic() + 2.0
                await send_json({"type": "stt", "text": ""})
                _processing_utterance = False
                return
            _barge_times.clear()  # 有效语音：重置背景音风暴计数
            # 短指令先过本地快路径：静音/音量/笑话 2-4 字指令会被下面
            # 碎片闸门吃掉（实测"声音大点"回"没听清"）。天猫级即时：
            # 命中就不进 brain，也不进碎片闸门。
            try:
                from voice_agent import FAST_PATHS as _FP
                _fp_reply = None
                for _p in _FP:
                    _fp_reply = _p.run(asr_text, session_id=f"xiaozhi-{device_key}")
                    if _fp_reply:
                        break
            except Exception as _e:
                log.debug("[xiaozhi] 快路径预检失败: %s", _e)
                _fp_reply = None
            if _fp_reply:
                # 音乐/系统罐头爱带 "好的，Sir，"，语音里又慢又怪
                import re as _re_fp
                _fp_reply = _re_fp.sub(
                    r"^(?:好的)?[，,]?\s*(?:Sir|sir|先生)[，,]?", "", _fp_reply).strip() or _fp_reply
                log.info("[xiaozhi] 快路径直答: %.24s -> %.32s", asr_text, _fp_reply)
                bump_metric("fast_path_replies")
                await send_json({"type": "stt", "text": asr_text})
                await cancel_stream()
                # 罐头直答不走 _safe_stream，必须自己复位，否则后续
                # 热帧全被 "_processing_utterance" 吞掉（聆听中没反应）。
                _processing_utterance = False
                _tts_play_started = time.monotonic()
                if _fp_reply == "__MUSIC_STOP__":
                    # 协议令牌不能当 TTS 文本念（实测把 __MUSIC_STOP__ 读出来）。
                    # 与 stream_tts_reply 的 MUSIC_STOP 分支一致：start→stop，
                    # 设备端停流，不出声。
                    log.info("[xiaozhi] MUSIC_STOP：停止音乐播放")
                    bump_metric("music_stops")
                    await send_json({"type": "tts", "state": "start"})
                    await send_json({"type": "tts", "state": "stop"})
                    return
                if _fp_reply.startswith("__MUSIC__"):
                    # 天猫式：先应一声再拉歌。网易云外链冷启实测 15s+，
                    # 静默等下载用户以为没听见；"好的"先出声，歌随后到。
                    async def _play_music_flow(_proto: str = _fp_reply):
                        try:
                            await _play_canned_ack("好的", force=True)
                        except Exception:
                            pass
                        await _safe_stream("播放音乐", prebuilt=[_proto])
                    stream_task = asyncio.create_task(_play_music_flow(_fp_reply))
                    return
                # 合成要几百 ms，起播标志必须现在就设——否则这段空窗里
                # 残尾 barge-in 会把刚起的天气播报掐了（实测 31ms）。
                stream_task = asyncio.create_task(
                    _play_canned_ack(_fp_reply, force=True))
                return
            # ASR 碎片闸门：极短且不像问句的残句（"大""那是""没几年"）不送 brain。
            # 实测这些碎片每句都要花 1.3-3.3s LLM，回一句"语句不完整"，
            # 既慢又吵；直接轻应答更适合语音场景。含疑问/指令词的短句放行。
            _stripped = asr_text.strip(" ，。！？、,.!?~～…")
            if (len(_stripped) <= 4
                    and not any(k in _stripped for k in (
                        "几", "什么", "谁", "哪", "怎么", "为什么", "多少",
                        "放", "播", "开", "关", "停", "换", "调", "查", "要", "帮",
                        "讲", "说", "唱", "笑", "来", "给",
                        "你", "我", "谢", "再", "早", "晚"))):
                log.info("[xiaozhi] ASR 碎片，轻应答: %r", asr_text)
                await send_json({"type": "stt", "text": asr_text})
                await cancel_stream()
                # 直接播罐头音频（prewarm 已热），不要再送 brain：
                # 早前误用 _safe_stream("没听清…") 会把这句当用户输入再跑一遍
                # LLM，实测 2.2s + 8s 播两段不相干的话（15:43）。
                _processing_utterance = False
                _tts_play_started = time.monotonic()
                stream_task = asyncio.create_task(_play_canned_ack("没听清，再说一遍？", force=True))
                return
            # pure filler (嗯/hmm/huh...) → light acknowledgment, no brain round-trip
            if is_low_intent_asr(asr_text):
                log.info("[xiaozhi] ASR low-intent, ack only: %r", asr_text)
                await send_json({"type": "stt", "text": asr_text})
                await cancel_stream()
                # 同碎片路径：播缓存音频，别把罐头语当用户输入再进 brain
                _processing_utterance = False
                _tts_play_started = time.monotonic()
                stream_task = asyncio.create_task(
                    _play_canned_ack(LOW_INTENT_ASR_REPLY, force=True))
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
            nonlocal _processing_utterance
            try:
                await stream_tts_reply(text, interrupted_reply=interrupted_reply,
                                       prebuilt=prebuilt)
            except Exception as e:
                log.error("[xiaozhi] stream_tts_reply error: %s", e, exc_info=True)
            finally:
                _processing_utterance = False  # 恢复 barge-in 检测

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
        noise_cooldown_until = 0.0  # garbled/no-speech 后短暂不再起 utterance，断噪音风暴
        auto_turns = 0       # 连续自动应答轮数（无新唤醒）；超过上限判定背景音
        _tts_play_started = 0.0  # 本次 TTS 起播时刻（防自回声/残尾误 barge-in）
        _tts_audio_started = False  # True=已经真的在发音频（brain 思考中不算）
        _window_closed_at = 0.0  # 自动应答超限主动关窗时刻（给用户一声提示）

        def update_volume_from_noise():
            nonlocal cur_volume_db
            if noise_floor > 200:
                cur_volume_db = 6.0
            elif noise_floor > 140:
                cur_volume_db = 3.0
            else:
                cur_volume_db = 0.0

        def speech_threshold() -> float:
            """Adaptive endpointer threshold = SIG_RATIO * rolling noise floor.

            上限 150：LC-S3 实测 noise_floor 被 100+ 的说话帧污染后
            thr 会飙到 250-380，RMS 兜底跟着失效（"说话没反应"）。
            """
            if noise_floor <= 0:
                return SILENCE_RMS_DEFAULT
            return min(max(SILENCE_RMS_DEFAULT * 0.5, noise_floor * SIG_RATIO),
                       SILENCE_RMS_DEFAULT)

        def reset_utterance():
            nonlocal buf_frames, speech_count, silence_count, utterance_active, hot_frames, noise_floor, resume_played, pending_sentences
            tail.clear()
            buf_frames = []
            speech_count = 0
            silence_count = 0
            utterance_active = False
            hot_frames = 0
            noise_floor = 0.0   # reset adaptive baseline so a loud utterance doesn't poison the threshold
            pending_sentences.clear()  # 重置当前语句的待发送句表
            resume_played = 0

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=120.0)
                    # 标记 ESP32 最近活跃（调度器据此判断是否推语音通知）
                    if frame_count % 100 == 0:
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
                            _next_rem = _next_reminder_in()
                            if _next_rem is not None:
                                # 天猫级闹钟：15 分钟内有提醒就不 goodbye，
                                # 长连接等调度器直推叮咚（否则固件已回待机，
                                # 提醒只能入队等下次唤醒，40 秒差就丢）。
                                hold = min(_next_rem + 45.0, 960.0)
                                armed_until = time.monotonic() + hold
                                log.info(
                                    "[xiaozhi] %.0fs 后有提醒，保持连接等闹钟 "
                                    "(hold=%.0fs, role_window=%.0fs)",
                                    _next_rem, hold, _arm_win)
                            else:
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
                        rms = _pcm_rms(pcm)
                    except Exception as dex:
                        rms = 0
                        if frame_count % 50 == 0:
                            log.info("[xiaozhi] opus decode err: %s", dex)
                    if frame_count % 300 == 0:
                        _vad_p = _vad_speech_prob(pcm) if pcm else -1.0
                        log.info("[xiaozhi] debug rms=%d vad=%.2f speech=%d silence=%d pktlen=%d active=%s",
                                 rms, _vad_p, speech_count, silence_count, len(pkt), utterance_active)

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
                        if time.monotonic() < noise_cooldown_until:
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
                        if await _is_speech_vad_async(pcm, rms, thr):
                            # Debounce: require a few consecutive hot frames so a
                            # single transient (own-TTS echo, door click, music)
                            # doesn't false-trigger a barge-in or start of speech.
                            hot_frames += 1
                            if hot_frames < BARGE_IN_FRAMES:
                                continue
                            # Barge-in: user started talking while we were still
                            # playing TTS — cut the audio so we can listen.
                            # 仅在"brain 处理中且尚未开始播报"时抑制：残余设备音频
                            # 帧会误取消一个还没开始播放的回复。播报开始后必须放行
                            # barge-in（用户喊停是核心交互），自TTS回声误触发由
                            # 设备AEC+下方风暴守卫（10s≥3次→goodbye）兜底。
                            if _processing_utterance and not (stream_task and not stream_task.done()):
                                hot_frames = 0
                                continue
                            if stream_task and not stream_task.done():
                                # 残尾保护只盖起播/起任务后 1.2s：再长的静默
                                # （音乐冷启下载 15s+）后续说话必须能插话/停止，
                                # 否则"放歌→下载中喊停"永远没反应。
                                if (_tts_play_started > 0
                                        and time.monotonic() - _tts_play_started < 1.2):
                                    # 还在合成/思考/残尾：不许打断
                                    # （实测残尾 31ms 就把刚识别完的天气播报掐了）。
                                    hot_frames = 0
                                    continue
                                if not _tts_audio_started:
                                    log.info("[xiaozhi] barge-in during synth (no audio yet)")
                                else:
                                    log.info("[xiaozhi] barge-in, cancelling current TTS")
                                bump_metric("barges_total")
                                _barge_times.append(time.monotonic())
                                # 风暴守卫：10s 内 ≥3 次 barge-in 且期间无一次有效
                                # ASR（有效语音会在 process_utterance 里清空计数）
                                # → 判定为电视/音响等背景音循环，goodbye 退出，
                                #   否则用户会经历"beep→沉默"的死循环（20:57 实测 44s）
                                recent = [t for t in _barge_times
                                          if time.monotonic() - t < 10.0]
                                _barge_times[:] = recent
                                if len(recent) >= 3:
                                    log.warning(
                                        "[xiaozhi] 10s 内连续 %d 次 barge-in 无有效语音，"
                                        "判定背景音干扰，goodbye 退出", len(recent))
                                    _barge_times.clear()
                                    try:
                                        await send_json({"type": "stt",
                                                         "text": "背景音干扰较多，先退出聆听，需要时再叫我"})
                                    except Exception:
                                        pass
                                    try:
                                        await send_json({"type": "goodbye"})
                                        await ws.close()
                                    except Exception:
                                        pass
                                    break
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
                    if await _is_speech_vad_async(pcm, rms, thr):
                        speech_count += 1
                        silence_count = 0
                    else:
                        silence_count += 1
                    # VAD 可用时静音确认更短(0.48s vs RMS 1.2s), 砍尾静音延迟
                    silence_limit = SILENCE_FRAMES_VAD if (VAD_ENABLED and _load_silero_vad() is not None) else SILENCE_FRAMES
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
                            noise_cooldown_until = time.monotonic() + 2.0
                            # 诊断：把被丢弃的音频落盘，离线看 Silero/ASR 到底听到了什么
                            try:
                                _wav_dbg = opus_decode_to_wav(frames, UPLINK_SAMPLE_RATE)
                                if _wav_dbg:
                                    _dump_dir = os.path.join(DATA_DIR, "asr_dump")
                                    os.makedirs(_dump_dir, exist_ok=True)
                                    with open(os.path.join(
                                            _dump_dir,
                                            f"nospeech_{datetime.datetime.now().strftime('%H%M%S')}.wav"),
                                            "wb") as _f:
                                        _f.write(_wav_dbg)
                            except Exception as _de:
                                log.debug("[xiaozhi] nospeech dump fail: %s", _de)
                            continue
                        _arm_win = _get_arm_window()
                        if _arm_win > 0 and time.monotonic() > armed_until:
                            # Heard real speech but outside the wake window —
                            # ambient conversation/TV. Don't talk on our own.
                            log.info("[xiaozhi] outside arm window, ignoring (%.1fs)",
                                     time.monotonic() - armed_until)
                            # 主动关窗后 8s 内还在说话 → 提示一次为什么不应
                            # （自然过期/电视不提示，免得自己跟电视对话）
                            if (_window_closed_at > 0
                                    and time.monotonic() - _window_closed_at < 8.0):
                                _window_closed_at = 0.0  # 只提示一次
                                try:
                                    await send_json({"type": "stt",
                                                     "text": "请先喊我"})
                                except Exception:
                                    pass
                                stream_task = asyncio.create_task(
                                    _play_canned_ack("请先喊我", force=True))
                            continue
                        # Heard speech inside the wake window: this is a real
                        # command. Refresh the window so a natural back-and-forth
                        # (user replies to our answer without re-waking) stays hot.
                        # 但 auto_turns 越过上限后不再续窗：电视/环境人声会一轮
                        # 接一轮无限续命（实测 20s 内 8 轮全量 LLM，15:37）。
                        auto_turns += 1
                        if _arm_win > 0 and auto_turns <= AUTO_TURN_LIMIT:
                            armed_until = time.monotonic() + _arm_win
                        elif auto_turns > AUTO_TURN_LIMIT:
                            # 立即关闭窗口（而不是只停止续窗）：否则上一轮续的
                            # 300s 仍然有效，背景音要闹满 5 分钟才停。
                            armed_until = 0.0
                            _window_closed_at = time.monotonic()
                            log.info("[xiaozhi] 连续自动应答 %d 轮，关闭对话窗口（需重新唤醒）",
                                     auto_turns)
                            # 别静默不理人：说一声为什么突然不应了
                            stream_task = asyncio.create_task(
                                _play_canned_ack("聊了挺久了，再喊我一声", force=True))
                        _track_task(asyncio.create_task(process_utterance(frames)))
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
                    # 复用写入侧同一套清洗规则：两侧规则漂移会让绑定静默失效
                    from agent.device_users import _sanitize_device_key
                    _hello_dev = ""
                    for dev_key in ("client_id", "device_id", "device"):
                        cand = data.get(dev_key)
                        if isinstance(cand, dict):
                            cand = (cand.get("device_id") or cand.get("mac")
                                    or cand.get("client_id") or cand.get("name"))
                        if cand:
                            _hello_dev = _sanitize_device_key(cand)
                            if _hello_dev:
                                device_key = _hello_dev
                            break
                    # 告诉连接表"这条连接是哪台设备"，定向提醒才能路由过来。
                    # hello 可能晚于 register_xiaozhi_client：那时连接已存在才写得进去；
                    # 早于注册则静默 no-op，flush 时再按 session_id 反推设备号兜底。
                    # 空值一律不写 —— 否则会被当成"已绑定空设备"。
                    if _hello_dev:
                        try:
                            from app.state import set_xiaozhi_client_device
                            set_xiaozhi_client_device(session_id, _hello_dev)
                        except Exception as _de:
                            log.debug("[xiaozhi] 记录连接设备号失败: %s", _de)
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
                        auto_turns = 0  # 新唤醒：重新给满自动应答额度
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
                            if _processing_utterance or (
                                    stream_task and not stream_task.done()):
                                # VAD 已 endpoint 并进入 ASR→brain→TTS；
                                # 固件此时补发的 listen stop 只是收尾，
                                # 不能 cancel 正在生成的回复（否则"听见了却没回答"）。
                                # 快路径已把 _processing_utterance 复位，还剩
                                # canned-ack / music 流在飞——同样不许掐。
                                log.info("[xiaozhi] listen stop, no frames, reply in flight — keep")
                            else:
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
                                _track_task(asyncio.create_task(process_utterance(frames)))

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
            for t in list(_connection_tasks):
                if not t.done():
                    t.cancel()
            _connection_tasks.clear()
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
