"""WebSocket route: bidirectional real-time voice/text with TTS interrupt support.

Extracted from voice_server.py.
"""
import os, sys, json, time, datetime, logging, asyncio, threading
import base64 as _b64enc
import queue as _queue
import requests

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.audio import to_wav, likely_empty_audio, MAX_AUDIO_SIZE
from app.auth import AUTH_TOKEN, _sanitize_text
from app.state import (
    _ws_clients, _ws_session_groups, _ws_client_locations, _interrupt_telemetry,
    _ws_client_count, _ws_clients_lock, _RATE_LOCK, _session_buckets, _RATE_WINDOW,
    enter_continuous_mode, exit_continuous_mode, is_continuous_mode, refresh_continuous_mode,
)
from app.http_helpers import sse_event, SSE_DONE_FRAME
from app.notifications import get_main_loop
from voice_agent import is_low_intent_asr, LOW_INTENT_ASR_REPLY

log = logging.getLogger("magic")

router = APIRouter(tags=["websocket"])

MAX_TEXT_LENGTH = 500
ACK_AFTER_ASR_MESSAGE = "嗯，让我想想"
_WS_STALE_TIMEOUT = 300  # 5分钟无活动视为过期

# Import helpers from conversation module (lazy to avoid import-time issues)
def _get_conv_helpers():
    from app.routes.conversation import (
        _friendly_brain_error, _flush_tts_buffer, _empty_asr_events,
        _low_intent_asr_events, _synthesize_tts_event, _check_session_rate,
    )
    return (_friendly_brain_error, _flush_tts_buffer, _empty_asr_events,
            _low_intent_asr_events, _synthesize_tts_event, _check_session_rate)

def _ws_cleanup_stale():
    now = time.time()
    with _ws_clients_lock:
        stale = [sid for sid, info in list(_ws_clients.items())
                 if now - info.get("last_active", now) > _WS_STALE_TIMEOUT]
    for sid in stale:
        _ws_cleanup_after_disconnect(sid, close_connection=True)
    if stale:
        with _ws_clients_lock:
            remaining = len(_ws_clients)
        log.info(f"[ws] 清理{len(stale)}个过期连接, 剩余{remaining}个")

def start_ws_cleanup():
    """启动WebSocket过期连接清理线程(每60秒)"""
    def _cleanup_loop():
        while True:
            try:
                time.sleep(60)
                _ws_cleanup_stale()
                now = time.time()
                with _RATE_LOCK:
                    expired = [k for k, v in _session_buckets.items()
                               if not v or now - v[-1] > _RATE_WINDOW * 2]
                    for k in expired:
                        del _session_buckets[k]
            except Exception:
                log.exception("[ws] 清理线程异常")
    threading.Thread(target=_cleanup_loop, daemon=True).start()

async def _ws_stream_and_send(ws, ws_id, *, text, asr_text="", session_id, interrupted_reply=""):
    try:
        # 进入连续对话模式（来自 gitee assistant-x-openclaw 的连续对话思路）
        enter_continuous_mode(session_id)
        if asr_text:
            await _ws_broadcast_to_session(ws_id, {"type": "asr", "text": asr_text}, exclude_self=True)
        async for event in _ws_stream_brain(ws_id, text, session_id, interrupted_reply=interrupted_reply):
            with _ws_clients_lock:
                info = _ws_clients.get(ws_id)
                stream_task = info.get("stream_task") if info else None
                should_break = not info or stream_task is not asyncio.current_task()
            if should_break:
                break
            await ws.send_json(event)
            if event.get("type") in ("text", "audio", "done", "error"):
                await _ws_broadcast_to_session(ws_id, event, exclude_self=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"[ws] 流式任务异常 (id={ws_id}): {e}")
        try:
            await ws.send_json({"type": "error", "message": str(e)[:60]})
        except Exception:
            pass
    finally:
        with _ws_clients_lock:
            info = _ws_clients.get(ws_id)
            if info and info.get("stream_task") is asyncio.current_task():
                info["stream_task"] = None
        # 流结束，退出连续对话模式
        exit_continuous_mode(session_id)

def _ws_cancel_stream(ws_id):
    with _ws_clients_lock:
        info = _ws_clients.get(ws_id)
        if not info:
            return
        info["interrupt"] = True
        task = info.get("stream_task")
    if task and not task.done():
        task.cancel()

def _ws_join_session(ws_id, session_id):
    with _ws_clients_lock:
        info = _ws_clients.get(ws_id)
        if not info:
            return
        info["session_id"] = session_id
        if session_id not in _ws_session_groups:
            _ws_session_groups[session_id] = []
        if ws_id not in _ws_session_groups[session_id]:
            _ws_session_groups[session_id].append(ws_id)
            peers = [p for p in _ws_session_groups[session_id] if p != ws_id]
            if peers:
                log.info(f"[ws] 会话 {session_id[:8]} 跨终端同步: {len(peers)+1}个终端")

async def _ws_broadcast_to_session(ws_id, event, exclude_self=True):
    with _ws_clients_lock:
        info = _ws_clients.get(ws_id)
        if not info:
            return
        session_id = info.get("session_id", "default")
        peers = list(_ws_session_groups.get(session_id, []))
        peer_infos = []
        for pid in peers:
            if exclude_self and pid == ws_id:
                continue
            pinfo = _ws_clients.get(pid)
            if pinfo:
                peer_infos.append(pinfo)
    for pinfo in peer_infos:
        try:
            await pinfo["ws"].send_json(event)
        except Exception:
            pass

async def _ws_reverse_geocode(lat, lng):
    amap_key = os.getenv("AMAP_KEY", "")
    if not amap_key:
        return f"经纬度: {lat:.4f}, {lng:.4f}"
    try:
        r = await asyncio.to_thread(
            requests.get,
            f"https://restapi.amap.com/v3/geocode/regeo?key={amap_key}&location={lng},{lat}&extensions=base",
            timeout=5
        )
        data = r.json()
        addr = data.get("regeocode", {}).get("formatted_address", "")
        if addr:
            return f"{addr} (经纬度: {lat:.4f}, {lng:.4f})"
        return f"经纬度: {lat:.4f}, {lng:.4f}"
    except Exception:
        return f"经纬度: {lat:.4f}, {lng:.4f}"

def _ws_cleanup_after_disconnect(ws_id, close_connection=False):
    with _ws_clients_lock:
        info = _ws_clients.pop(ws_id, None)
        task = info.get("stream_task") if info else None
        session_id = info.get("session_id", "default") if info else "default"
    if not info:
        return
    _interrupt_telemetry.discard_pending(ws_id)
    if task and not task.done():
        task.cancel()
    with _ws_clients_lock:
        if session_id in _ws_session_groups:
            try:
                _ws_session_groups[session_id].remove(ws_id)
            except ValueError:
                pass
            if not _ws_session_groups[session_id]:
                del _ws_session_groups[session_id]
        _ws_client_locations.pop(ws_id, None)
    if close_connection:
        ws = info.get("ws")
        _main_loop = get_main_loop()
        if ws and _main_loop and not _main_loop.is_closed():
            try:
                _main_loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.close()))
            except RuntimeError:
                pass

async def _ws_stream_brain(ws_id, text, session_id="default", interrupted_reply=""):
    from voice_agent import brain_stream_sentences
    from agent.tts_player import TTSParallelPlayer
    (_friendly_brain_error, _flush_tts_buffer, _empty_asr_events,
     _low_intent_asr_events, _synthesize_tts_event, _check_session_rate) = _get_conv_helpers()

    q = _queue.Queue()

    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(text, session_id, interrupted_reply=interrupted_reply):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", _friendly_brain_error(e), None))
        finally:
            q.put(("done", None, None))

    threading.Thread(target=brain_worker, daemon=True).start()
    # TTS 并行合成 + 顺序播放 (来自 gitee assistant-x-openclaw)
    player = TTSParallelPlayer(max_workers=2)
    tts_buffer = ""
    tts_failed = False
    first_audio_sent = False
    total_wait = 0
    pending_seqs: list[int] = []  # seq numbers waiting to be played

    while True:
        with _ws_clients_lock:
            if _ws_clients.get(ws_id, {}).get("interrupt"):
                break
        # Event-driven: await async result instead of polling
        player_result = await player.async_get_next(timeout=0.1)
        try:
            item = q.get_nowait()
            total_wait = 0
        except _queue.Empty:
            if total_wait >= 120:
                yield {"type": "error", "message": "思考超时"}
                yield {"type": "done"}
                break
            if not player_result:
                await asyncio.sleep(0.1)
                total_wait += 0.1
            continue
        etype, sentence, full_reply = item
        if etype == "done":
            if tts_buffer.strip() and not tts_failed:
                seq = player.submit(tts_buffer, _flush_tts_buffer)
                if seq >= 0:
                    pending_seqs.append(seq)
                # Drain remaining results
                while pending_seqs:
                    result = await player.async_get_next(timeout=1.0)
                    if result is None:
                        break
                    _, txt, audio_b64, warning = result
                    if warning:
                        if not tts_failed:
                            tts_failed = True
                            yield warning
                    elif audio_b64:
                        yield {"type": "audio", "data": audio_b64}
                    if result[0] in pending_seqs:
                        pending_seqs.remove(result[0])
            yield {"type": "done"}
            break
        elif etype == "error":
            yield {"type": "error", "message": sentence}
            yield {"type": "done"}
            break
        elif etype == "sentence":
            with _ws_clients_lock:
                if _ws_clients.get(ws_id, {}).get("interrupt"):
                    break
            yield {"type": "text", "text": sentence}
            if sentence and len(sentence) >= 2 and not sentence.startswith('__MUSIC__') and sentence != '__MUSIC_STOP__':
                tts_buffer = (tts_buffer + "，" + sentence) if tts_buffer else sentence
                if not first_audio_sent:
                    if tts_failed:
                        tts_buffer = ""
                        first_audio_sent = True
                        continue
                    # 提交到并行 TTS 播放器
                    seq = player.submit(tts_buffer, _flush_tts_buffer)
                    if seq >= 0:
                        pending_seqs.append(seq)
                    tts_buffer = ""
                    first_audio_sent = True
                    # 等待并播放第一个结果
                    result = await player.async_get_next(timeout=1.0)
                    if result:
                        _, txt, audio_b64, warning = result
                        if warning:
                            if not tts_failed:
                                tts_failed = True
                                yield warning
                        elif audio_b64:
                            yield {"type": "audio", "data": audio_b64}
                        if result[0] in pending_seqs:
                            pending_seqs.remove(result[0])
        if player_result:
            seq, txt, audio_b64, warning = player_result
            if warning:
                if not tts_failed:
                    tts_failed = True
                    yield warning
            elif audio_b64:
                yield {"type": "audio", "data": audio_b64}
            if seq in pending_seqs:
                pending_seqs.remove(seq)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    from app.audit_log import audit_log
    audit_log("ws:connect", input_data="websocket", action="connect")
    await ws.accept()
    if AUTH_TOKEN:
        ci = ws.client.host if ws.client else ""
        if ci not in ("127.0.0.1", "::1", ""):
            tk = ws.query_params.get("token", "")
            if tk != AUTH_TOKEN:
                await ws.close(code=4001, reason="未授权")
                return
    ws_id = id(ws)
    with _ws_clients_lock:
        _ws_clients[ws_id] = {"ws": ws, "interrupt": False, "last_active": time.time(), "stream_task": None}
        client_count = len(_ws_clients)
    log.info(f"[ws] 客户端已连接 (id={ws_id}), 共{client_count}个连接")
    await ws.send_json({"type": "connect", "text": "Charlie已连接",
                        "time": datetime.datetime.now().isoformat()})
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=120)
                with _ws_clients_lock:
                    _ws_clients[ws_id]["last_active"] = time.time()
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            try:
                data = json.loads(msg)
                if data.get('type') == 'clipboard':
                    text = data.get('text', '')
                    log.info(f"[ws] 剪贴板: {text[:50]}...")
                    continue
                if data.get('type') == 'discover':
                    with _ws_clients_lock:
                        peers = len(_ws_clients)
                    await ws.send_json({"type": "discover_ack", "peers": peers})
                    continue
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "消息格式错误,需要JSON"})
                continue
            mtype = data.get("type", "")
            if mtype == "ping":
                await ws.send_json({"type": "pong", "time": datetime.datetime.now().isoformat()})
                continue
            if mtype == "interrupt":
                interrupted_reply = data.get("interrupted_reply", "")
                _ws_cancel_stream(ws_id)
                _interrupt_telemetry.record(ws_id, interrupted_reply)
                log.info(f"[ws] 客户端请求打断TTS (id={ws_id}), 被打断回复: {interrupted_reply[:60]}")
                await ws.send_json({"type": "interrupted"})
                continue
            if mtype == "location":
                lat = data.get("lat")
                lng = data.get("lng")
                acc = data.get("accuracy", 0)
                if lat is None or lng is None:
                    await ws.send_json({"type": "error", "message": "缺少经纬度"})
                    continue
                with _ws_clients_lock:
                    _ws_client_locations[ws_id] = {
                        "lat": lat, "lng": lng, "accuracy": acc,
                        "time": datetime.datetime.now().isoformat()
                    }
                try:
                    from voice_agent import update_user_state
                    update_user_state(location=(lat, lng))
                except Exception:
                    pass
                addr = await _ws_reverse_geocode(lat, lng)
                log.info(f"[ws] 位置上报 (id={ws_id}): {addr}")
                await ws.send_json({"type": "location_ack", "address": addr, "lat": lat, "lng": lng})
                continue
            if mtype == "text":
                text = _sanitize_text(data.get("message", ""), MAX_TEXT_LENGTH)
                session_id = data.get("session_id", "default")
                if not text:
                    await ws.send_json({"type": "error", "message": "消息不能为空"})
                    continue
                if len(text) > MAX_TEXT_LENGTH:
                    await ws.send_json({"type": "error", "message": f"输入过长(上限{MAX_TEXT_LENGTH}字)"})
                    continue
                _ws_join_session(ws_id, session_id)
                (_, _, _, _, _, _check_session_rate) = _get_conv_helpers()
                allowed, _, _ = _check_session_rate(session_id)
                if not allowed:
                    await ws.send_json({"type": "error", "message": "请求过于频繁，请稍后再试"})
                    continue
                interrupted_reply = _interrupt_telemetry.record_follow_up(ws_id, text, "text")
                _ws_cancel_stream(ws_id)
                with _ws_clients_lock:
                    _ws_clients[ws_id]["interrupt"] = False
                # 连续对话模式：如果处于连续模式，发送提示
                if refresh_continuous_mode(session_id):
                    log.debug(f"[ws] 连续对话模式续期 (session={session_id[:8]})")
                    await ws.send_json({"type": "continuous_mode", "message": "继续对话"})
                else:
                    enter_continuous_mode(session_id)
                log.info(f"[ws] 文字对话: {text[:40]} (session={session_id[:8]})")
                task = asyncio.create_task(_ws_stream_and_send(ws, ws_id, text=text, session_id=session_id, interrupted_reply=interrupted_reply))
                with _ws_clients_lock:
                    _ws_clients[ws_id]["stream_task"] = task
                continue
            if mtype == "audio":
                audio_b64 = data.get("data", "")
                fmt = data.get("format", "wav")
                if not audio_b64:
                    await ws.send_json({"type": "error", "message": "音频数据为空"})
                    continue
                try:
                    raw = _b64enc.b64decode(audio_b64)
                except Exception:
                    await ws.send_json({"type": "error", "message": "base64解码失败"})
                    continue
                if len(raw) > MAX_AUDIO_SIZE:
                    await ws.send_json({"type": "error", "message": "音频过大"})
                    continue
                ws_session_id = data.get("session_id", "default")
                _ws_join_session(ws_id, ws_session_id)
                (_, _, _, _, _, _check_session_rate) = _get_conv_helpers()
                allowed, _, _ = _check_session_rate(ws_session_id)
                if not allowed:
                    await ws.send_json({"type": "error", "message": "请求过于频繁，请稍后再试"})
                    continue
                _ws_cancel_stream(ws_id)
                with _ws_clients_lock:
                    _ws_clients[ws_id]["interrupt"] = False
                # 连续对话模式
                if refresh_continuous_mode(ws_session_id):
                    log.debug(f"[ws] 连续对话模式续期 (session={ws_session_id[:8]})")
                    await ws.send_json({"type": "continuous_mode", "message": "继续对话"})
                else:
                    enter_continuous_mode(ws_session_id)
                log.info(f"[ws] 语音对话: {len(raw)}字节, 格式={fmt}")
                wav = to_wav(raw, fmt)
                if likely_empty_audio(wav):
                    log.info(f"[ws] 本地判定为长静音，短路ASR、大脑和TTS (id={ws_id})")
                    (_, _, _empty_asr_events, _, _, _) = _get_conv_helpers()
                    for event in _empty_asr_events(False):
                        await ws.send_json(event)
                    continue
                from voice_agent import asr
                try:
                    asr_text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "error", "message": "语音识别超时"})
                    continue
                if not asr_text:
                    log.info(f"[ws] ASR为空，短路大脑和TTS (id={ws_id})")
                    (_, _, _empty_asr_events, _, _, _) = _get_conv_helpers()
                    for event in _empty_asr_events(False):
                        await ws.send_json(event)
                    continue
                from voice_agent import is_garbled_asr as _is_garbled_asr
                if is_low_intent_asr(asr_text):
                    log.info(f"[ws] ASR为低意图语气词，短路大脑和TTS (id={ws_id})")
                    (_, _, _, _low_intent_asr_events, _, _) = _get_conv_helpers()
                    for event in _low_intent_asr_events(asr_text, False):
                        await ws.send_json(event)
                    continue
                if _is_garbled_asr(asr_text):
                    log.info(f"[ws] ASR为乱码/碎片，短路大脑和TTS (id={ws_id})")
                    (_, _, _empty_asr_events, _, _, _) = _get_conv_helpers()
                    for event in _empty_asr_events(False):
                        await ws.send_json(event)
                    continue
                asr_event = {"type": "asr", "text": asr_text}
                await ws.send_json(asr_event)
                await ws.send_json({"type": "ack", "message": ACK_AFTER_ASR_MESSAGE})
                interrupted_reply = _interrupt_telemetry.record_follow_up(ws_id, asr_text, "asr")
                task = asyncio.create_task(_ws_stream_and_send(ws, ws_id, text=asr_text, asr_text=asr_text, session_id=ws_session_id, interrupted_reply=interrupted_reply))
                with _ws_clients_lock:
                    _ws_clients[ws_id]["stream_task"] = task
                continue
            if mtype == "audio_stream":
                # Wake-during-TTS: 客户端在 TTS 播放期间持续发送音频
                audio_b64 = data.get("data", "")
                fmt = data.get("format", "wav")
                if not audio_b64:
                    continue
                try:
                    raw = _b64enc.b64decode(audio_b64)
                except Exception:
                    continue
                if len(raw) > MAX_AUDIO_SIZE:
                    continue
                # 将音频存入 audio_queue 供唤醒词检测
                try:
                    from agent.audio_queue import get_audio_queue
                    aq = get_audio_queue()
                    wav = to_wav(raw, fmt)
                    # 提取 PCM 数据（去掉 WAV header）
                    if len(wav) > 44:
                        pcm = wav[44:]
                    else:
                        pcm = wav
                    aq.push(pcm)
                except Exception:
                    pass
                continue
            await ws.send_json({"type": "error", "message": f"未知消息类型: {mtype}"})
    except WebSocketDisconnect:
        log.info(f"[ws] 客户端断开 (id={ws_id})")
    except Exception as e:
        log.error(f"[ws] 异常 (id={ws_id}): {e}")
    finally:
        _ws_cleanup_after_disconnect(ws_id)
        with _ws_clients_lock:
            remaining = len(_ws_clients)
        log.info(f"[ws] 连接清理完成 (id={ws_id}), 剩余{remaining}个")
