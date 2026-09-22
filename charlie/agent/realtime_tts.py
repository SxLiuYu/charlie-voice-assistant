"""StepAudio 2.5 Realtime —— 把 brain 的回复文本变成语音（FR-13 的发声通道）。

设计意图（详见 docs/PLAN_FR13_REALTIME_INTERRUPT.md）：

    ASR → brain(LLM + MCP，动作在此执行完) → 回复文本
          → 本模块逐字朗读 → pcm16(24kHz) → 调用方转 Opus 下行
          ↑ 用户开口 → cancel_evt → response.cancel

**取消边界只画在"发声"这一段**：MCP 动作有真实副作用且不可回滚，一旦开始执行就必须
让它跑完；被用户打断的只是"说"。调用方不要用本模块的取消去中断工具调用。

实测数据（step_plan 路径 `stepaudio-2.5-realtime`，2026-09-19 真 Key 实测，17 个样本；
`scripts/stepaudio3_probe.py`）：
- 首音频 **576–769ms**（上一代 `stepaudio-3-realtime-preview` 239–305ms，仍是
  preview；对比现用 step-tts-2 批量接口 1849ms、生产在用的 stepaudio-2.5-tts 1733ms）
- 采样率 **24000Hz**（24k 假设下语速 5.0 字/秒，中文 TTS 正常约 4.5）
- 取消后残留 **0 个音频增量**；174 字长文本逐字一致；多轮连接无指令污染

⚠️ 四个必须知道的协议事实（2.5 实测，官方文档未覆盖）：

1. **照读不可靠，instructions 是必要不充分条件。** 实测 17 个样本里约 30–40%
   会被改写：多数是把大脑文本当用户发言自己回一句（"把电视音量调到十五。"→
   "好的，这就为您把电视音量调到十五。"），少数是轻微改写（"现在时间是下午
   三点二十分。"→"下午三点二十分。"）。助手口吻的陈述句（动作确认/天气/
   提醒播报）大多逐字一致；**指令式、用户口吻的句子失手率接近 100%**。
   ⇒ 接线时必须用 `response.audio_transcript.done` 的 transcript 与源文本比对，
   不一致就丢弃这段音频、回落常规 TTS —— 否则用户会听到"和大脑想的不一样"。
2. **取消后 ``response.done.status`` 是 ``incomplete``，不是 ``cancelled``**，
   而且**不会发 ``response.cancelled`` 事件** —— 等它就会挂死。
3. **音频只有 pcm16（Base64）**，没有 mp3 / opus 选项。
4. **连接有配额限制与空闲超时**：连续请求会 HTTP 429（15s 间隔仍被限，是持续性
   配额）；连接建好后不发任何操作，服务端主动断开并报 error
   "too long without operation"。生产是"每次合成一个请求、发完就收"的短连接
   模式，正常对话节奏（每轮 1 个请求）不会撞上，但突发/循环重试要留退避。

环境变量沿用 ``STEPAUDIO3_*`` 前缀（历史命名，模型已是 2.5，改名会让存量
.env 静默失效——``is_configured()`` 找不到新名字的开关就永远返回 False）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("magic")

# 订阅路径 step_plan/v1：与 STEPFUN_TTS_BASE / STEPFUN_LLM_BASE 同一个
# 订阅计费通道。v1 路径的 stepaudio-3-realtime-preview 是限免预览版，随时可能
# 到期；2.5 是正式计费模型（输出 70 元/1M tokens），默认指向它。
DEFAULT_URL = "wss://api.stepfun.com/step_plan/v1/realtime"
DEFAULT_MODEL = "stepaudio-2.5-realtime"
DEFAULT_VOICE = "jingdiannvsheng"
DEFAULT_SAMPLE_RATE = 24000

# 让模型"照读"的指令模板。实测这个措辞能逐字复现（含 85 字多句带数字的长文本）；
# 换成"只许朗读"写进 system 消息会被改写且报 error。
_READ_INSTRUCTION = "请逐字朗读下面这句话，不要改写：{text}"

_PCM16_BYTES_PER_SAMPLE = 2


class RealtimeTTSError(RuntimeError):
    """连接 / 协议 / 鉴权层面的失败。"""


def is_configured() -> bool:
    """是否具备启用条件：开关打开 + 有 StepFun Key（支持多把轮询）。"""
    from agent import stepfun_keys
    return (os.getenv("STEPAUDIO3_RT_ENABLED", "0") == "1"
            and bool(stepfun_keys.has_keys()))


def sample_rate() -> int:
    try:
        return int(os.getenv("STEPAUDIO3_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE)))
    except ValueError:
        return DEFAULT_SAMPLE_RATE


def _url() -> str:
    base = os.getenv("STEPAUDIO3_RT_URL", DEFAULT_URL).strip()
    model = os.getenv("STEPAUDIO3_RT_MODEL", DEFAULT_MODEL).strip()
    return f"{base}?model={model}"


def _send(ws, payload: dict) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False))


def _session_update(ws, voice: Optional[str]) -> None:
    session = {
        "modalities": ["text", "audio"],
        # 文本驱动，不要服务端的 VAD —— 打断判定归 Charlie 自己的 VAD 管，
        # 两套 VAD 同时存在会互相误触发。
        "turn_detection": None,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
    }
    v = (voice or os.getenv("STEPAUDIO3_VOICE", DEFAULT_VOICE)).strip()
    if v:
        session["voice"] = v
    _send(ws, {"type": "session.update", "session": session})


def _ask_verbatim(ws, text: str) -> None:
    """让模型逐字朗读 text。"""
    _send(ws, {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]},
    })
    tmpl = os.getenv("STEPAUDIO3_READ_INSTRUCTION", _READ_INSTRUCTION)
    _send(ws, {
        "type": "response.create",
        "response": {"instructions": tmpl.format(text=text)},
    })


def synthesize_streaming(
    text: str,
    on_pcm: Callable[[bytes], None],
    cancel_evt: Optional[threading.Event] = None,
    *,
    voice: Optional[str] = None,
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    cancel_grace: float = 2.0,
) -> dict:
    """逐字朗读 ``text``，音频增量经 ``on_pcm`` 回调抛出。

    Args:
        text: 要念的文本。**原样朗读**，模型不会改写。
        on_pcm: 每收到一段 pcm16 音频调用一次。**取消后不再回调。**
            采样率见 :func:`sample_rate`（24000Hz），单声道 16bit。
        cancel_evt: 被 set 时立即发 ``response.cancel`` 并停止回调。
            用于"用户开口打断"。
        voice: 音色，默认取 ``STEPAUDIO3_VOICE``。
        timeout: 单次 recv 的超时（秒）。
        connect_timeout: 建连超时。
        cancel_grace: 发出 cancel 后最多再等多久收 ``response.done``。

    Returns:
        dict：``ok`` / ``bytes`` / ``cancelled`` / ``first_audio_ms`` /
        ``total_ms`` / ``status`` / ``error``。**不抛异常**——调用方靠 ok 判断，
        失败要降级到下一个 TTS provider 时不会被异常打断音频流。
    """
    import websocket  # websocket-client，同步 API
    from agent import stepfun_keys

    stats = {"ok": False, "bytes": 0, "cancelled": False, "first_audio_ms": None,
             "total_ms": None, "status": None, "error": None}
    if not stepfun_keys.has_keys():
        stats["error"] = "STEPFUN_KEY 未配置"
        return stats
    # 多 key 轮询路由：每条连接取下一把；握手 429/401 打标冷却
    key = stepfun_keys.next_key()

    t0 = time.monotonic()
    ws = None
    cancelled = False
    done = False
    try:
        ws = websocket.create_connection(
            _url(),
            header=[f"Authorization: Bearer {key}"],
            timeout=connect_timeout,
        )
        ws.settimeout(timeout)
        _session_update(ws, voice)
        _ask_verbatim(ws, text)

        while not done:
            # 取消检查放在 recv 之前：recv 会阻塞到超时，打断延迟就是这里
            if cancel_evt is not None and cancel_evt.is_set() and not cancelled:
                cancelled = True
                stats["cancelled"] = True
                try:
                    _send(ws, {"type": "response.cancel"})
                except Exception as e:  # 连接可能已经断了
                    log.debug(f"[realtime_tts] 发 response.cancel 失败: {e}")
                ws.settimeout(cancel_grace)

            try:
                raw = ws.recv()
            except Exception as e:
                if cancelled:
                    break  # 取消后读不到 done 是正常收尾
                raise RealtimeTTSError(f"recv 失败: {e}") from e
            if not raw:
                break
            try:
                ev = json.loads(raw)
            except (TypeError, ValueError):
                continue

            et = ev.get("type", "")
            if et == "error":
                # 注意：error 事件不会终止会话，但对我们来说这次合成已经废了
                stats["error"] = f"{ev.get('code')}: {ev.get('message')}"
                log.warning(f"[realtime_tts] 服务端 error: {stats['error']}")
                # 带内限流/鉴权错误同样打标这把 key（code 可能是 int 或数字字符串）
                try:
                    if int(ev.get("code") or 0) in (429, 401, 403):
                        stepfun_keys.mark_from_status(key, int(ev["code"]))
                except (TypeError, ValueError):
                    pass
                break
            if et == "response.audio.delta":
                if cancelled:
                    continue  # 已取消：丢弃在途音频，不回调
                chunk = base64.b64decode(ev.get("delta", "") or "")
                if not chunk:
                    continue
                if stats["first_audio_ms"] is None:
                    stats["first_audio_ms"] = (time.monotonic() - t0) * 1000
                stats["bytes"] += len(chunk)
                try:
                    on_pcm(chunk)
                except Exception as e:
                    # 下游（转码 / 下行）出错就停，别把连接吊死
                    stats["error"] = f"on_pcm 回调失败: {e}"
                    log.warning(f"[realtime_tts] {stats['error']}")
                    break
            elif et == "response.done":
                # 实测：取消时 status == "incomplete"（不是 cancelled），
                # 且不会发 response.cancelled 事件
                resp = ev.get("response")
                stats["status"] = (resp or {}).get("status") or ev.get("status")
                done = True
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        log.warning(f"[realtime_tts] 合成失败: {stats['error']}")
        # 多 key 路由：握手被 429/401 拒时给这把 key 打标冷却，下一把顶上
        sc = getattr(e, "status_code", None)
        if sc is not None:
            stepfun_keys.mark_from_status(key, sc)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        stats["total_ms"] = (time.monotonic() - t0) * 1000
        stats["ok"] = stats["bytes"] > 0 and not stats["error"]
        if stats["ok"]:
            log.info(
                "[realtime_tts] %d 字 → %d 字节 pcm16 / 首音频 %.0fms / 总 %.0fms%s",
                len(text), stats["bytes"], stats["first_audio_ms"] or 0,
                stats["total_ms"], "（已取消）" if cancelled else "",
            )
    return stats


def synthesize(
    text: str,
    cancel_evt: Optional[threading.Event] = None,
    *,
    voice: Optional[str] = None,
    timeout: float = 30.0,
) -> bytes:
    """便捷封装：返回完整 pcm16 字节（批处理 / 测试用）。

    实时链路请用 :func:`synthesize_streaming` —— 它能在收到首段音频时就开始下行，
    不用等整句合成完。
    """
    buf = bytearray()
    synthesize_streaming(text, buf.extend, cancel_evt, voice=voice, timeout=timeout)
    return bytes(buf)


def stream_sentence_opus(
    text: str,
    on_opus: Callable[[bytes], None],
    cancel_evt: Optional[threading.Event] = None,
    *,
    voice: Optional[str] = None,
    target_sample_rate: int = 24000,
    frame_duration: int = 60,
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    cancel_grace: float = 2.0,
) -> dict:
    """逐字朗读 ``text``，**直接产出 Opus 帧**（pcm16 → Opus 在回调链内完成）。

    这是 FR-13 第 2 步的适配层：把「Realtime 的流式 pcm」与「下行的 Opus 帧」
    接起来，调用方只需要决定"怎么发帧"（pacing + tts 消息），不必关心
    采样率 / 帧长 / 尾部补齐。

    ⚠️ **为什么不能 `synthesize_streaming` + 收齐 pcm 再编码**：
    Realtime 的全部价值就是首音频 ~600ms（576–769ms）。等整句 pcm 收齐再编码，首音频就退化成
    `600ms + 整句生成时间`（长句十几秒），等于白接。这里用
    `PcmOpusStreamEncoder` 在**每个 pcm 分片到达时立刻**吐帧。

    ⚠️ **采样率不匹配时拒绝**（返回 ``ok=False``）：Realtime 输出 24kHz，下行也是
    24kHz ⇒ 不需要重采样。若 ``STEPAUDIO3_SAMPLE_RATE`` 被改成非 24k，宁可降级到
    mp3 也不要发错采样率的音频 —— 设备会放出变调/变速的声音，而且**不会报任何错**。

    ⚠️ **`on_opus` 必须快速返回**：它是在 Realtime 的 recv 循环里被同步调用的，
    慢回调会把连接读端顶住。真实链路请把帧丢进队列，由独立的 pacing 任务发送。

    Returns:
        与 :func:`synthesize_streaming` 相同的 dict，另加 ``frames``（吐出的帧数）。
    """
    src_rate = sample_rate()
    if src_rate != int(target_sample_rate):
        log.warning("[realtime_tts] 采样率不匹配（源 %d / 目标 %d），拒绝合成以免下行变调",
                    src_rate, target_sample_rate)
        return {"ok": False, "bytes": 0, "frames": 0, "cancelled": False,
                "first_audio_ms": None, "total_ms": 0.0, "status": None,
                "error": f"sample_rate mismatch: {src_rate} != {target_sample_rate}"}

    try:
        from app.xiaozhi_codec import PcmOpusStreamEncoder
        enc = PcmOpusStreamEncoder(src_rate, frame_duration)
    except Exception as e:
        return {"ok": False, "bytes": 0, "frames": 0, "cancelled": False,
                "first_audio_ms": None, "total_ms": 0.0, "status": None,
                "error": f"opus encoder 不可用: {e}"}

    counter = {"n": 0}

    def _on_pcm(chunk: bytes) -> None:
        for pkt in enc.feed(chunk):
            counter["n"] += 1
            on_opus(pkt)

    stats = synthesize_streaming(
        text, _on_pcm, cancel_evt, voice=voice,
        timeout=timeout, connect_timeout=connect_timeout,
        cancel_grace=cancel_grace)

    # 尾巴：不足一帧的**真实音频**不能丢（最多 60ms），补静音成整帧再吐。
    # 取消时不补 —— 用户已经在说话了，多吐 60ms 只会推迟"停"。
    if not stats.get("cancelled"):
        try:
            for pkt in enc.flush():
                counter["n"] += 1
                on_opus(pkt)
        except Exception as e:
            log.warning(f"[realtime_tts] flush 尾部失败: {e}")

    stats["frames"] = counter["n"]
    return stats


def pcm_duration_seconds(pcm_bytes: int) -> float:
    """pcm16 单声道字节数 → 秒。用于日志与耗时核对。"""
    return pcm_bytes / _PCM16_BYTES_PER_SAMPLE / sample_rate()


def status() -> dict:
    """给 /api/... 诊断接口用。"""
    return {
        "enabled": is_configured(),
        "model": os.getenv("STEPAUDIO3_RT_MODEL", DEFAULT_MODEL),
        "voice": os.getenv("STEPAUDIO3_VOICE", DEFAULT_VOICE),
        "sample_rate": sample_rate(),
        "endpoint": os.getenv("STEPAUDIO3_RT_URL", DEFAULT_URL),
    }
