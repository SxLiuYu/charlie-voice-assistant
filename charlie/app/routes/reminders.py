"""Reminder/notification/SSE/lan-info/OTA routes.

Extracted from voice_server.py.
"""
import os, sys, json, time, datetime, logging, asyncio

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.http_helpers import (
    json_response, file_not_modified_response, file_etag_token,
    sse_event, SSE_EVENT_HEARTBEAT_FRAME,
)
from app.state import (
    register_sse_client, unregister_sse_client, sse_client_count,
)
from app.notifications import drain_notifications
from app.auth import _sanitize_text
from app.config import http_port, https_port
from app.reminders import (
    REMINDERS_FILE, _load_reminders, append_reminder, complete_reminder,
)

log = logging.getLogger("magic")

router = APIRouter(tags=["reminders"])

MAX_TEXT_LENGTH = 500

class ReminderRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)
    time: str = Field(default="")
    repeat: str = Field(default="")

def _get_lan_ip() -> str | None:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None

@router.get("/api/reminders")
async def list_reminders(request: Request):
    cached = file_not_modified_response(request, REMINDERS_FILE, "reminders")
    if cached is not None:
        return cached
    reminders_token = file_etag_token(REMINDERS_FILE, "reminders")
    data = _load_reminders()
    reminders_token_after_load = file_etag_token(REMINDERS_FILE, "reminders")
    pending = [r for r in data if not r.get("done")]
    return json_response(
        request,
        {"total": len(data), "pending": len(pending), "reminders": data},
        etag_token=reminders_token if reminders_token == reminders_token_after_load else None,
    )

@router.post("/api/reminders")
async def add_reminder(req: ReminderRequest):
    text = _sanitize_text(req.text, 200)
    time_str = _sanitize_text(req.time, 50)
    repeat = _sanitize_text(req.repeat, 20) if req.repeat else ""
    due = None
    if time_str:
        from utils import parse_time_str
        due = parse_time_str(time_str)
    if repeat:
        item = append_reminder(text, time_str, due, repeat=repeat)
    else:
        item = append_reminder(text, time_str, due)
    rid = item["id"]
    when = f"，提醒时间{due.replace('T', ' ')}" if due else (f"（时间'{time_str}'未解析出时刻）" if time_str else "")
    repeat_desc = {"daily": "（每天重复）", "weekly": "（每周重复）", "weekdays": "（工作日重复）"}.get(repeat, "")
    return {"ok": True, "id": rid, "message": f"已添加提醒：{text}{when}{repeat_desc}"}

@router.delete("/api/reminders/{rid}")
async def delete_reminder(rid: int):
    if not complete_reminder(rid):
        raise HTTPException(404, "提醒不存在")
    return {"ok": True, "message": f"提醒{rid}已标记完成"}

@router.get("/api/notifications")
async def get_notifications():
    notifs = drain_notifications()
    return {"count": len(notifs), "notifications": notifs}

@router.get("/api/events")
async def sse_events():
    """SSE实时通知流(Web客户端用EventSource连接, 免轮询)"""
    queue = asyncio.Queue()
    register_sse_client(queue)

    async def event_stream():
        try:
            yield sse_event({
                "type": "connect",
                "text": "已连接",
                "time": datetime.datetime.now().isoformat(),
            })
            while True:
                try:
                    event_frame = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event_frame
                except asyncio.TimeoutError:
                    yield SSE_EVENT_HEARTBEAT_FRAME
        except asyncio.CancelledError:
            pass
        finally:
            unregister_sse_client(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

@router.get("/api/lan-info")
async def lan_info():
    lan_ip = _get_lan_ip() or "127.0.0.1"
    return {
        "http_url": f"http://{lan_ip}:{http_port()}",
        "https_url": f"https://{lan_ip}:{https_port()}",
        "lan_ip": lan_ip,
        "http_port": http_port(),
        "https_port": https_port(),
    }

@router.api_route("/xiaozhi/ota", methods=["GET", "POST"])
async def xiaozhi_ota(request: Request):
    """OTA config endpoint for xiaozhi firmware."""
    try:
        body = await request.body()
        if body:
            log.info("[xiaozhi] OTA device self-report: %s", body[:500].decode("utf-8", "replace"))
    except Exception as e:
        log.warning("[xiaozhi] OTA body read error: %s", e)
    host = request.url.hostname or ""
    client_ip = request.client.host if request.client else ""
    if client_ip and not client_ip.startswith("127."):
        host = _get_lan_ip() or "127.0.0.1"
    elif host in ("localhost", "127.0.0.1", "::1", ""):
        host = _get_lan_ip() or "127.0.0.1"
    ws_url = f"ws://{host}:{http_port()}/ws/xiaozhi"
    ota_response = {
        "websocket": {"url": ws_url, "version": 1},
        "server_time": {"timestamp": int(datetime.datetime.now().timestamp()), "timezone_offset": 480},
    }
    mqtt_broker = os.getenv("MQTT_BROKER", "")
    if mqtt_broker and os.getenv("MQTT_ENABLE_OTA", "0") == "1":
        mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        mqtt_user = os.getenv("MQTT_USER", "")
        mqtt_pass = os.getenv("MQTT_PASSWORD", "")
        device_id = os.getenv("MQTT_DEVICE_ID", "esp32-default")
        ota_response["mqtt"] = {
            "endpoint": f"{mqtt_broker}:{mqtt_port}",
            "client_id": f"charlie-{device_id}",
            "publish_topic": f"charlie/esp32/{device_id}/up",
            "subscribe_topic": f"charlie/esp32/{device_id}/down",
            "keepalive": 60,
        }
        if mqtt_user:
            ota_response["mqtt"]["username"] = mqtt_user
        if mqtt_pass:
            ota_response["mqtt"]["password"] = mqtt_pass
        log.info(f"[xiaozhi] OTA 返回 MQTT 配置: {mqtt_broker}:{mqtt_port} (device={device_id})")
    return JSONResponse(ota_response)
