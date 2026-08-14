"""
Charlie - 实时语音服务 (精简入口)

voice_server.py 现在是组装入口: 日志 → FastAPI app → 中间件 → 路由 → 启动。
所有业务逻辑已拆分到:
  app/http_helpers.py     — ETag/JSON/HTML/SSE 响应辅助
  app/notifications.py     — 通知队列/飞书/ntfy/SSE推送/提醒音频/xiaozhi推送
  app/cors.py              — 动态CORS + 限流
  app/schedulers.py         — 提醒/主动建议/自进化/决策引擎/唤醒词后台线程
  app/routes/system.py      — 只读系统状态路由
  app/routes/conversation.py — 语音/文字/流式/ASR/TTS/搜索/导出路由
  app/routes/reminders.py    — 提醒/通知/SSE/lan-info/OTA路由
  app/routes/websocket.py   — WebSocket双向通信路由
  app/routes/manage.py      — 配置/PWA/ESP32/偏好/行为/协议/MCP路由
"""
import os, sys, json, time, logging, asyncio, concurrent.futures
import uuid

if not getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    _dotenv_path = os.path.join(os.path.dirname(sys.executable), ".env") if getattr(sys, "frozen", False) else None
    load_dotenv(_dotenv_path) if _dotenv_path else load_dotenv()
except ImportError: pass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from pydantic import BaseModel, Field

# ===== 结构化日志 =====
import logging as _logging
import logging.handlers as _loghandlers

LOG_FORMAT = os.getenv("LOG_FORMAT", "text")

class JsonFormatter(_logging.Formatter):
    def format(self, record):
        import json as _j
        entry = {
            "ts": _logging.Formatter.formatTime(self, record),
            "level": record.levelname,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])[:200]
        return _j.dumps(entry, ensure_ascii=False)

if LOG_FORMAT == "json":
    _handler = _logging.StreamHandler()
    _handler.setFormatter(JsonFormatter())
    _logging.basicConfig(level=_logging.INFO, handlers=[_handler])
else:
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = _logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.environ.get("ASSISTANT_KID_LOG_DIR", os.path.join(PROJECT_DIR, "logs"))
TUNNEL_FILE = os.path.join(PROJECT_DIR, "tunnel_url.txt")

os.makedirs(_LOG_DIR, exist_ok=True)
_file_handler = _loghandlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "app.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
_file_handler.setLevel(_logging.INFO)
for _lg in (_logging.getLogger(), _logging.getLogger("uvicorn"),
            _logging.getLogger("uvicorn.error"), _logging.getLogger("uvicorn.access")):
    _lg.addHandler(_file_handler)
log.info(f"文件日志已启用: {os.path.join(_LOG_DIR, 'app.log')}")

# ===== 导入拆分后的模块 =====
from app.audio import MAX_AUDIO_SIZE
from app.auth import _client_ip, _check_auth, AUTH_TOKEN
from app.brain_health import _brain_is_warm, _warmup_brain
from app import env_catalog
from app.config import http_port, https_port
from app.state import (
    _metrics, MAX_REQUEST_BODY, _ws_client_count, _RATE_GENERAL, _RATE_VOICE,
    _RATE_PER_SESSION, _rate_buckets, _session_buckets, _RATE_LOCK, _RATE_WINDOW,
    sse_client_count,
)
from app.cors import (
    DynamicCORSMiddleware, refresh_cors_origins, reload_cors_origins,
    get_cors_origins, check_rate, tunnel_origins, read_tunnel_url, get_lan_ip,
)
from app.notifications import (
    add_notification, set_main_loop, get_main_loop,
    push_notification_to_sse, async_push_tts_to_xiaozhi, push_tts_to_xiaozhi,
)
from app.schedulers import (
    start_scheduler, start_proactive, start_evolution, start_decision_engine,
    start_wake_listener,
)
from app.routes.system import system_router
from app.routes.conversation import router as conversation_router
from app.routes.reminders import router as reminders_router
from app.routes.websocket import router as websocket_router, start_ws_cleanup
from app.routes.manage import router as manage_router, set_start_time
from app.http_helpers import weak_etag, if_none_matches, not_modified_response
from app.xiaozhi_ws import register_xiaozhi_routes
from tuya_proxy import router as tuya_router
from voice_agent import LOW_INTENT_ASR_REPLY, is_low_intent_asr

# ===== Backward-compatible aliases (代码中可能直接 import voice_server.xxx) =====
_reload_cors_origins = reload_cors_origins
_refresh_cors_origins = refresh_cors_origins
_get_lan_ip = get_lan_ip
_get_tunnel_url = read_tunnel_url
_add_notification = add_notification
_push_notification_to_sse = push_notification_to_sse

# ===== 启动时序 =====
_start_time = time.time()
set_start_time(_start_time)
_io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="charlie-io")

# env validation
for line in env_catalog.render_startup_log():
    log.info(line)
missing = env_catalog.missing_required()
if missing:
    log.warning(f"[env] 缺少{len(missing)}个必需密钥: {[e.name for e in missing]}")
if env_catalog.demo_mode_active():
    log.warning("=== Demo 模式已启用 ===")
    log.warning("未配置 ARK_KEY / GLM_KEY，大脑将使用 Ollama 本地模型。")
    log.warning("推荐: 打开 http://localhost:%d/welcome 引导页" % http_port())

try:
    from app.preflight import run_preflight
    run_preflight()
except Exception as _e:
    log.debug(f"[preflight] 检测跳过: {_e}")

# ===== FastAPI app =====
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    asyncio.get_running_loop().set_default_executor(_io_pool)
    if os.environ.get("SKIP_BACKGROUND") == "1":
        log.info("后台调度器跳过(SKIP_BACKGROUND=1，由HTTP进程管理)")
    else:
        _main_loop = asyncio.get_running_loop()
        set_main_loop(_main_loop)
        from app.mqtt_server import init_server as init_mqtt_server
        init_mqtt_server(_main_loop)
        from utils import cleanup_temp_files, truncate_history_file
        from voice_agent import runtime_temp_audio_path
        from app.reminders import REMINDERS_FILE
        _history_file = os.path.join(os.path.dirname(REMINDERS_FILE), "conversation_history.json")
        cleanup_temp_files(extra_dirs=[runtime_temp_audio_path()])
        truncate_history_file(_history_file, 100)
        start_scheduler()
        start_proactive()
        start_evolution()
        start_decision_engine()
        start_wake_listener()
        start_ws_cleanup()
        _warmup_brain()
        try:
            from agent.asr_tts import _load_sense_voice
            _load_sense_voice()
        except Exception:
            pass
        if os.getenv("FEISHU_PUSH_OPEN_ID"):
            try:
                import threading as _th
                from personalized_push import personalized_push_loop
                _th.Thread(target=personalized_push_loop, daemon=True).start()
            except Exception:
                pass
    yield
    log.info("[shutdown] 保存状态并退出...")
    try:
        from voice_agent import _save_history
        _save_history()
    except Exception:
        pass
    _io_pool.shutdown(wait=False, cancel_futures=True)

app = FastAPI(title="Charlie语音服务", lifespan=lifespan)
app.include_router(system_router)
app.include_router(tuya_router)
app.include_router(conversation_router)
app.include_router(reminders_router)
app.include_router(websocket_router)
app.include_router(manage_router)

# ===== 全局异常处理 =====
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback as _tb
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    log.error(f"[500] 未处理异常 {request.method} {request.url.path}: {exc}\n{tb}")
    return JSONResponse({"error": "internal_server_error", "detail": str(exc), "path": request.url.path}, status_code=500)

# ===== 请求体大小限制 =====
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_REQUEST_BODY:
            return JSONResponse({"error": f"请求体过大({int(cl)//1024//1024}MB), 上限{MAX_REQUEST_BODY//1024//1024}MB"}, status_code=413)
    return await call_next(request)

# ===== CORS =====
app.add_middleware(DynamicCORSMiddleware,
    allow_origins=lambda: get_cors_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    allow_credentials=True,
    max_age=3600)

# ===== 请求日志 + 认证 + 限流 =====
@app.middleware("http")
async def request_logger(request: Request, call_next):
    rid = str(uuid.uuid4())[:8]
    start = time.time()
    log.debug(f"[{rid}] {request.method} {request.url.path}")
    if not _check_auth(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    ip = _client_ip(request)
    path = request.url.path
    is_voice = "/api/voice" in path or "/api/tts" in path or "/api/asr" in path
    bucket_type = "voice" if is_voice else "general"
    limit = _RATE_VOICE if is_voice else _RATE_GENERAL
    allowed, remaining, retry_after = check_rate(ip, bucket_type, limit)
    if not allowed:
        log.warning(f"[{rid}] 限流 {ip} {path} (超过{limit}/min)")
        return JSONResponse({"error": f"请求过于频繁, 请{retry_after}秒后重试"}, status_code=429, headers={"Retry-After": str(retry_after)})
    try:
        response = await call_next(request)
    except Exception as e:
        dur = (time.time() - start) * 1000
        _metrics.record(request.url.path, dur, ok=False, include_latency=path != "/api/metrics", include_in_metrics=path != "/api/metrics")
        log.error(f"[{rid}] 异常: {e} ({dur:.0f}ms)")
        raise
    dur = (time.time() - start) * 1000
    ok = response.status_code < 500
    conditional = request.method.upper() == "GET" and bool(request.headers.get("if-none-match"))
    not_modified = response.status_code == 304
    _metrics.record(request.url.path, dur, ok=ok, conditional=conditional, not_modified=not_modified,
                    include_latency=path != "/api/metrics", include_in_metrics=path != "/api/metrics")
    msg = f"[{rid}] {request.method} {request.url.path} → {response.status_code} {dur:.0f}ms"
    if response.status_code == 304:
        log.debug(msg)
    else:
        log.info(msg)
    response.headers["X-Request-ID"] = rid
    return response

# Register xiaozhi-compatible WebSocket endpoint
register_xiaozhi_routes(app)

# ===== Backward-compatible re-exports =====
# Tests and external code import voice_server._xxx directly.
# These re-exports keep them working after the split.
from app.routes import manage as _manage_mod
from app.routes import conversation as _conv_mod
from app.routes import websocket as _ws_mod
from app.routes import reminders as _rem_mod
from app.http_helpers import (
    weak_etag as _weak_etag, etag_headers as _etag_headers,
    if_none_matches as _if_none_matches, not_modified_response as _not_modified_response,
    file_not_modified_response as _file_not_modified_response, file_etag_token as _file_etag_token,
    read_cached_text as _read_cached_text, html_response as _html_response,
    json_response as _json_response, build_manifest_payload as _build_manifest_payload,
    manifest_response as _manifest_response, sse_event as _sse_event,
    SSE_DONE_FRAME as _SSE_DONE_FRAME, SSE_HEARTBEAT_FRAME as _SSE_HEARTBEAT_FRAME,
    SSE_EVENT_HEARTBEAT_FRAME as _SSE_EVENT_HEARTBEAT_FRAME,
)
from app.notifications import (
    append_notification as _append_notification, drain_notifications as _drain_notifications,
    push_feishu_async as _push_feishu_async, _push_ntfy_async,
    play_reminder_audio as _play_reminder_audio, _afplay_and_cleanup,
    _main_loop as _notif_main_loop,
)
from app.cors import (
    _cors_origins as _cors_origins, _CORS_ORIGIN_TTL_SECONDS,
    _cors_origins_loaded_at,
)

_esp32_flash = _manage_mod._esp32_flash
_model_download = _manage_mod._model_download
_find_esp32_firmware = _manage_mod._find_esp32_firmware
_esp32_flash_worker = _manage_mod._esp32_flash_worker
_check_model_exists = _manage_mod._check_model_exists
_download_model_worker = _manage_mod._download_model_worker
_parse_env_file = _manage_mod._parse_env_file
_write_env_file = _manage_mod._write_env_file
_reload_runtime_env = _manage_mod._reload_runtime_env
_ENV_FILE = _manage_mod._ENV_FILE
_SETUP_WHITELIST = _manage_mod._SETUP_WHITELIST
AVAILABLE_TTS_VOICES = _manage_mod.AVAILABLE_TTS_VOICES
_build_app_icon_png = _manage_mod._build_app_icon_png
_APP_ICON_PNG = _manage_mod._APP_ICON_PNG
_APP_ICON_ETAG = _manage_mod._APP_ICON_ETAG
_ICON_HEADERS = _manage_mod._ICON_HEADERS

# Conversation helpers
_flush_tts_buffer = _conv_mod._flush_tts_buffer
_friendly_brain_error = _conv_mod._friendly_brain_error
_empty_asr_events = _conv_mod._empty_asr_events
_low_intent_asr_events = _conv_mod._low_intent_asr_events
_synthesize_tts_event = _conv_mod._synthesize_tts_event
_stream_brain_tts = _conv_mod._stream_brain_tts
_check_session_rate = _conv_mod._check_session_rate
ChatRequest = _conv_mod.ChatRequest
TTSRequest = _conv_mod.TTSRequest
ACK_AFTER_ASR_MESSAGE = _conv_mod.ACK_AFTER_ASR_MESSAGE

# WebSocket helpers
_ws_stream_and_send = _ws_mod._ws_stream_and_send
_ws_cancel_stream = _ws_mod._ws_cancel_stream
_ws_join_session = _ws_mod._ws_join_session
_ws_broadcast_to_session = _ws_mod._ws_broadcast_to_session
_ws_reverse_geocode = _ws_mod._ws_reverse_geocode
_ws_cleanup_after_disconnect = _ws_mod._ws_cleanup_after_disconnect
_ws_cleanup_stale = _ws_mod._ws_cleanup_stale
_ws_stream_brain = _ws_mod._ws_stream_brain
_WS_STALE_TIMEOUT = _ws_mod._WS_STALE_TIMEOUT

# Notifications
from app.notifications import _notifications, _notifications_lock
_add_notification = add_notification
_push_notification_to_sse = push_notification_to_sse
_push_tts_to_xiaozhi = push_tts_to_xiaozhi
_async_push_tts_to_xiaozhi = async_push_tts_to_xiaozhi
_main_loop = get_main_loop()

# Schedulers
from app.schedulers import (
    _reminder_scheduler, _proactive_suggestions, _process_wake_command,
    _scheduler_lock_handle, _proactive_lock_handle, _proactive_thread,
    SUGGESTIONS_STATE, _SUGGESTIONS_DEFAULT_STATE, _suggest_state_lock,
    _locked_suggest_state, _read_locked_suggest_state, _write_locked_suggest_state,
    _refresh_suggestions_state, _load_suggest_state, _save_suggest_state,
    _suggest_state_snapshot, _update_suggest_state, _claim_suggest_state,
    _get_weather, _forecast_for_date, _preference_state_key,
    SUGGEST_STATE_FILE, SUGGEST_STATE_LOCK_FILE, AMAP_KEY,
)
_start_proactive = start_proactive

# Vosk wake word
_VOSK_MODEL = None
_VOSK_VARIONS = _conv_mod._VOSK_VARIONS
_VOSK_WAKE_WORDS = _conv_mod._VOSK_WAKE_WORDS
_get_vosk_model = _conv_mod._get_vosk_model

# State imports for backward compat
from app.state import (
    _ws_clients, _ws_session_groups, _ws_client_locations,
    _interrupt_telemetry, _sse_clients, _sse_clients_lock,
)

MAX_TEXT_LENGTH = 500

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=http_port())
