"""Notification system: queue, Feishu push, ntfy, SSE dispatch, reminder audio, xiaozhi TTS push.

Extracted from voice_server.py so route modules and schedulers can share
notification dispatch without importing the monolith.
"""
import os, sys, json, time, datetime, logging, threading, asyncio, tempfile, subprocess
import base64 as _b64enc
from collections import deque

import requests

from app.state import (
    register_sse_client, unregister_sse_client, snapshot_sse_clients, sse_client_count,
)
from app.http_helpers import sse_event

log = logging.getLogger("magic")

# ===== Notification queue =====
MAX_NOTIFICATIONS = 20
_notifications = deque(maxlen=MAX_NOTIFICATIONS)
_notifications_lock = threading.Lock()

def append_notification(notification: dict) -> None:
    with _notifications_lock:
        _notifications.append(notification)

def drain_notifications() -> list[dict]:
    with _notifications_lock:
        notifications = list(_notifications)
        _notifications.clear()
        return notifications

# ===== Feishu push =====
FEISHU_PUSH_OPEN_ID = os.getenv("FEISHU_PUSH_OPEN_ID", "")
FEISHU_PUSH_ENABLED = os.getenv("FEISHU_PUSH_ENABLED", "1") == "1" and bool(FEISHU_PUSH_OPEN_ID)

# ntfy backup channel
from app.ntfy_push import push_ntfy_async as _push_ntfy_async

def push_feishu_async(text: str):
    """异步推飞书消息(线程, 不阻塞通知/SSE)。所有主动服务触发时自动推送。"""
    if not FEISHU_PUSH_ENABLED:
        return
    def _send():
        try:
            app_id = os.getenv("FEISHU_APP_ID", "")
            app_secret = os.getenv("FEISHU_APP_SECRET", "")
            r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
            token = r.json().get("tenant_access_token", "")
            if not token:
                return
            requests.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"receive_id": FEISHU_PUSH_OPEN_ID, "msg_type": "text",
                      "content": json.dumps({"text": text})}, timeout=10)
            log.info(f"[feishu] 推送成功: {text[:40]}")
        except Exception as e:
            log.warning(f"[feishu] 推送失败: {e}")
    threading.Thread(target=_send, daemon=True).start()

def add_notification(text: str, ntype: str = "reminder"):
    """添加通知到队列+SSE推送+飞书推送+ntfy备用

    飞书/ntfy 仅推送高价值通知，过滤低价值噪音:
    - 保留: weather/sleep/away/home/morning/evening/decision/preference
    - 跳过: wake(对话转录无意义)、health(系统监控Mac可见无需推)
    """
    # SSE: 所有类型都推（浏览器能过滤）
    notification = {
        "text": text, "type": ntype,
        "time": datetime.datetime.now().isoformat()
    }
    append_notification(notification)
    if sse_client_count():
        push_notification_to_sse(sse_event(notification))  # SSE实时推送

    # 飞书/ntfy: 仅高价值通知
    FEISHU_HIGH_VALUE = {"weather", "sleep", "away", "home", "morning", "evening", "decision", "preference"}
    if ntype in FEISHU_HIGH_VALUE:
        push_feishu_async(text)   # 飞书消息推送(异步, 不阻塞)
        _push_ntfy_async(text)     # ntfy 备用通道(异步, 不阻塞)
    elif ntype not in ("wake", "health", "reminder"):
        # 未知类型默认推（保持向后兼容）
        push_feishu_async(text)
        _push_ntfy_async(text)
    else:
        log.debug(f"[notification] 跳过飞书/ntfy推送: [{ntype}] {text[:40]}")

# ===== SSE dispatch =====
_main_loop = None  # 主线程event loop(启动时捕获)

def set_main_loop(loop):
    """Called from lifespan to register the asyncio event loop."""
    global _main_loop
    _main_loop = loop

def get_main_loop():
    return _main_loop

def push_notification_to_sse(event_frame: str):
    """推送已编码的 SSE 帧到所有已连接客户端(线程安全)"""
    if _main_loop is None:
        return  # 没有SSE客户端或loop未初始化
    for client_q in snapshot_sse_clients():
        try:
            _main_loop.call_soon_threadsafe(_put_sse_event_nowait, client_q, event_frame)
        except Exception:
            log.debug("SSE调度失败，等待连接清理", exc_info=True)

def _put_sse_event_nowait(client_q: asyncio.Queue, event_frame: str) -> None:
    try:
        client_q.put_nowait(event_frame)
    except Exception:
        unregister_sse_client(client_q)

# ===== Reminder audio playback =====

def play_reminder_audio(text: str, reminder_id: int | None = None):
    """生成提醒语音并播放到 ESP32 + 浏览器SSE + macOS afplay(异步)
    afplay 放到独立线程避免阻塞决策/调度线程"""
    import platform as _platform
    try:
        from app.audit_log import audit_log
        audit_log("notification:reminder_audio", input_data=text,
                  action="play", session_id="system", source="system")
    except Exception:
        pass
    try:
        from voice_agent import tts_to_mp3
        log.info(f"[reminder] TTS生成: {text}")
        audio = tts_to_mp3(f"主人，提醒您：{text}")
        if not audio or len(audio) < 100:
            raise RuntimeError("TTS返回空音频")

        # 推送到 ESP32 (xiaozhi WebSocket) — fire and forget
        display_text = f"主人，提醒您：{text}"
        delivery = push_tts_to_xiaozhi(display_text, audio)

        # macOS: afplay 放到独立线程，不阻塞决策循环
        if _platform.system() == "Darwin":
            from voice_agent import runtime_temp_audio_path
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=runtime_temp_audio_path())
            tmp.write(audio)
            tmp.close()
            log.info(f"[reminder] 播放提醒语音 {len(audio)}字节(MP3): {text}")
            threading.Thread(
                target=lambda: _afplay_and_cleanup(tmp.name),
                daemon=True,
            ).start()
        else:
            # Linux/容器: 通过 SSE 推送音频给所有连接的浏览器客户端
            audio_b64 = _b64enc.b64encode(audio).decode()
            push_notification_to_sse(sse_event({"type": "audio", "audio": audio_b64, "source": "reminder"}))
            log.info(f"[reminder] 通过SSE推送提醒语音 {len(audio)}字节: {text}")

        # 仅直推成功才标记完成；入队/失败保持 pending 等待 ESP32 flush 或重试
        if reminder_id is not None:
            from app.reminders import complete_reminder_delivery
            if delivery == "direct":
                complete_reminder_delivery(reminder_id)
            else:
                log.info(f"[reminder] 提醒{reminder_id}投递状态={delivery}，保持 pending 等待 ESP32 flush")
    except Exception as e:
        log.error(f"[reminder] 播放失败: {e}")
        if reminder_id is not None:
            from app.reminders import release_failed_reminder
            release_failed_reminder(reminder_id, datetime.datetime.now(), str(e))

def _afplay_and_cleanup(tmp_path: str):
    """独立线程执行 afplay + 清理临时文件"""
    try:
        subprocess.run(["afplay", tmp_path], timeout=30, capture_output=True)
        log.info("[reminder] 播放完成")
    except Exception as e:
        log.debug(f"[reminder] afplay失败: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

# ===== Xiaozhi TTS push =====

async def async_push_tts_to_xiaozhi(ws, text: str, mp3_data: bytes):
    """异步推送 TTS Opus 音频到单个 ESP32 设备"""
    import json as _json
    try:
        from app.xiaozhi_codec import mp3_to_opus_packets
        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, mp3_to_opus_packets, mp3_data)
        if not packets:
            log.warning("[xiaozhi-push] Opus编码失败")
            return
        await ws.send_text(_json.dumps({"type": "tts", "state": "start"}, ensure_ascii=False))
        await ws.send_text(_json.dumps({"type": "tts", "state": "sentence_start", "text": text}, ensure_ascii=False))
        for pkt in packets:
            await ws.send_bytes(pkt)
            await asyncio.sleep(0.06)  # 60ms 帧间隔，匹配设备解码速度
        await ws.send_text(_json.dumps({"type": "tts", "state": "stop"}, ensure_ascii=False))
        log.info(f"[xiaozhi-push] 推送成功: {text[:30]} ({len(packets)}帧)")
    except Exception as e:
        log.warning(f"[xiaozhi-push] 推送失败: {e}")


def _https_forward_reminder(text: str, mp3_data: bytes) -> None:
    """异步线程：将提醒 TTS 转发到 HTTPS 端口（ESP32 可能连 8443）。"""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        from app.config import https_port
        _hport = https_port()
        mp3_b64 = _b64enc.b64encode(mp3_data).decode()
        r = requests.post(
            f"https://127.0.0.1:{_hport}/api/internal/xiaozhi-push",
            json={"text": text, "mp3": mp3_b64},
            verify=False, timeout=10)
        if r.status_code in (200, 202):
            data = r.json()
            if data.get("pushed", 0) > 0:
                log.info(f"[xiaozhi-push] HTTPS转发成功({data['pushed']}/{data['total']}): {text[:30]}")
    except Exception as e:
        log.debug(f"[xiaozhi-push] HTTPS转发失败: {e}")


def push_tts_to_xiaozhi(text: str, mp3_data: bytes) -> str:
    """推送 TTS 到所有连接的 ESP32 设备（从同步线程调用）
    本进程有连接则直推；无连接时入队待 flush + 转发HTTPS进程
    返回投递状态: "direct" | "enqueued" | "failed"
    """
    from app.state import snapshot_xiaozhi_clients, enqueue_xiaozhi_pending
    from app.config import https_port

    # 如果正在语音对话 streaming，提醒 TTS 不入队 WS 推送（避免两路音频交错），
    # 改为只走 afplay（本地播放）+ 入队待 flush。
    try:
        from app.xiaozhi_ws import is_xiaozhi_streaming
        if is_xiaozhi_streaming():
            log.info("[xiaozhi-push] 对话 streaming 中，提醒 TTS 跳过 WS 推送: %s", text[:30])
            # 本地 afplay 由 play_reminder_audio 负责；这里只做入队 + HTTPS 转发
            qsize = enqueue_xiaozhi_pending(text, mp3_data)
            log.info("[xiaozhi-push] 提醒 TTS 入队待flush(%d): %s", qsize, text[:30])
            # 仍然尝试 HTTPS 转发（ESP32 可能连 8443）— 异步线程避免阻塞
            _th = threading.Thread(target=_https_forward_reminder, args=(text, mp3_data), daemon=True)
            _th.start()
            return "enqueued"
    except Exception:
        pass

    # 1. 本进程直推（ESP32 可能连 HTTP 8000）
    # 等待 future.result() 确认投递成功，避免提醒被误标"已送达"
    import concurrent.futures as _cf
    clients = snapshot_xiaozhi_clients()
    pushed = 0
    for client_id, info in clients.items():
        ws = info["ws"]
        loop = info["loop"]
        try:
            future = asyncio.run_coroutine_threadsafe(
                async_push_tts_to_xiaozhi(ws, text, mp3_data), loop)
            future.result(timeout=10)
            pushed += 1
        except _cf.TimeoutError:
            future.cancel()  # 阻止 coroutine 继续向 ws 发送，避免与后续 TTS 流交错
            log.warning(f"[xiaozhi-push] 直推超时 {client_id}: {text[:30]}")
        except Exception as e:
            log.warning(f"[xiaozhi-push] 直推失败 {client_id}: {e}")

    if pushed > 0:
        log.info(f"[xiaozhi-push] 直推 {pushed} 个设备: {text[:30]}")
        return "direct"

    # 2. 本进程无连接 → 尝试 MQTT 直推 + 入队 + HTTPS 转发
    # 2a. MQTT 协议端（ESP32 常驻连接时直接推送，秒级响应）
    try:
        from app.mqtt_server import push_tts_to_mqtt
        if push_tts_to_mqtt(text, mp3_data):
            log.info(f"[xiaozhi-push] MQTT直推成功: {text[:30]}")
            return "direct"
    except Exception:
        pass

    # 2b. 入队待 flush（ESP32 下次唤醒时补发）
    qsize = enqueue_xiaozhi_pending(text, mp3_data)
    log.info(f"[xiaozhi-push] ESP32未连接，入队({qsize}): {text[:30]}")

    # 3. 异步转发到 HTTPS 进程（ESP32 可能连 8443），不阻塞调度线程
    import threading as _th
    _hport = https_port()
    mp3_b64 = _b64enc.b64encode(mp3_data).decode()

    def _https_forward():
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.post(
                f"https://127.0.0.1:{_hport}/api/internal/xiaozhi-push",
                json={"text": text, "mp3": mp3_b64},
                verify=False, timeout=10)
            if r.status_code in (200, 202):
                data = r.json()
                if data.get("pushed", 0) > 0:
                    log.info(f"[xiaozhi-push] HTTPS转发成功({data['pushed']}/{data['total']}): {text[:30]}")
        except Exception as e:
            log.debug(f"[xiaozhi-push] HTTPS转发失败: {e}")

    _th.Thread(target=_https_forward, daemon=True).start()

    return "enqueued"

def _get_lan_ip() -> str | None:
    """获取本机局域网IP (非127.x)"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None
