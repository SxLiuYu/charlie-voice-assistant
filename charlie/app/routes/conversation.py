"""Conversation routes: voice/chat/asr/tts/streaming/export/search.

Extracted from voice_server.py. Uses APIRouter so voice_server.py can mount it.
"""
import os, sys, json, time, datetime, logging, asyncio, subprocess
import base64 as _b64enc
import queue as _queue

from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import Response, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.audio import likely_empty_audio, to_wav, _wav_to_mp3, MAX_AUDIO_SIZE
from app.auth import _sanitize_text
from app.http_helpers import sse_event, SSE_DONE_FRAME, SSE_HEARTBEAT_FRAME
from app.state import (
    _metrics, _RATE_PER_SESSION, _RATE_LOCK, _session_buckets, _RATE_WINDOW,
)
from voice_agent import LOW_INTENT_ASR_REPLY, is_low_intent_asr

log = logging.getLogger("magic")

router = APIRouter(tags=["conversation"])

MAX_TEXT_LENGTH = 500
ACK_AFTER_ASR_MESSAGE = "嗯，让我想想"
_TTS_BATCH_SIZE = 30
TTS_DEGRADED_MESSAGE = "语音服务繁忙，本轮先显示文字回复。"
TTS_UNCONFIGURED_MESSAGE = "语音合成未配置，已显示文字回复。可在设置中填入百度语音 Key 开启语音。"
BRAIN_BUSY_MESSAGE = "大脑服务繁忙，请稍后再试。"

# ===== Pydantic models =====
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    session_id: str = Field(default="default")

class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

# ===== Session rate limit =====
def _check_session_rate(session_id: str) -> tuple:
    if not session_id or session_id == "default":
        return True, _RATE_PER_SESSION, 0
    now = time.time()
    with _RATE_LOCK:
        bucket = _session_buckets.setdefault(session_id, [])
        bucket[:] = [ts for ts in bucket if now - ts < _RATE_WINDOW]
        if len(bucket) >= _RATE_PER_SESSION:
            retry = int(_RATE_WINDOW - (now - bucket[0])) + 1
            return False, 0, retry
        bucket.append(now)
        return True, _RATE_PER_SESSION - len(bucket), 0

# ===== Streaming helpers =====
def _friendly_brain_error(error: Exception) -> str:
    from utils import sanitize_error
    raw_message = str(error)
    lowered = raw_message.lower()
    if "429" in raw_message or "too many requests" in lowered or "rate limit" in lowered or "限流" in raw_message:
        log.warning(f"大脑服务限流，返回友好提示: {raw_message}")
        return BRAIN_BUSY_MESSAGE
    log.error(f"大脑流式生成失败: {raw_message}")
    return sanitize_error(raw_message)

def _flush_tts_buffer(tts_buffer: str) -> str:
    from voice_agent import _tts_cleaned_to_mp3
    if not tts_buffer or len(tts_buffer) < 2:
        return ""
    mp3 = _tts_cleaned_to_mp3(tts_buffer)
    if not mp3 or len(mp3) < 100:
        return ""
    return _b64enc.b64encode(mp3).decode()

def _empty_asr_events(as_event_stream: bool):
    from voice_agent import EMPTY_ASR_REPLY
    text_event = {"type": "text", "text": EMPTY_ASR_REPLY}
    done_event = {"type": "done"}
    if as_event_stream:
        yield sse_event(text_event)
        yield sse_event(done_event)
    else:
        yield text_event
        yield done_event

def _low_intent_asr_events(asr_text: str, as_event_stream: bool):
    asr_event = {"type": "asr", "text": asr_text}
    text_event = {"type": "text", "text": LOW_INTENT_ASR_REPLY}
    done_event = {"type": "done"}
    if as_event_stream:
        yield sse_event(asr_event)
        yield sse_event(text_event)
        yield sse_event(done_event)
    else:
        yield asr_event
        yield text_event
        yield done_event

async def _synthesize_tts_event(tts_buffer: str):
    if not tts_buffer or len(tts_buffer) < 2:
        return "", None
    try:
        audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
        return audio_b64, None
    except Exception as e:
        log.warning(f"流式TTS失败，降级为文字: {e}")
        try:
            from agent import asr_tts as _at
            configured = bool(_at.BAIDU_APP_ID and _at.BAIDU_API_KEY and _at.BAIDU_SECRET_KEY)
        except Exception:
            configured = True
        msg = TTS_DEGRADED_MESSAGE if configured else TTS_UNCONFIGURED_MESSAGE
        return "", {"type": "warning", "message": msg}

async def _stream_brain_tts(text: str, asr_text: str = "", session_id: str = "default"):
    """流式大脑+TTS生成器(SSE事件流) — 并行版。"""
    from voice_agent import brain_stream_sentences
    q = _queue.Queue()
    tts_q = _queue.Queue()
    brain_thread = None

    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(text, session_id):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", _friendly_brain_error(e), None))
        finally:
            q.put(("done", None, None))

    if asr_text:
        yield sse_event({"type": "asr", "text": asr_text})
        yield sse_event({"type": "ack", "message": ACK_AFTER_ASR_MESSAGE})

    import threading
    brain_thread = threading.Thread(target=brain_worker, daemon=True)
    brain_thread.start()

    tts_buffer = ""
    tts_failed = False
    first_audio_sent = False
    brain_done = False
    pending_tts = []
    total_wait = 0
    HEARTBEAT_INTERVAL = 0.15
    MAX_WAIT = 120

    def _submit_tts(text_to_synth: str):
        try:
            if not text_to_synth or len(text_to_synth) < 2:
                tts_q.put(("result", text_to_synth, "", None))
                return
            audio_b64 = _flush_tts_buffer(text_to_synth)
            tts_q.put(("result", text_to_synth, audio_b64, None))
        except Exception as e:
            try:
                from agent import asr_tts as _at
                configured = bool(_at.BAIDU_APP_ID and _at.BAIDU_API_KEY and _at.BAIDU_SECRET_KEY)
            except Exception:
                configured = True
            msg = TTS_DEGRADED_MESSAGE if configured else TTS_UNCONFIGURED_MESSAGE
            tts_q.put(("error", text_to_synth, None, {"type": "warning", "message": msg}))

    while True:
        brain_item = None
        try:
            brain_item = q.get_nowait()
            total_wait = 0
        except _queue.Empty:
            pass
        tts_result = None
        try:
            tts_result = tts_q.get_nowait()
        except _queue.Empty:
            pass
        if brain_item:
            etype, sentence, full_reply = brain_item
            if etype == "done":
                brain_done = True
                if not pending_tts and not tts_buffer.strip():
                    yield SSE_DONE_FRAME
                    break
            elif etype == "error":
                yield sse_event({"type": "error", "message": sentence})
                yield SSE_DONE_FRAME
                break
            elif etype == "sentence":
                yield sse_event({"type": "text", "text": sentence})
                if sentence and len(sentence) >= 2 and not sentence.startswith("__MUSIC__") and sentence != "__MUSIC_STOP__":
                    tts_buffer = (tts_buffer + "， " + sentence) if tts_buffer else sentence
                    should_flush = not first_audio_sent
                    if should_flush and not tts_failed:
                        buf = tts_buffer
                        tts_buffer = ""
                        first_audio_sent = True
                        pending_tts.append(buf)
                        threading.Thread(target=_submit_tts, args=(buf,), daemon=True).start()
        if tts_result:
            rtype, txt, audio_b64, warning = tts_result
            if rtype == "result" and audio_b64:
                yield sse_event({"type": "audio", "audio": audio_b64})
                if txt in pending_tts:
                    pending_tts.remove(txt)
            elif rtype == "error":
                if txt in pending_tts:
                    pending_tts.remove(txt)
                if not tts_failed:
                    tts_failed = True
                    yield sse_event(warning)
        if brain_done and not pending_tts:
            if tts_buffer.strip() and not tts_failed:
                threading.Thread(target=_submit_tts, args=(tts_buffer,), daemon=True).start()
                pending_tts.append(tts_buffer)
                tts_buffer = ""
            elif not pending_tts:
                yield SSE_DONE_FRAME
                break
        if not brain_item and not tts_result:
            if total_wait >= MAX_WAIT:
                yield sse_event({"type": "error", "message": "思考超时"})
                yield SSE_DONE_FRAME
                break
            yield SSE_HEARTBEAT_FRAME
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            total_wait += HEARTBEAT_INTERVAL

# ===== Routes =====

@router.post("/api/voice")
async def voice_api(file: UploadFile = File(...)):
    import time as _t
    from app.audit_log import audit_log
    _start = _t.time()
    data = await file.read()
    ext = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    audit_log("api:voice", input_data=f"audio={len(data)}bytes fmt={ext}",
              action="start", session_id="voice")
    if len(data) > MAX_AUDIO_SIZE:
        return JSONResponse({"error": f"音频过大({len(data)//1024}KB), 上限{MAX_AUDIO_SIZE//1024//1024}MB"}, status_code=413)
    log.info(f"/api/voice 收到音频: {len(data)}字节, 格式={ext}")
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        from voice_agent import EMPTY_ASR_REPLY, EMPTY_ASR_TEXT
        return {"text": EMPTY_ASR_TEXT, "reply": EMPTY_ASR_REPLY, "audio": "", "format": "mp3", "degraded": True}
    from voice_agent import voice_loop
    try:
        text, reply, audio_out = await asyncio.wait_for(asyncio.to_thread(voice_loop, wav, "wav"), timeout=60)
        mp3_out = _wav_to_mp3(audio_out)
        degraded = not mp3_out
        audit_log("api:voice", input_data=text, output_data=reply[:100],
                  action="complete", session_id="voice", duration_ms=(_t.time()-_start)*1000)
        return {"text": text, "reply": reply, "audio": _b64enc.b64encode(mp3_out).decode(), "format": "mp3", "degraded": degraded}
    except asyncio.TimeoutError:
        return JSONResponse({"error": "处理超时，请重试"}, status_code=504)
    except Exception as e:
        from utils import sanitize_error
        return JSONResponse({"error": sanitize_error(str(e))}, status_code=500)

@router.post("/api/chat/stream")
async def chat_stream_api(req: ChatRequest):
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    session_id = req.session_id
    allowed, remaining, retry_after = _check_session_rate(session_id)
    if not allowed:
        return JSONResponse({"error": f"请求过于频繁, 请{retry_after}秒后重试"}, status_code=429, headers={"Retry-After": str(retry_after)})
    return StreamingResponse(_stream_brain_tts(text, session_id=session_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/api/voice/stream")
async def voice_stream_api(request: Request, file: UploadFile = File(...), session_id: str = "default"):
    data = await file.read()
    ext = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    if len(data) > MAX_AUDIO_SIZE:
        return JSONResponse({"error": f"音频过大({len(data)//1024}KB), 上限{MAX_AUDIO_SIZE//1024//1024}MB"}, status_code=413)
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    from voice_agent import asr
    try:
        asr_text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "语音识别超时"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": f"识别失败: {e}"}, status_code=500)
    if not asr_text:
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if is_low_intent_asr(asr_text):
        return StreamingResponse(_low_intent_asr_events(asr_text, True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    from voice_agent import is_garbled_asr
    if is_garbled_asr(asr_text):
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(_stream_brain_tts(asr_text, asr_text, session_id=session_id),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/api/chat")
async def chat_api(req: ChatRequest):
    import time as _t
    from app.audit_log import audit_log
    _start = _t.time()
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    allowed, remaining, retry_after = _check_session_rate(req.session_id)
    if not allowed:
        audit_log("api:chat", input_data=text, success=False, error="rate_limited",
                  action="429", session_id=req.session_id, duration_ms=(_t.time()-_start)*1000)
        return JSONResponse({"error": f"请求过于频繁, 请{retry_after}秒后重试"}, status_code=429, headers={"Retry-After": str(retry_after)})
    from voice_agent import brain
    try:
        reply = await asyncio.wait_for(asyncio.to_thread(brain, text, req.session_id), timeout=60)
        audit_log("api:chat", input_data=text, output_data=reply,
                  action="success", session_id=req.session_id, duration_ms=(_t.time()-_start)*1000)
        return {"reply": reply}
    except asyncio.TimeoutError:
        audit_log("api:chat", input_data=text, success=False, error="timeout",
                  action="504", session_id=req.session_id, duration_ms=(_t.time()-_start)*1000)
        return JSONResponse({"error": "思考超时，请重试"}, status_code=504)
    except Exception as e:
        audit_log("api:chat", input_data=text, success=False, error=str(e),
                  action="degraded", session_id=req.session_id, duration_ms=(_t.time()-_start)*1000)
        return {"reply": "抱歉，我现在有点忙不过来，请稍等一下再试。", "degraded": True}

@router.post("/api/reset")
async def reset_conversation(session_id: str = "default"):
    from voice_agent import reset_history
    reset_history(session_id)
    return {"ok": True, "message": "对话已重置", "session_id": session_id}

@router.get("/api/conversation")
async def get_conversation(page: int = 1, limit: int = 50, session_id: str = "default"):
    from voice_agent import _history_snapshot
    hist = _history_snapshot(session_id)
    total = len(hist)
    start = max(0, (page - 1) * limit)
    end = start + limit
    items = hist[start:end]
    return {"history": items, "count": len(items), "total": total, "page": page, "limit": limit, "has_more": end < total}

@router.post("/api/tts")
async def tts_api(req: TTSRequest):
    text = _sanitize_text(req.text, MAX_TEXT_LENGTH)
    from voice_agent import tts_to_mp3
    try:
        audio = await asyncio.wait_for(asyncio.to_thread(tts_to_mp3, text), timeout=30)
        if not audio:
            return JSONResponse({"error": "TTS生成失败"}, status_code=500)
        return Response(content=audio, media_type="audio/mpeg")
    except asyncio.TimeoutError:
        return JSONResponse({"error": "TTS超时"}, status_code=504)
    except Exception:
        return JSONResponse({"error": "TTS服务繁忙，请稍后再试"}, status_code=503)

@router.post("/api/asr")
async def asr_api(file: UploadFile = File(...)):
    data = await file.read()
    ext = (file.filename or "audio.wav").rsplit(".", 1)[-1].lower()
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        return {"text": ""}
    from voice_agent import asr
    try:
        text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
        return {"text": text}
    except asyncio.TimeoutError:
        return JSONResponse({"error": "ASR超时"}, status_code=504)

# ===== Vosk wake word detection =====
_VOSK_MODEL = None
_VOSK_VARIONS = ["charlie", "charley", "charls", "charles", "チャーリー", "查理", "查里", "chali", "chali", "charli",
                "查理", "查莉", "查利", "查里", "茶理", "嘞查嘞", "嘞查", "查嘞"]
_VOSK_WAKE_WORDS = [w.strip().lower() for w in
                    os.getenv("WAKE_WORDS", ",".join(_VOSK_VARIONS)).split(",")
                    if w.strip()] or _VOSK_VARIONS

def _get_vosk_model():
    global _VOSK_MODEL
    if _VOSK_MODEL is None:
        try:
            from vosk import Model
            model_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web", "vosk", "vosk-model-small-en-us-0.15")
            if os.path.exists(model_path):
                _VOSK_MODEL = Model(model_path)
                log.info("Vosk 英文唤醒词模型已加载")
        except Exception as e:
            log.warning(f"Vosk 模型加载失败(不影响正常使用): {e}")
    return _VOSK_MODEL

@router.post("/api/wakecheck")
async def wakecheck_api(file: UploadFile = File(...)):
    import wave, io, json as json_mod
    data = await file.read()
    if len(data) < 1000:
        return {"wake": False, "text": ""}
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=data, capture_output=True, timeout=15)
        wav = r.stdout if r.returncode == 0 and r.stdout and len(r.stdout) > 100 else data
    except Exception:
        wav = data
    model = _get_vosk_model()
    if model is None:
        from voice_agent import asr
        try:
            text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=15)
            text_lower = text.lower().strip()
            matched = any(w in text_lower for w in _VOSK_WAKE_WORDS)
            return {"wake": matched, "text": text}
        except Exception:
            return {"wake": False, "text": ""}
    try:
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, 16000)
        wf = wave.open(io.BytesIO(wav), 'rb')
        chunk_size = 4000
        text = ""
        while True:
            frames = wf.readframes(chunk_size)
            if len(frames) == 0:
                break
            if rec.AcceptWaveform(frames):
                result = json_mod.loads(rec.Result())
                t = result.get("text", "")
                if t:
                    text = t
                break
        final = json_mod.loads(rec.FinalResult())
        if final.get("text"):
            text = final["text"]
        text = text.lower()
        matched = any(w in text for w in _VOSK_WAKE_WORDS)
        return {"wake": matched, "text": text}
    except Exception as e:
        log.warning(f"/api/wakecheck 异常: {e}")
        return {"wake": False, "text": ""}

@router.get("/api/export")
async def export_conversation(format: str = "txt", session_id: str = "default", from_date: str | None = None, to_date: str | None = None):
    from voice_agent import _history_snapshot
    hist = _history_snapshot(session_id)
    start_date = None
    end_date = None
    try:
        if from_date:
            start_date = datetime.date.fromisoformat(from_date)
        if to_date:
            end_date = datetime.date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    filtered = []
    for message in hist:
        raw_ts = message.get("ts")
        if not raw_ts:
            if not start_date and not end_date:
                filtered.append(message)
            continue
        try:
            message_date = datetime.datetime.fromisoformat(str(raw_ts)).date()
        except ValueError:
            continue
        if start_date and message_date < start_date:
            continue
        if end_date and message_date > end_date:
            continue
        filtered.append(message)
    hist = filtered
    if not hist:
        return Response(content="(对话历史为空)".encode("utf-8"), media_type="text/plain")
    if format == "json":
        return Response(content=json.dumps(hist, ensure_ascii=False, indent=2).encode("utf-8"),
                       media_type="application/json",
                       headers={"Content-Disposition": "attachment; filename=conversation.json"})
    if format in ("markdown", "md"):
        lines = ["# Charlie · 对话记录\n"]
        for m in hist:
            role = "🙋 我" if m.get("role") == "user" else "🤖 Charlie"
            lines.append(f"### {role}\n\n{m.get('content', '')}\n")
        lines.append(f"\n---\n*共{len(hist)}条消息 · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}导出*")
        return Response(content="\n".join(lines).encode("utf-8"), media_type="text/markdown",
                       headers={"Content-Disposition": "attachment; filename=conversation.md"})
    lines = []
    for m in hist:
        role = "我" if m.get("role") == "user" else "Charlie"
        ts = str(m.get("ts", "") or "")[:19].replace("T", " ") if m.get("ts") else ""
        ts_prefix = f"[{ts}] " if ts else ""
        lines.append(f"[{role}] {ts_prefix}{m.get('content', '')}")
    return Response(content="\n\n".join(lines).encode("utf-8"), media_type="text/plain",
                   headers={"Content-Disposition": "attachment; filename=conversation.txt"})

@router.get("/api/search")
async def search_conversation(q: str = "", session_id: str = "default", limit: int = 20, offset: int = 0):
    if not q:
        return JSONResponse({"error": "请提供搜索关键词?q=xxx"}, status_code=400)
    from voice_agent import _searchable_history
    all_history = _searchable_history(session_id)
    results = []
    for i, m in enumerate(all_history):
        content = m.get("content", "")
        content_lower = content.lower()
        q_lower = q.lower()
        if q_lower not in content_lower:
            continue
        role = "我" if m.get("role") == "user" else "Charlie"
        score = 1
        if content_lower == q_lower:
            score = 100
        elif q_lower in content_lower:
            count = content_lower.count(q_lower)
            score = min(50, 10 * count)
            if content_lower.startswith(q_lower):
                score += 20
        idx = content_lower.find(q_lower)
        start = max(0, idx - 25)
        end = min(len(content), idx + len(q) + 25)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""
        ctx = prefix + content[start:idx] + "[" + content[idx:idx+len(q)] + "]" + content[idx+len(q):end] + suffix
        ts = str(m.get("ts", "") or "")[:19].replace("T", " ") if m.get("ts") else ""
        results.append({"role": role, "context": ctx, "full": content[:200], "score": score, "index": i, "timestamp": ts})
    results.sort(key=lambda r: r["score"], reverse=True)
    total = len(results)
    paginated = results[offset:offset+limit]
    return {"query": q, "count": len(paginated), "total": total, "offset": offset, "limit": limit, "results": paginated}

@router.post("/api/brain/restart")
async def restart_brain_api():
    from voice_agent import restart_brain
    msg = restart_brain()
    log.info(f"[brain] 手动重启: {msg}")
    return {"ok": True, "message": msg}
