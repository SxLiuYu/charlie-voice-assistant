"""
Charlie - 实时语音服务
POST /api/voice    : 音频进 → ASR → 大脑(deepseek-v4-flash+MCP) → TTS → 音频出
POST /api/chat     : 纯文字进 → 大脑 → 文字出
GET  /api/reminders: 待办列表
POST /api/reminders: 添加提醒
DEL  /api/reminders/{id}: 删除/完成提醒
GET  /              : Web自动监听客户端(免点击对话)
GET  /health        : 健康检查

后台调度器: 每30s检查reminders.json，到期提醒自动TTS+afplay播报
"""
import os, sys, subprocess, tempfile, json, threading, time, datetime, logging, asyncio, concurrent.futures
import queue as _queue
import base64 as _b64enc
import hashlib
from contextlib import asynccontextmanager, contextmanager
from collections import deque
from collections.abc import Callable
if not getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse, JSONResponse, StreamingResponse
import uvicorn
import requests
import fcntl
from pydantic import BaseModel, Field, field_validator

# ===== 结构化日志(JSON或文本格式, 通过LOG_FORMAT环境变量控制) =====
import logging as _logging
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")  # "text" 或 "json"

class JsonFormatter(_logging.Formatter):
    """JSON格式日志(便于日志聚合系统收集)"""
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

# ===== 文件日志(持久化, 含uvicorn错误堆栈, 防止traceback丢失) =====
import logging.handlers as _loghandlers
os.makedirs(_LOG_DIR, exist_ok=True)
_file_handler = _loghandlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "app.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
_file_handler.setLevel(_logging.INFO)
# 挂到root + uvicorn子logger, 确保未捕获异常堆栈落盘
for _lg in (_logging.getLogger(), _logging.getLogger("uvicorn"),
            _logging.getLogger("uvicorn.error"), _logging.getLogger("uvicorn.access")):
    _lg.addHandler(_file_handler)
log.info(f"文件日志已启用: {os.path.join(_LOG_DIR, 'app.log')}")
# ===== 抽取到app模块(Phase1: 纯函数) =====
from app.audio import likely_empty_audio, to_wav, _wav_to_mp3, MAX_AUDIO_SIZE
from app.xiaozhi_ws import register_xiaozhi_routes
from app.auth import _client_ip, _is_local_request, _check_auth, _sanitize_text, AUTH_TOKEN
from app.brain_health import _brain_is_warm, _warmup_brain
from app.config import (
    configured_cors_origins,
    http_port,
    invalidate_lan_origins_cache,
    lan_origins,
    localhost_origins,
)
from app.state import (_metrics, _ws_clients, _rate_buckets, _session_buckets,
    _RATE_GENERAL, _RATE_VOICE, _RATE_WINDOW, _RATE_LOCK, _RATE_PER_SESSION, MAX_REQUEST_BODY, _ws_client_count,
    _ws_session_groups, _ws_client_locations, _interrupt_telemetry,
    register_sse_client, unregister_sse_client, snapshot_sse_clients, sse_client_count)
from app.reminders import (
    REMINDERS_FILE, _load_reminders, acquire_scheduler_lock, append_reminder, claim_due_reminders,
    complete_reminder, complete_reminder_delivery, release_failed_reminder,
    SUGGESTIONS_STATE_FILE, PROACTIVE_LOCK_FILE, acquire_proactive_lock,
)
from app.routes.system import system_router
from tuya_proxy import router as tuya_router
from voice_agent import LOW_INTENT_ASR_REPLY, is_low_intent_asr

ACK_AFTER_ASR_MESSAGE = "嗯，让我想想"

HISTORY_FILE = os.path.join(os.path.dirname(REMINDERS_FILE), "conversation_history.json")

# ===== 请求指标追踪 =====

_start_time = time.time()  # 服务启动时间(用于健康检查uptime)
_scheduler_lock_handle = None


@asynccontextmanager
async def lifespan(app):
    """启动+关闭生命周期"""
    # === 启动 ===
    if os.environ.get("SKIP_BACKGROUND") == "1":
        log.info("后台调度器跳过(SKIP_BACKGROUND=1，由HTTP进程管理)")
    else:
        global _main_loop
        _main_loop = asyncio.get_running_loop()
        # 启动时清理临时文件 + 截断历史
        from utils import cleanup_temp_files, truncate_history_file
        from voice_agent import runtime_temp_audio_path
        cleanup_temp_files(extra_dirs=[runtime_temp_audio_path()])
        truncate_history_file(HISTORY_FILE, 100)
        _start_scheduler()
        _start_proactive()
        _start_evolution()
        _start_decision_engine()
        _start_wake_listener()
        _start_ws_cleanup()
        _warmup_brain()
    yield
    # === 关闭 ===
    log.info("[shutdown] 保存状态并退出...")
    try:
        from voice_agent import _save_history
        _save_history()
    except Exception:
        pass

def _validate_env():
    """启动时检查必需的API密钥，缺失的会警告"""
    required = [
        ("ARK_KEY", "火山引擎ARK(大脑)"),
        ("BAIDU_API_KEY", "百度ASR/TTS"),
        ("AMAP_KEY", "高德地图"),
    ]
    optional = [
        ("BAIDU_APP_ID", "百度APP_ID"),
        ("BAIDU_SECRET_KEY", "百度SECRET_KEY"),
        ("TAVILY_API_KEY", "Tavily搜索"),
        ("ALIYUN_API_KEY", "阿里云(购物分析)"),
    ]
    missing = []
    for key, desc in required:
        val = os.getenv(key, "")
        if not val:
            missing.append(f"❌ {key} ({desc})")
            log.error(f"缺少必需密钥: {key} ({desc})")
        else:
            log.info(f"✅ {key} ({desc})")
    for key, desc in optional:
        val = os.getenv(key, "")
        if not val:
            log.warning(f"⚠️ 可选密钥缺失: {key} ({desc})")
    if missing:
        log.error(f"缺少{len(missing)}个必需密钥！请打开 http://localhost:8000/setup 配置")
    return len(missing) == 0


def _weak_etag(token: str) -> str:
    """Build a compact weak ETag from an opaque cache token."""
    return 'W/"' + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] + '"'


def _etag_headers(etag: str) -> dict:
    return {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}


def _if_none_matches(request: Request, etag: str) -> bool:
    """Match If-None-Match, tolerating comma-separated weak/strong ETags."""
    cached_etags = []
    for value in request.headers.get("if-none-match", "").split(","):
        value = value.strip()
        if not value:
            continue
        cached_etags.append(value)
        if value.startswith("W/"):
            cached_etags.append(value[2:])
        else:
            cached_etags.append("W/" + value)
    return etag in cached_etags


def _not_modified_response(etag: str) -> Response:
    return Response(status_code=304, headers=_etag_headers(etag))


def _file_not_modified_response(request: Request, path: str, prefix: str) -> Response | None:
    """Return 304 only if both the request ETag and current file metadata still match."""
    etag = _weak_etag(_file_etag_token(path, prefix))
    if not _if_none_matches(request, etag):
        return None
    if _weak_etag(_file_etag_token(path, prefix)) != etag:
        return None
    return _not_modified_response(etag)


def _file_etag_token(path: str, prefix: str) -> str:
    """Return a stable file token without opening or reading the file."""
    try:
        stat = os.stat(path)
        return f"{prefix}:{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"
    except FileNotFoundError:
        return f"{prefix}:missing"
    except OSError as exc:
        return f"{prefix}:error:{exc.__class__.__name__}"


_open_text_file = open
_text_file_cache: dict[str, tuple[tuple[int, int, int], str]] = {}
_text_file_cache_lock = threading.Lock()


def _read_cached_text(path: str, return_token: bool = False):
    """Read a small static text file, reusing contents while file metadata is unchanged."""
    for _ in range(2):
        stat = os.stat(path)
        token = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        with _text_file_cache_lock:
            cached = _text_file_cache.get(path)
        if cached is not None and cached[0] == token:
            return (cached[1], token) if return_token else cached[1]

        with _open_text_file(path, encoding="utf-8") as f:
            text = f.read()

        after_stat = os.stat(path)
        after_token = (after_stat.st_mtime_ns, after_stat.st_size, after_stat.st_ino)
        if token == after_token:
            with _text_file_cache_lock:
                cached = _text_file_cache.get(path)
                if cached is None or cached[0] != token:
                    _text_file_cache[path] = (token, text)
            return (text, token) if return_token else text
    return (text, token) if return_token else text


def _html_response(request: Request, path: str, prefix: str) -> Response:
    """Return HTML with a file-based weak ETag and no-store validation headers."""
    cached = _file_not_modified_response(request, path, prefix)
    if cached is not None:
        return cached
    text, token = _read_cached_text(path, return_token=True)
    etag = _weak_etag(f"{prefix}:{token[0]}:{token[1]}:{token[2]}")
    if _if_none_matches(request, etag):
        return _not_modified_response(etag)
    body = text.encode("utf-8")
    headers = _etag_headers(etag)
    headers["Content-Length"] = str(len(body))
    if request.method == "HEAD":
        body = b""
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)


def _json_response(
    request: Request,
    payload: dict | Callable[[], dict],
    etag_token: str | None = None,
) -> Response:
    """Return compact JSON with a weak ETag for polling-heavy GET endpoints."""
    if etag_token is not None:
        etag = _weak_etag(etag_token)
        if _if_none_matches(request, etag):
            return _not_modified_response(etag)
        resolved_payload = payload() if callable(payload) else payload
        body = json.dumps(resolved_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        resolved_payload = payload() if callable(payload) else payload
        body = json.dumps(resolved_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = 'W/"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if _if_none_matches(request, etag):
            return _not_modified_response(etag)
    return Response(content=body, media_type="application/json", headers=_etag_headers(etag))


_manifest_lock = threading.Lock()
_MANIFEST_BODY: tuple[bytes, str] | None = None


def _build_manifest_payload() -> dict:
    return {
        "name": "Charlie",
        "short_name": "Charlie",
        "description": "中国版贾维斯 - AI语音助理，全屋智能家居控制",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f0c29",
        "theme_color": "#e94560",
        "icons": [
            {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml"},
            {"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml"}
        ],
        "categories": ["productivity", "lifestyle", "utilities"],
        "lang": "zh-CN",
        "dir": "ltr"
    }


def _manifest_response(request: Request) -> Response:
    """Serve the immutable PWA manifest once, supporting HEAD and conditional GET."""
    global _MANIFEST_BODY
    with _manifest_lock:
        if _MANIFEST_BODY is None:
            body = json.dumps(
                _build_manifest_payload(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            etag = 'W/"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            _MANIFEST_BODY = (body, etag)
        body, etag = _MANIFEST_BODY

    if _if_none_matches(request, etag):
        return _not_modified_response(etag)

    headers = _etag_headers(etag)
    headers["Content-Length"] = str(len(body))
    content = b"" if request.method == "HEAD" else body
    return Response(content=content, media_type="application/json", headers=headers)


_validate_env()

# ===== Pydantic请求模型(自动验证+Swagger文档) =====
class ChatRequest(BaseModel):
    """文字对话请求"""
    message: str = Field(..., min_length=1, max_length=500, description="用户消息(1-500字)")
    session_id: str = Field(default="default", description="会话ID(多用户隔离)")

class TTSRequest(BaseModel):
    """TTS语音合成请求"""
    text: str = Field(..., min_length=1, max_length=500, description="要合成的文字(1-500字)")

class ReminderRequest(BaseModel):
    """添加提醒请求"""
    text: str = Field(..., min_length=1, max_length=200, description="提醒内容(1-200字)")
    time: str = Field(default="", description="提醒时间(自然语言,如'30分钟后')")
    repeat: str = Field(default="", description="重复: 空=一次性, daily=每天, weekly=每周, weekdays=工作日")

class BrainRestartRequest(BaseModel):
    """大脑重启请求(预留)"""
    force: bool = Field(default=False, description="强制重启")

app = FastAPI(title="Charlie语音服务", lifespan=lifespan)
app.include_router(system_router)
app.include_router(tuya_router)

# ===== 全局异常处理: 捕获所有未处理异常, 记录完整堆栈到文件, 返回结构化JSON(而非裸500) =====
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback as _tb
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    log.error(f"[500] 未处理异常 {request.method} {request.url.path}: {exc}\n{tb}")
    return JSONResponse(
        {"error": "internal_server_error", "detail": str(exc), "path": request.url.path},
        status_code=500,
    )

# ===== 请求体大小限制中间件 =====

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """限制请求体大小, 防止大文件上传导致OOM"""
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_REQUEST_BODY:
            return JSONResponse(
                {"error": f"请求体过大({int(cl)//1024//1024}MB), 上限{MAX_REQUEST_BODY//1024//1024}MB"},
                status_code=413
            )
    return await call_next(request)

# CORS: 允许跨域访问(手机/其他设备)
# CORS: 动态允许来源(localhost + tunnel + 局域网)


class DynamicCORSMiddleware(CORSMiddleware):
    """CORS middleware that refreshes allowed origins from a provider per request."""

    def __init__(self, app, allow_origins=(), **kwargs):
        if callable(allow_origins):
            self._origin_provider = allow_origins
        else:
            self._origin_provider = lambda: allow_origins
        super().__init__(app, allow_origins=list(self._origin_provider()), **kwargs)

    def _refresh_origins(self):
        origins = list(self._origin_provider())
        allow_all = "*" in origins
        self.allow_origins = origins
        self.allow_all_origins = allow_all
        self.preflight_explicit_allow_origin = not allow_all or self.allow_credentials
        if allow_all:
            self.simple_headers["Access-Control-Allow-Origin"] = "*"
        else:
            self.simple_headers.pop("Access-Control-Allow-Origin", None)
        if self.preflight_explicit_allow_origin:
            self.preflight_headers["Vary"] = "Origin"
            self.preflight_headers.pop("Access-Control-Allow-Origin", None)
        else:
            self.preflight_headers["Access-Control-Allow-Origin"] = "*"
            self.preflight_headers.pop("Vary", None)

    async def __call__(self, scope, receive, send):
        _refresh_cors_origins()
        self._refresh_origins()
        return await super().__call__(scope, receive, send)


def _tunnel_origins() -> list[str]:
    try:
        with open(TUNNEL_FILE, encoding="utf-8") as f:
            tunnel = f.read().strip()
        return [tunnel] if tunnel else []
    except OSError:
        return []


_CORS_ORIGIN_TTL_SECONDS = 2.0
_cors_origins_loaded_at = 0.0


def _refresh_cors_origins(force: bool = False) -> list[str]:
    """Refresh tunnel CORS origins when cloudflared writes a new public URL."""
    global _cors_origins_loaded_at
    now = time.monotonic()
    if not force and _cors_origins and now - _cors_origins_loaded_at < _CORS_ORIGIN_TTL_SECONDS:
        return []
    invalidate_lan_origins_cache()
    tunnel = _tunnel_origins()
    origins = [
        *localhost_origins(),
        *lan_origins(),
        *tunnel,
        *configured_cors_origins(),
    ]
    _cors_origins[:] = list(dict.fromkeys(origins))
    _cors_origins_loaded_at = now
    return tunnel


def _reload_cors_origins() -> list[str]:
    """Force refresh CORS origins, returning the active tunnel origins."""
    return _refresh_cors_origins(force=True)


_cors_origins = [
    *localhost_origins(),
    *lan_origins(),
    *_tunnel_origins(),
    *configured_cors_origins(),
]

app.add_middleware(DynamicCORSMiddleware,
    allow_origins=lambda: list(_cors_origins),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    allow_credentials=True,
    max_age=3600)

# ===== 限流中间件(防滥用, 每IP每分钟60次普通+10次语音) =====


def _check_rate(ip: str, bucket_type: str, limit: int) -> tuple:
    """检查速率, 返回(allowed, remaining, retry_after)"""
    import time as _t
    now = _t.time()
    # 定期清理过期IP桶(防内存泄漏, 每5分钟)
    if len(_rate_buckets) > 1000:
        expired = [k for k, v in _rate_buckets.items()
                   if all(not v.get(bt) or now - v[bt][-1] > _RATE_WINDOW
                          for bt in ("voice", "general"))]
        for k in expired:
            del _rate_buckets[k]
    with _RATE_LOCK:
        if ip not in _rate_buckets:
            _rate_buckets[ip] = {}
        bucket = _rate_buckets[ip].setdefault(bucket_type, [])
        # 清除过期记录
        bucket[:] = [ts for ts in bucket if now - ts < _RATE_WINDOW]
        if len(bucket) >= limit:
            retry_after = int(_RATE_WINDOW - (now - bucket[0])) + 1
            return False, 0, retry_after
        bucket.append(now)
        return True, limit - len(bucket), 0

def _check_session_rate(session_id: str) -> tuple:
    """检查会话速率限制, 返回(allowed, remaining, retry_after)"""
    import time as _t
    if not session_id or session_id == "default":
        return True, _RATE_PER_SESSION, 0  # default不限
    now = _t.time()
    with _RATE_LOCK:
        bucket = _session_buckets.setdefault(session_id, [])
        bucket[:] = [ts for ts in bucket if now - ts < _RATE_WINDOW]
        if len(bucket) >= _RATE_PER_SESSION:
            retry = int(_RATE_WINDOW - (now - bucket[0])) + 1
            return False, 0, retry
        bucket.append(now)
        return True, _RATE_PER_SESSION - len(bucket), 0


# 请求日志中间件
@app.middleware("http")
async def request_logger(request: Request, call_next):
    import uuid, time as _t
    rid = str(uuid.uuid4())[:8]
    start = _t.time()
    log.debug(f"[{rid}] {request.method} {request.url.path}")
    # 认证检查
    if not _check_auth(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    # 限流检查
    ip = _client_ip(request)
    path = request.url.path
    is_voice = "/api/voice" in path or "/api/tts" in path or "/api/asr" in path
    bucket_type = "voice" if is_voice else "general"
    limit = _RATE_VOICE if is_voice else _RATE_GENERAL
    allowed, remaining, retry_after = _check_rate(ip, bucket_type, limit)
    if not allowed:
        log.warning(f"[{rid}] 限流 {ip} {path} (超过{limit}/min)")
        return JSONResponse(
            {"error": f"请求过于频繁, 请{retry_after}秒后重试"},
            status_code=429,
            headers={"Retry-After": str(retry_after)}
        )
    try:
        response = await call_next(request)
    except Exception as e:
        dur = (_t.time() - start) * 1000
        _metrics.record(
            request.url.path,
            dur,
            ok=False,
            include_latency=path != "/api/metrics",
            include_in_metrics=path != "/api/metrics",
        )
        log.error(f"[{rid}] 异常: {e} ({dur:.0f}ms)")
        raise
    dur = (_t.time() - start) * 1000
    ok = response.status_code < 500
    conditional = request.method.upper() == "GET" and bool(request.headers.get("if-none-match"))
    not_modified = response.status_code == 304
    _metrics.record(
        request.url.path,
        dur,
        ok=ok,
        conditional=conditional,
        not_modified=not_modified,
        include_latency=path != "/api/metrics",
        include_in_metrics=path != "/api/metrics",
    )
    completion_message = f"[{rid}] {request.method} {request.url.path} → {response.status_code} {dur:.0f}ms"
    if response.status_code == 304:
        log.debug(completion_message)
    else:
        log.info(completion_message)
    response.headers["X-Request-ID"] = rid
    return response

MAX_TEXT_LENGTH = 500  # 文字输入上限

# ===== 音频转wav =====


# ===== 提醒管理 =====



# ===== 通知队列(Web客户端可轮询获取主动通知) =====
MAX_NOTIFICATIONS = 20
_notifications = deque(maxlen=MAX_NOTIFICATIONS)
_notifications_lock = threading.Lock()


def _append_notification(notification: dict) -> None:
    with _notifications_lock:
        _notifications.append(notification)


def _drain_notifications() -> list[dict]:
    with _notifications_lock:
        notifications = list(_notifications)
        _notifications.clear()
        return notifications

def _add_notification(text: str, ntype: str = "reminder"):
    """添加通知到队列+SSE推送"""
    notification = {
        "text": text, "type": ntype,
        "time": datetime.datetime.now().isoformat()
    }
    _append_notification(notification)
    if sse_client_count():
        _push_notification_to_sse(_sse_event(notification))  # SSE实时推送

# ===== SSE实时通知推送 =====
_main_loop = None  # 主线程event loop(启动时捕获)

def _push_notification_to_sse(event_frame: str):
    """推送已编码的 SSE 帧到所有已连接客户端(线程安全)"""
    global _main_loop
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

def _play_reminder_audio(text: str, reminder_id: int | None = None):
    """生成提醒语音并播放到默认音频输出(AirPods/扬声器)
    macOS原生用afplay; Linux/容器环境通过SSE推送给浏览器播放"""
    import platform as _platform
    tmp = None
    delivery_failed = False
    delivery_error = ""
    try:
        from voice_agent import tts_to_mp3
        _add_notification(text, "reminder")
        log.info(f"[reminder] TTS生成: {text}")
        audio = tts_to_mp3(f"主人，提醒您：{text}")
        if not audio or len(audio) < 100:
            raise RuntimeError("TTS返回空音频")

        if _platform.system() == "Darwin":
            # macOS: 用 afplay 直接播放到系统音频设备
            from voice_agent import runtime_temp_audio_path
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=runtime_temp_audio_path())
            tmp.write(audio)
            tmp.close()
            log.info(f"[reminder] 播放提醒语音 {len(audio)}字节(MP3): {text}")
            subprocess.run(["afplay", tmp.name], timeout=30, capture_output=True)
            log.info("[reminder] 播放完成")
        else:
            # Linux/容器: 通过 SSE 推送音频给所有连接的浏览器客户端
            import base64 as _b64
            audio_b64 = _b64.b64encode(audio).decode()
            _push_notification_to_sse(_sse_event({"type": "audio", "audio": audio_b64, "source": "reminder"}))
            log.info(f"[reminder] 通过SSE推送提醒语音 {len(audio)}字节: {text}")

        if reminder_id is not None:
            complete_reminder_delivery(reminder_id)
    except Exception as e:
        delivery_failed = True
        delivery_error = str(e)
        log.error(f"[reminder] 播放失败: {e}")
        if reminder_id is not None:
            release_failed_reminder(reminder_id, datetime.datetime.now(), delivery_error)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass
_REMINDER_SCHEDULER_STOP = False


def _reminder_scheduler():
    """后台守护线程：每30s检查到期提醒，自动播报"""
    global _scheduler_lock_handle
    log.info("[reminder] 提醒调度器已启动，正在竞争机器级调度锁")
    cleanup_counter = 0
    while True:
        try:
            if _scheduler_lock_handle is None:
                _scheduler_lock_handle = acquire_scheduler_lock()
                if _scheduler_lock_handle is None:
                    time.sleep(30)
                    continue
                log.info("[reminder] 已获取机器级调度锁，开始检查到期提醒")
            cleanup_counter += 1
            if cleanup_counter >= 20:
                cleanup_counter = 0
                from utils import cleanup_temp_files, truncate_history_file
                from voice_agent import runtime_temp_audio_path
                cleanup_temp_files(extra_dirs=[runtime_temp_audio_path()])
                truncate_history_file(HISTORY_FILE, 100)
            now = datetime.datetime.now()
            due_reminders = claim_due_reminders(now)
            for reminder in due_reminders:
                rid = reminder.get("id", 0)
                text = reminder.get("text", "提醒")
                due_str = reminder.get("due", "")
                log.info(f"[reminder] 提醒到期(id={rid}): {text} (due={due_str})")
                _play_reminder_audio(text, reminder_id=rid)
        except Exception as e:
            log.error(f"[reminder] 调度器异常: {e}")
        time.sleep(30)

def _start_scheduler():
    t = threading.Thread(target=_reminder_scheduler, daemon=True)
    t.start()

# ===== 主动建议(天气/时间感知) =====
AMAP_KEY = os.getenv("AMAP_KEY", "")
SUGGEST_STATE_FILE = SUGGESTIONS_STATE_FILE
SUGGEST_STATE_LOCK_FILE = SUGGEST_STATE_FILE + ".lock"
_SUGGESTIONS_DEFAULT_STATE = {
    "last_weather_check": 0,
    "last_rain_suggest": "",
    "last_time_suggest": "",
    "last_health_alert": "",
}
SUGGESTIONS_STATE = dict(_SUGGESTIONS_DEFAULT_STATE)
_suggest_state_lock = threading.Lock()
_proactive_lock_handle = None
_proactive_thread = None


@contextmanager
def _locked_suggest_state(shared: bool = False):
    os.makedirs(os.path.dirname(SUGGEST_STATE_FILE), exist_ok=True)
    with _suggest_state_lock:
        with open(SUGGEST_STATE_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read_locked_suggest_state() -> dict:
    try:
        with open(SUGGEST_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_locked_suggest_state(state: dict) -> None:
    directory = os.path.dirname(SUGGEST_STATE_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".suggestions_state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SUGGEST_STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _refresh_suggestions_state(data: dict) -> None:
    SUGGESTIONS_STATE.clear()
    SUGGESTIONS_STATE.update(_SUGGESTIONS_DEFAULT_STATE)
    SUGGESTIONS_STATE.update(data)

def _load_suggest_state():
    try:
        with _locked_suggest_state(shared=True):
            data = _read_locked_suggest_state()
        _refresh_suggestions_state(data)
    except Exception:
        pass

def _save_suggest_state():
    try:
        _update_suggest_state({})
    except Exception:
        pass


def _suggest_state_snapshot() -> dict:
    with _locked_suggest_state(shared=True):
        data = _read_locked_suggest_state()
    state = dict(_SUGGESTIONS_DEFAULT_STATE)
    state.update(data)
    _refresh_suggestions_state(state)
    return dict(state)


def _update_suggest_state(updates: dict) -> dict:
    with _locked_suggest_state(shared=False):
        state = dict(_SUGGESTIONS_DEFAULT_STATE)
        state.update(_read_locked_suggest_state())
        state.update(updates)
        _write_locked_suggest_state(state)
        _refresh_suggestions_state(state)
        return dict(state)


def _claim_suggest_state(key: str, value) -> bool:
    with _locked_suggest_state(shared=False):
        state = dict(_SUGGESTIONS_DEFAULT_STATE)
        state.update(_read_locked_suggest_state())
        if state.get(key) == value:
            _refresh_suggestions_state(state)
            return False
        state[key] = value
        _write_locked_suggest_state(state)
        _refresh_suggestions_state(state)
        return True


_load_suggest_state()

def _get_weather():
    """直接调高德天气API(不走MCP，避免asyncio问题)"""
    try:
        r = requests.get(f"https://restapi.amap.com/v3/weather/weatherInfo",
            params={"city": "110000", "key": AMAP_KEY, "extensions": "all"}, timeout=10)
        data = r.json()
        if data.get("forecasts"):
            casts = data["forecasts"][0].get("casts", [])
            return casts
    except Exception as e:
        log.error(f"[suggest] 天气API失败: {e}")
    return []

def _forecast_for_date(casts, target_date: str) -> dict:
    """返回指定日期的预报；兼容缺少 date 字段的旧测试/API 返回。"""
    for cast in casts:
        if str(cast.get("date", "")).strip() == target_date:
            return cast
    if casts and all(not str(cast.get("date", "")).strip() for cast in casts):
        return casts[0]
    return {}

def _preference_state_key(pkey: str, pval: str) -> tuple[str, str]:
    """为偏好生成稳定且不会因中文截断而碰撞的当日去重键。"""
    fingerprint = hashlib.sha256(f"{pkey}\0{pval}".encode("utf-8")).hexdigest()[:16]
    return f"last_pref_{fingerprint}", fingerprint

def _proactive_suggestions():
    """后台守护线程：基于用户状态 + 时间 + 天气的主动推送建议"""
    global _proactive_lock_handle
    log.info("[suggest] 主动建议系统已启动，正在竞争机器级运行锁")
    _last_triggered_state = None
    while True:
        try:
            if _proactive_lock_handle is None:
                _proactive_lock_handle = acquire_proactive_lock()
                if _proactive_lock_handle is None:
                    time.sleep(60)
                    continue
                log.info("[suggest] 已获取机器级运行锁，开始状态感知建议")
            now = datetime.datetime.now()
            hour = now.hour
            today = now.strftime("%Y-%m-%d")
            suggest_state = _suggest_state_snapshot()

            # 获取用户状态机
            try:
                from voice_agent import get_user_state, update_user_state
                import psutil
                try:
                    screen_active = False
                    for proc in psutil.process_iter(['name', 'cpu_percent']):
                        try:
                            if proc.info['name'] in ('WindowServer', 'Dock', 'Finder'):
                                if proc.info['cpu_percent'] > 0.5:
                                    screen_active = True
                                    break
                        except Exception:
                            pass
                    if screen_active:
                        update_user_state(screen_active=True)
                except Exception:
                    pass
                user_state = get_user_state()
            except Exception:
                user_state = {"state": "unknown", "confidence": 0.0}

            state = user_state.get("state", "unknown")
            state_changed = (state != _last_triggered_state)
            if state_changed:
                log.info(f"[suggest] 用户状态变化: {_last_triggered_state} -> {state}")
                _last_triggered_state = state

            casts = None
            weather_loaded = False

            # 1. 天气建议(每小时检查一次)
            now_ts = time.time()
            if now_ts - float(suggest_state.get("last_weather_check", 0) or 0) > 3600:
                should_check_weather = _claim_suggest_state("last_weather_check", now_ts)
            else:
                should_check_weather = False
            if should_check_weather:
                casts = _get_weather()
                weather_loaded = bool(casts)
                today_forecast = _forecast_for_date(casts, today)
                if today_forecast:
                    c = today_forecast
                    weather_parts = []
                    for weather_name in (c.get("dayweather", ""), c.get("nightweather", "")):
                        weather_name = str(weather_name).strip()
                        if weather_name and weather_name not in weather_parts:
                            weather_parts.append(weather_name)
                    weather = " ".join(weather_parts)
                    if (
                        any("雨" in weather_name or "雪" in weather_name for weather_name in weather_parts)
                        and _claim_suggest_state("last_rain_suggest", today)
                    ):
                        msg = f"主人，今天天气预报有{weather}，出门记得带伞哦。"
                        _add_notification(msg, "weather")
                        log.info(f"[suggest] 主动天气建议: {msg}")
                        _play_reminder_audio(msg)

            # 2. 状态触发建议(替代纯时间触发)
            if state == "home_sleeping" and state_changed:
                if _claim_suggest_state("last_sleep_scene", today):
                    msg = "检测到你已休息，晚安。已关闭空调和电视。"
                    _add_notification(msg, "sleep")
                    log.info(f"[suggest] 睡眠场景触发: {msg}")
                    _play_reminder_audio(msg)

            elif state == "away" and state_changed and _claim_suggest_state("last_away_scene", today):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                weather_info = f"今天{w}，{temp}度" if w and temp else ""
                msg = f"出门注意，{weather_info}。" if weather_info else "出门注意安全。"
                _add_notification(msg, "away")
                log.info(f"[suggest] 出门场景触发: {msg}")
                _play_reminder_audio(msg)

            elif state == "home_awake" and state_changed and _last_triggered_state == "away":
                if _claim_suggest_state("last_home_scene", today):
                    if not weather_loaded:
                        casts = _get_weather()
                    today_forecast = _forecast_for_date(casts, today)
                    w = today_forecast.get("dayweather", "") if today_forecast else ""
                    temp = today_forecast.get("daytemp", "") if today_forecast else ""
                    parts = ["欢迎回家！"]
                    if w and temp:
                        parts.append(f"今天{w}，{temp}度。")
                    pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                    if pending:
                        parts.append(f"今天还有{len(pending)}项待办。")
                    msg = "".join(parts)
                    _add_notification(msg, "home")
                    log.info(f"[suggest] 回家场景触发: {msg}")
                    _play_reminder_audio(msg)

            # 3. 时间建议(保留原逻辑, 作为状态推断的补充)
            if 8 <= hour < 10 and _claim_suggest_state("last_time_suggest", today + "_morning"):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                parts = [f"早上好主人！{'今天' + w + '，最高' + temp + '度，' if w and temp else ''}新的一天加油！"]
                pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                if pending:
                    todo = "、".join(r["text"] for r in pending)
                    parts.append(f"今天还有{len(pending)}项待办：{todo}。")
                else:
                    parts.append("今天没有待办事项，轻松一天！")
                msg = "".join(parts)
                _add_notification(msg, "morning")
                log.info(f"[suggest] 每日晨报: {msg}")
                _play_reminder_audio(msg)

            # 4. 系统健康监控
            import psutil as _ps
            cpu = _ps.cpu_percent(interval=None)
            mem = _ps.virtual_memory().percent
            health_key = today + f"_health_{hour}"
            if (cpu > 90 or mem > 95) and _claim_suggest_state("last_health_alert", health_key):
                msg = f"系统资源紧张：CPU使用率{cpu:.0f}%，内存{mem:.0f}%，建议关闭一些不必要的程序。"
                _add_notification(msg, "health")
                log.warning(f"[suggest] 系统健康告警: {msg}")
                _play_reminder_audio(msg)

            # 5. 基于用户偏好的主动建议
            try:
                from voice_agent import list_preferences
                prefs = list_preferences()
                for pkey, pval in prefs.items():
                    state_key, pref_fingerprint = _preference_state_key(str(pkey), str(pval))
                    pref_suggest_key = f"{today}_pref_{pref_fingerprint}"
                    suggestion = None
                    if "下班" in pkey or "下班" in pval:
                        if 17 <= hour < 19:
                            suggestion = f"主人，快到下班时间了({pval})，需要我帮你查查路况或叫个车吗？"
                    elif "食物" in pkey or "喜欢吃" in pkey:
                        if 11 <= hour < 13 or 17 <= hour < 19:
                            suggestion = f"到饭点了，我记得你喜欢{pval}，要不要帮你找附近的餐厅？"
                    elif "运动" in pkey or "锻炼" in pkey:
                        if 6 <= hour < 8 or 18 <= hour < 20:
                            suggestion = f"是你平时的运动时间，今天别忘了{pval}哦。"
                    elif "睡" in pkey or "休息" in pkey:
                        if 22 <= hour < 24:
                            bedtime_state = _suggest_state_snapshot().get("last_bedtime_suggest", "")
                            if not bedtime_state.startswith(today):
                                suggestion = f"你设定了{pkey}为{pval}，该准备休息了。"
                    if suggestion and _claim_suggest_state(state_key, pref_suggest_key):
                        _add_notification(suggestion, "preference")
                        log.info(f"[suggest] 偏好建议({pkey}): {suggestion}")
                        _play_reminder_audio(suggestion)
                        break
            except Exception as e:
                log.debug(f"[suggest] 偏好建议检查异常: {e}")

        except Exception as e:
            log.error(f"[suggest] 主动建议异常: {e}")
        time.sleep(60)

def _start_evolution():
    """后台自进化线程: 每30分钟分析一次对话历史，自动学习用户偏好"""
    def _evolve_loop():
        import time
        # 启动时立即学习一次
        try:
            from voice_agent import _get_brain, _classify_intent
            brain = _get_brain("magic-evolution")
            for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
                pass
            log.info("[evolution] 启动时自进化学习完成")
        except Exception as e:
            log.debug(f"[evolution] 启动时学习跳过: {e}")
        # 每30分钟学习一次
        while True:
            time.sleep(1800)
            try:
                brain = _get_brain("magic-evolution")
                for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
                    pass
                log.info("[evolution] 定时自进化学习完成")
            except Exception as e:
                log.debug(f"[evolution] 定时学习跳过: {e}")
    threading.Thread(target=_evolve_loop, daemon=True).start()


def _start_decision_engine():
    """后台自主决策线程: 每2分钟评估一次, 匹配则执行"""
    def _decision_loop():
        import time
        time.sleep(10)  # 启动后等10秒, 让其他系统就绪
        log.info("[decision] 自主决策引擎已启动")
        while True:
            try:
                # 获取用户状态
                from voice_agent import get_user_state
                user_state = get_user_state()
                # 加载决策引擎
                import importlib.util as _iu
                _spec = _iu.spec_from_file_location("magic_decisions", os.path.join(PROJECT_DIR, "magic-decisions.py"))
                if _spec and _spec.loader:
                    _dec = _iu.module_from_spec(_spec)
                    _spec.loader.exec_module(_dec)
                    # 评估决策
                    decisions = _dec.evaluate(user_state)
                    for rule in decisions:
                        if rule.get("confirm"):
                            # 需要确认的: 推送通知 + 设置待确认状态
                            msg = rule["action"].get("text", "")
                            if msg:
                                _add_notification(msg, "decision")
                                log.info(f"[decision] 推送建议(需确认): {msg}")
                                _play_reminder_audio(msg)
                                # 设置待确认状态: brain() 会检测用户回应
                                _dec.set_pending_confirmation(rule["id"], msg)
                        else:
                            # 自动执行: 加载Protocol执行器
                            try:
                                _scene_spec = _iu.spec_from_file_location("magic_scenes", os.path.join(PROJECT_DIR, "magic-scenes.py"))
                                if _scene_spec and _scene_spec.loader:
                                    _scene_mod = _iu.module_from_spec(_scene_spec)
                                    _scene_spec.loader.exec_module(_scene_mod)
                                    result = _dec.execute_decision(rule, _scene_mod.execute_protocol)
                                    if result:
                                        _add_notification(result[:200], "decision")
                                        _play_reminder_audio(result[:200])
                            except Exception as e:
                                log.warning(f"[decision] 执行失败: {e}")
            except Exception as e:
                log.debug(f"[decision] 评估异常: {e}")
            time.sleep(120)  # 每2分钟评估一次
    threading.Thread(target=_decision_loop, daemon=True).start()


def _start_wake_listener():
    def _wake_loop():
        import time
        time.sleep(20)
        try:
            import local_wake
            local_wake.start_wake_detector(_process_wake_command)
            log.info("[wake] local wake listener started")
        except Exception as e:
            log.warning(f"[wake] start failed: {e}")
    threading.Thread(target=_wake_loop, daemon=True).start()


def _process_wake_command(wav_bytes: bytes):
    from voice_agent import voice_loop
    try:
        text, reply, audio_out = voice_loop(wav_bytes, "wav")
        if text and reply:
            log.info(f"[wake] cmd: asr={text[:30]} reply={reply[:30]}")
            _add_notification(f"{text[:50]} -> {reply[:50]}", "wake")
            if audio_out:
                import local_wake
                local_wake._play_audio(audio_out)
    except Exception as e:
        log.warning(f"[wake] cmd failed: {e}")


def _start_proactive():
    global _proactive_thread
    if _proactive_thread is not None and _proactive_thread.is_alive():
        return
    _proactive_thread = threading.Thread(target=_proactive_suggestions, daemon=True)
    _proactive_thread.start()



# ===== 预热大脑(修复asyncio子线程问题) =====

# ===== API 路由 =====

@app.post("/api/voice")
async def voice_api(file: UploadFile = File(...)):
    data = await file.read()
    ext = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    if len(data) > MAX_AUDIO_SIZE:
        log.warning(f"/api/voice 音频过大: {len(data)}字节 (>{MAX_AUDIO_SIZE})")
        return JSONResponse({"error": f"音频过大({len(data)//1024}KB), 上限{MAX_AUDIO_SIZE//1024//1024}MB"}, status_code=413)
    log.info(f"/api/voice 收到音频: {len(data)}字节, 格式={ext}")
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        from voice_agent import EMPTY_ASR_REPLY, EMPTY_ASR_TEXT
        log.info("/api/voice 本地判定为长静音，短路ASR、大脑和TTS")
        return {
            "text": EMPTY_ASR_TEXT,
            "reply": EMPTY_ASR_REPLY,
            "audio": "",
            "format": "mp3",
            "degraded": True,
        }
    from voice_agent import voice_loop
    try:
        text, reply, audio_out = await asyncio.wait_for(
            asyncio.to_thread(voice_loop, wav, "wav"), timeout=60)
        mp3_out = _wav_to_mp3(audio_out)
        degraded = not mp3_out
        log.info(f"/api/voice 完成: 识别={text[:30]} 回复={reply[:30]} WAV={len(audio_out)}→MP3={len(mp3_out)}字节 degraded={degraded}")
        import base64 as _b64
        return {
            "text": text, "reply": reply,
            "audio": _b64.b64encode(mp3_out).decode(),
            "format": "mp3", "degraded": degraded,
        }
    except asyncio.TimeoutError:
        log.error("/api/voice 超时(60s)")
        return JSONResponse({"error": "处理超时，请重试"}, status_code=504)
    except Exception as e:
        log.error(f"/api/voice 异常: {e}")
        from utils import sanitize_error
        return JSONResponse({"error": sanitize_error(str(e))}, status_code=500)

# ===== 流式端点: 大脑逐句产出 → TTS批量推送(SSE) =====
_TTS_BATCH_SIZE = 30  # TTS批量大小(字符数)，降低延迟
TTS_DEGRADED_MESSAGE = "语音服务繁忙，本轮先显示文字回复。"
BRAIN_BUSY_MESSAGE = "大脑服务繁忙，请稍后再试。"
_SSE_DONE_FRAME = 'data: {"type":"done"}\n\n'
_SSE_HEARTBEAT_FRAME = ': heartbeat\n\n'


def _sse_event(event: dict) -> str:
    """Serialize one compact SSE data frame."""
    return f'data: {json.dumps(event, ensure_ascii=False, separators=(",", ":"))}\n\n'


_SSE_EVENT_HEARTBEAT_FRAME = _sse_event({"type": "heartbeat", "text": "", "time": ""})


def _friendly_brain_error(error: Exception) -> str:
    """Keep upstream brain errors in logs while returning a stable client message."""
    from utils import sanitize_error

    raw_message = str(error)
    lowered = raw_message.lower()
    if "429" in raw_message or "too many requests" in lowered or "rate limit" in lowered or "限流" in raw_message:
        log.warning(f"大脑服务限流，返回友好提示: {raw_message}")
        return BRAIN_BUSY_MESSAGE
    log.error(f"大脑流式生成失败: {raw_message}")
    return sanitize_error(raw_message)

def _flush_tts_buffer(tts_buffer: str) -> str:
    """为已清洗文本生成TTS音频，返回base64 MP3(空则返回'')"""
    from voice_agent import _tts_cleaned_to_mp3
    if not tts_buffer or len(tts_buffer) < 2:
        return ""
    mp3 = _tts_cleaned_to_mp3(tts_buffer)
    if not mp3 or len(mp3) < 100:
        return ""
    return _b64enc.b64encode(mp3).decode()

def _empty_asr_events(as_event_stream: bool):
    """空 ASR 的降级事件：展示用户可读提示，但不写入大脑历史或触发 TTS。"""
    from voice_agent import EMPTY_ASR_REPLY
    text_event = {"type": "text", "text": EMPTY_ASR_REPLY}
    done_event = {"type": "done"}
    if as_event_stream:
        yield _sse_event(text_event)
        yield _sse_event(done_event)
    else:
        yield text_event
        yield done_event

def _low_intent_asr_events(asr_text: str, as_event_stream: bool):
    """语气词 ASR 的本地确认事件：展示 ASR 与轻量回复，但不进入大脑、历史或 TTS。"""
    asr_event = {"type": "asr", "text": asr_text}
    text_event = {"type": "text", "text": LOW_INTENT_ASR_REPLY}
    done_event = {"type": "done"}
    if as_event_stream:
        yield _sse_event(asr_event)
        yield _sse_event(text_event)
        yield _sse_event(done_event)
    else:
        yield asr_event
        yield text_event
        yield done_event

async def _synthesize_tts_event(tts_buffer: str):
    """合成一段 TTS；失败时返回降级 warning，供上层停止本轮后续语音尝试。"""
    if not tts_buffer or len(tts_buffer) < 2:
        return "", None
    try:
        audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
        return audio_b64, None
    except Exception as e:
        log.warning(f"流式TTS失败，降级为文字: {e}")
        return "", {"type": "warning", "message": TTS_DEGRADED_MESSAGE}

async def _stream_brain_tts(text: str, asr_text: str = "", session_id: str = "default"):
    """
    流式大脑+TTS生成器(SSE事件流) — 并行版。
    brain 在后台线程逐句产出，TTS 合成在独立线程池并行执行，
    总耗时 = max(brain总时间, TTS总时间) 而非两者之和。
    """
    from voice_agent import brain_stream_sentences
    
    q = _queue.Queue()          # brain → 主循环
    tts_q = _queue.Queue()      # TTS线程 → 主循环 (并行)
    # 启动后台大脑线程, 逐句推送文本→TTS流
    brain_thread = None
    
    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(text, session_id):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", _friendly_brain_error(e), None))
        finally:
            q.put(("done", None, None))
    
    # 如果有ASR结果，先推送 asr + ack(回执), 再启动大脑
    if asr_text:
        yield _sse_event({"type": "asr", "text": asr_text})
        yield _sse_event({"type": "ack", "message": ACK_AFTER_ASR_MESSAGE})
    
    if brain_thread is None:
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
        """在后台线程合成TTS，完成后通过tts_q返回结果。"""
        try:
            if not text_to_synth or len(text_to_synth) < 2:
                tts_q.put(("result", text_to_synth, "", None))
                return
            audio_b64 = _flush_tts_buffer(text_to_synth)
            tts_q.put(("result", text_to_synth, audio_b64, None))
        except Exception as e:
            tts_q.put(("error", text_to_synth, None, {"type": "warning", "message": TTS_DEGRADED_MESSAGE}))
    
    while True:
        # 检查 brain 队列
        brain_item = None
        try:
            brain_item = q.get_nowait()
            total_wait = 0
        except _queue.Empty:
            pass
        
        # 检查 TTS 结果队列
        tts_result = None
        try:
            tts_result = tts_q.get_nowait()
        except _queue.Empty:
            pass
        
        if brain_item:
            etype, sentence, full_reply = brain_item
            if etype == "done":
                brain_done = True
                # brain 结束，但还有 pending TTS 任务 — 继续等待
                if not pending_tts and not tts_buffer.strip():
                    yield _SSE_DONE_FRAME
                    break
            elif etype == "error":
                yield _sse_event({"type": "error", "message": sentence})
                yield _SSE_DONE_FRAME
                break
            elif etype == "sentence":
                # 文本事件即时推送
                yield _sse_event({"type": "text", "text": sentence})
                # __MUSIC__ 标记不送TTS(前端浏览器播放)
                if sentence and len(sentence) >= 2 and not sentence.startswith("__MUSIC__") and sentence != "__MUSIC_STOP__":
                    tts_buffer = (tts_buffer + "， " + sentence) if tts_buffer else sentence
                    should_flush = not first_audio_sent
                    if should_flush and not tts_failed:
                        buf = tts_buffer
                        tts_buffer = ""
                        first_audio_sent = True
                        pending_tts.append(buf)
                        # 并行提交 TTS 到线程池
                        threading.Thread(target=_submit_tts, args=(buf,), daemon=True).start()
        
        if tts_result:
            rtype, txt, audio_b64, warning = tts_result
            if rtype == "result" and audio_b64:
                yield _sse_event({"type": "audio", "audio": audio_b64})
                if txt in pending_tts:
                    pending_tts.remove(txt)
            elif rtype == "error":
                if not tts_failed:
                    tts_failed = True
                    yield _sse_event(warning)
        
        # brain 结束 + 所有TTS完成 → 结束
        if brain_done and not pending_tts:
            if tts_buffer.strip() and not tts_failed:
                threading.Thread(target=_submit_tts, args=(tts_buffer,), daemon=True).start()
                pending_tts.append(tts_buffer)
                tts_buffer = ""
            elif not pending_tts:
                yield _SSE_DONE_FRAME
                break
        
        if not brain_item and not tts_result:
            if total_wait >= MAX_WAIT:
                yield _sse_event({"type": "error", "message": "思考超时"})
                yield _SSE_DONE_FRAME
                break
            yield _SSE_HEARTBEAT_FRAME
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            total_wait += HEARTBEAT_INTERVAL

@app.post("/api/chat/stream")
async def chat_stream_api(req: ChatRequest):
    """流式文字对话: 文字进 → 大脑逐句产出 → TTS批量推送(SSE)"""
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    session_id = req.session_id
    allowed, remaining, retry_after = _check_session_rate(session_id)
    if not allowed:
        log.warning(f"/api/chat/stream 会话限流 {session_id[:8]} (超过{_RATE_PER_SESSION}/min)")
        return JSONResponse(
            {"error": f"请求过于频繁, 请{retry_after}秒后重试"},
            status_code=429,
            headers={"Retry-After": str(retry_after)}
        )
    log.info(f"/api/chat/stream 流式对话: {text[:40]} (session={session_id[:8]})")
    return StreamingResponse(_stream_brain_tts(text, session_id=session_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/voice/stream")
async def voice_stream_api(request: Request, file: UploadFile = File(...), session_id: str = "default"):
    """流式语音对话: 音频进 → ASR → 大脑逐句 → TTS批量(SSE)"""
    data = await file.read()
    ext = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    if len(data) > MAX_AUDIO_SIZE:
        log.warning(f"/api/voice/stream 音频过大: {len(data)}字节 (>{MAX_AUDIO_SIZE})")
        return JSONResponse(
            {"error": f"音频过大({len(data)//1024}KB), 上限{MAX_AUDIO_SIZE//1024//1024}MB"},
            status_code=413,
        )
    ua = request.headers.get("user-agent", "?")[:80]
    log.info(f"/api/voice/stream 收到音频: {len(data)}字节, 格式={ext}, UA={ua}")
    
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        log.info("/api/voice/stream 本地判定为长静音，短路ASR、大脑和TTS")
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    from voice_agent import asr
    try:
        asr_text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "语音识别超时"}, status_code=504)
    except Exception as e:
        log.error(f"/api/voice/stream ASR异常: {e}")
        return JSONResponse({"error": f"识别失败: {e}"}, status_code=500)
    
    log.info(f"/api/voice/stream ASR结果: [{asr_text[:60]}] (len={len(asr_text or '')})")
    if not asr_text:
        log.info("/api/voice/stream ASR为空，短路大脑和TTS")
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if is_low_intent_asr(asr_text):
        log.info("/api/voice/stream ASR为低意图语气词，短路大脑和TTS")
        return StreamingResponse(_low_intent_asr_events(asr_text, True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # 保守版乱码检查: 只拦截纯虚词堆叠和纯标点(不拦截短句)
    from voice_agent import is_garbled_asr
    if is_garbled_asr(asr_text):
        log.info(f"/api/voice/stream ASR判定为乱码/碎片，短路大脑和TTS: [{asr_text[:30]}]")
        return StreamingResponse(_empty_asr_events(True), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    
    return StreamingResponse(_stream_brain_tts(asr_text, asr_text, session_id=session_id), 
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/chat")
async def chat_api(req: ChatRequest):
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    allowed, remaining, retry_after = _check_session_rate(req.session_id)
    if not allowed:
        log.warning(f"/api/chat 会话限流 {req.session_id[:8]} (超过{_RATE_PER_SESSION}/min)")
        return JSONResponse(
            {"error": f"请求过于频繁, 请{retry_after}秒后重试"},
            status_code=429,
            headers={"Retry-After": str(retry_after)}
        )
    from voice_agent import brain
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(brain, text, req.session_id), timeout=60)
        return {"reply": reply}
    except asyncio.TimeoutError:
        log.error("/api/chat 超时(30s)")
        return JSONResponse({"error": "思考超时，请重试"}, status_code=504)
    except Exception as e:
        log.error(f"/api/chat 异常: {e}")
        # 优雅降级: 返回友好提示而非500错误
        fallback = "抱歉，我现在有点忙不过来，请稍等一下再试。"
        log.info("[chat] 大脑失败，返回降级回复")
        return {"reply": fallback, "degraded": True}

@app.post("/api/reset")
async def reset_conversation(session_id: str = "default"):
    """清空指定会话的对话历史"""
    from voice_agent import reset_history
    reset_history(session_id)
    return {"ok": True, "message": "对话已重置", "session_id": session_id}


@app.get("/api/reminders")
async def list_reminders(request: Request):
    cached = _file_not_modified_response(request, REMINDERS_FILE, "reminders")
    if cached is not None:
        return cached
    reminders_token = _file_etag_token(REMINDERS_FILE, "reminders")
    data = _load_reminders()
    reminders_token_after_load = _file_etag_token(REMINDERS_FILE, "reminders")
    pending = [r for r in data if not r.get("done")]
    return _json_response(
        request,
        {"total": len(data), "pending": len(pending), "reminders": data},
        etag_token=reminders_token if reminders_token == reminders_token_after_load else None,
    )

@app.post("/api/reminders")
async def add_reminder(req: ReminderRequest):
    text = _sanitize_text(req.text, 200)
    time_str = _sanitize_text(req.time, 50)
    repeat = _sanitize_text(req.repeat, 20) if req.repeat else ""
    # 复用共享时间解析工具
    due = None
    if time_str:
        from utils import parse_time_str
        due = parse_time_str(time_str)
    if repeat:
        item = append_reminder(text, time_str, due, repeat=repeat)
    else:
        # 不带 repeat 参数 → 原子事务默认不重复
        item = append_reminder(text, time_str, due)
    rid = item["id"]
    when = f"，提醒时间{due.replace('T', ' ')}" if due else (f"（时间'{time_str}'未解析出时刻）" if time_str else "")
    repeat_desc = {"daily": "（每天重复）", "weekly": "（每周重复）", "weekdays": "（工作日重复）"}.get(repeat, "")
    return {"ok": True, "id": rid, "message": f"已添加提醒：{text}{when}{repeat_desc}"}

@app.delete("/api/reminders/{rid}")
async def delete_reminder(rid: int):
    if not complete_reminder(rid):
        raise HTTPException(404, "提醒不存在")
    return {"ok": True, "message": f"提醒{rid}已标记完成"}

@app.get("/api/conversation")
async def get_conversation(page: int = 1, limit: int = 50, session_id: str = "default"):
    """获取对话历史(支持分页)
    page: 页码(从1开始), limit: 每页条数(默认50)
    """
    from voice_agent import _history_snapshot
    hist = _history_snapshot(session_id)
    total = len(hist)
    # 分页计算
    start = max(0, (page - 1) * limit)
    end = start + limit
    items = hist[start:end]
    return {
        "history": items,
        "count": len(items),
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": end < total,
    }

@app.post("/api/tts")
async def tts_api(req: TTSRequest):
    """文字 → 语音(MP3)"""
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

@app.post("/api/asr")
async def asr_api(file: UploadFile = File(...)):
    """音频 → 文字"""
    data = await file.read()
    ext = (file.filename or "audio.wav").rsplit(".", 1)[-1].lower()
    wav = to_wav(data, ext)
    if likely_empty_audio(wav):
        log.info("/api/asr 本地判定为长静音，短路远端ASR")
        return {"text": ""}
    from voice_agent import asr
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(asr, wav, "wav"), timeout=30)
        log.info(f"/api/asr 识别结果: [{text}] (len={len(text)}, 音频={len(data)}字节)")
        return {"text": text}
    except asyncio.TimeoutError:
        return JSONResponse({"error": "ASR超时"}, status_code=504)

# ===== Vosk 唤醒词检测(Charlie) =====
_VOSK_MODEL = None
_VOSK_WAKE_WORDS = ["charlie", "charley", "charls", "charles", "チャーリー", "查理", "查里", "chali", "chali", "charli",
                    "查理", "查莉", "查利", "查里", "茶理", "嘞查嘞", "嘞查", "查嘞"]

def _get_vosk_model():
    global _VOSK_MODEL
    if _VOSK_MODEL is None:
        try:
            from vosk import Model
            model_path = os.path.join(os.path.dirname(__file__), "web", "vosk", "vosk-model-small-en-us-0.15")
            if os.path.exists(model_path):
                _VOSK_MODEL = Model(model_path)
                log.info("Vosk 英文唤醒词模型已加载")
        except Exception as e:
            log.warning(f"Vosk 模型加载失败(不影响正常使用): {e}")
    return _VOSK_MODEL

@app.post("/api/wakecheck")
async def wakecheck_api(file: UploadFile = File(...)):
    """Vosk 唤醒词检测: 返回是否包含 'Charlie'"""
    import wave, io, json as json_mod
    data = await file.read()
    if len(data) < 1000:
        return {"wake": False, "text": ""}
    # 转 WAV 16kHz mono — 用 ffmpeg 自动检测格式(不指定-f, 让ffmpeg自己猜)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            input=data, capture_output=True, timeout=15)
        wav = r.stdout if r.returncode == 0 and r.stdout and len(r.stdout) > 100 else data
    except Exception:
        wav = data
    model = _get_vosk_model()
    if model is None:
        # fallback: 用原有的 ASR
        from voice_agent import asr
        try:
            text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=15)
            text_lower = text.lower().strip()
            matched = any(w in text_lower for w in _VOSK_WAKE_WORDS)
            return {"wake": matched, "text": text}
        except:
            return {"wake": False, "text": ""}
    try:
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, 16000)
        wf = wave.open(io.BytesIO(wav), 'rb')
        # 分块送入 Vosk (每块 4000 frames = 0.25s)
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
        # 取最终结果
        final = json_mod.loads(rec.FinalResult())
        if final.get("text"):
            text = final["text"]
        text = text.lower()
        matched = any(w in text for w in _VOSK_WAKE_WORDS)
        log.info(f"/api/wakecheck Vosk: [{text}] matched={matched} (audio={len(data)}B wav={len(wav)}B)")
        return {"wake": matched, "text": text}
    except Exception as e:
        log.warning(f"/api/wakecheck 异常: {e}")
        return {"wake": False, "text": ""}

@app.get("/api/export")
async def export_conversation(
    format: str = "txt",
    session_id: str = "default",
    from_date: str | None = None,
    to_date: str | None = None,
):
    """导出对话历史(支持txt/markdown/json格式, 按会话和日期筛选)"""
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
        import json as _j
        return Response(content=_j.dumps(hist, ensure_ascii=False, indent=2).encode("utf-8"),
                       media_type="application/json",
                       headers={"Content-Disposition": "attachment; filename=conversation.json"})
    
    if format in ("markdown", "md"):
        lines = ["# Charlie · 对话记录\n"]
        for m in hist:
            role = "🙋 我" if m.get("role") == "user" else "🤖 Charlie"
            lines.append(f"### {role}\n\n{m.get('content', '')}\n")
        lines.append(f"\n---\n*共{len(hist)}条消息 · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}导出*")
        text = "\n".join(lines)
        return Response(content=text.encode("utf-8"), media_type="text/markdown",
                       headers={"Content-Disposition": "attachment; filename=conversation.md"})
    
    # 默认txt格式(带时间戳)
    lines = []
    for m in hist:
        role = "我" if m.get("role") == "user" else "Charlie"
        ts = m.get("ts", "")[:19].replace("T", " ") if m.get("ts") else ""
        ts_prefix = f"[{ts}] " if ts else ""
        lines.append(f"[{role}] {ts_prefix}{m.get('content', '')}")
    text = "\n\n".join(lines)
    return Response(content=text.encode("utf-8"), media_type="text/plain",
                   headers={"Content-Disposition": "attachment; filename=conversation.txt"})

@app.get("/api/notifications")
async def get_notifications():
    """获取并清空通知队列(Web客户端轮询用)"""
    notifs = _drain_notifications()
    return {"count": len(notifs), "notifications": notifs}

@app.get("/api/events")
async def sse_events():
    """SSE实时通知流(Web客户端用EventSource连接, 免轮询)"""
    queue = asyncio.Queue()
    register_sse_client(queue)

    async def event_stream():
        try:
            # 发送连接确认
            yield _sse_event({
                "type": "connect",
                "text": "已连接",
                "time": datetime.datetime.now().isoformat(),
            })
            while True:
                try:
                    event_frame = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event_frame
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield _SSE_EVENT_HEARTBEAT_FRAME
        except asyncio.CancelledError:
            pass
        finally:
            unregister_sse_client(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})




# ===== API令牌认证(保护公网访问) =====



# ===== 输入清洗(防XSS/注入) =====

# ===== WebSocket 双向通信(实时语音/文字, 支持打断TTS) =====
_WS_STALE_TIMEOUT = 300  # 5分钟无活动视为过期

def _ws_cleanup_stale():
    """清理过期的WebSocket连接(5分钟无活动)"""
    now = time.time()
    stale = [sid for sid, info in list(_ws_clients.items())
             if now - info.get("last_active", now) > _WS_STALE_TIMEOUT]
    for sid in stale:
        _ws_cleanup_after_disconnect(sid, close_connection=True)
    if stale:
        log.info(f"[ws] 清理{len(stale)}个过期连接, 剩余{len(_ws_clients)}个")

def _start_ws_cleanup():
    """启动WebSocket过期连接清理线程(每60秒)"""
    def _cleanup_loop():
        while True:
            time.sleep(60)
            _ws_cleanup_stale()
            # 清理过期会话桶(防内存泄漏)
            now = time.time()
            with _RATE_LOCK:
                expired = [k for k, v in _session_buckets.items()
                           if not v or now - v[-1] > _RATE_WINDOW * 2]
                for k in expired:
                    del _session_buckets[k]
    threading.Thread(target=_cleanup_loop, daemon=True).start()

async def _ws_stream_and_send(
    ws,
    ws_id: int,
    *,
    text: str,
    asr_text: str = "",
    session_id: str,
    interrupted_reply: str = "",
):
    """后台运行大脑流式生成+发送事件; 可被interrupt标志或任务取消打断。
    后台任务模式下receive循环不被阻塞, 客户端可随时发送interrupt。
    跨终端：回复同时广播给同一 session 的所有终端。"""
    try:
        if asr_text:
            await _ws_broadcast_to_session(
                ws_id,
                {"type": "asr", "text": asr_text},
                exclude_self=True,
            )
        async for event in _ws_stream_brain(
            ws_id,
            text,
            session_id,
            interrupted_reply=interrupted_reply,
        ):
            info = _ws_clients.get(ws_id)
            if not info or info.get("stream_task") is not asyncio.current_task():
                break
            await ws.send_json(event)
            # 跨终端广播（text/audio/done/error 事件发给同 session 其他终端）
            if event.get("type") in ("text", "audio", "done", "error"):
                await _ws_broadcast_to_session(ws_id, event, exclude_self=True)
    except asyncio.CancelledError:
        raise  # 由调用方发送interrupted, 这里只清理
    except Exception as e:
        log.error(f"[ws] 流式任务异常 (id={ws_id}): {e}")
        try:
            await ws.send_json({"type": "error", "message": str(e)[:60]})
        except Exception:
            pass
    finally:
        info = _ws_clients.get(ws_id)
        if info and info.get("stream_task") is asyncio.current_task():
            info["stream_task"] = None

def _ws_cancel_stream(ws_id: int):
    """打断当前流式任务: 设interrupt标志 + 取消后台task(即时停止发送)"""
    info = _ws_clients.get(ws_id)
    if not info:
        return
    info["interrupt"] = True
    task = info.get("stream_task")
    if task and not task.done():
        task.cancel()

def _ws_join_session(ws_id: int, session_id: str):
    """把 WebSocket 加入会话组，供后续流式回复跨终端同步。"""
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

async def _ws_broadcast_to_session(ws_id: int, event: dict, exclude_self: bool = True):
    """跨终端广播：把事件发给同一 session 的所有客户端（排除发送者）"""
    info = _ws_clients.get(ws_id)
    if not info:
        return
    session_id = info.get("session_id", "default")
    peers = _ws_session_groups.get(session_id, [])
    for pid in peers:
        if exclude_self and pid == ws_id:
            continue
        pinfo = _ws_clients.get(pid)
        if pinfo:
            try:
                await pinfo["ws"].send_json(event)
            except Exception:
                pass

async def _ws_reverse_geocode(lat: float, lng: float) -> str:
    """高德地图反向地理编码：经纬度→地址"""
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

def _ws_cleanup_after_disconnect(ws_id: int, close_connection: bool = False):
    """取消连接上的后台流式任务, 并可从后台线程安全关闭WebSocket。"""
    info = _ws_clients.pop(ws_id, None)
    if not info:
        return

    _interrupt_telemetry.discard_pending(ws_id)

    task = info.get("stream_task")
    if task and not task.done():
        task.cancel()

    # 从会话组移除
    session_id = info.get("session_id", "default")
    if session_id in _ws_session_groups:
        try:
            _ws_session_groups[session_id].remove(ws_id)
        except ValueError:
            pass
        if not _ws_session_groups[session_id]:
            del _ws_session_groups[session_id]

    # 清除位置
    _ws_client_locations.pop(ws_id, None)

    if close_connection:
        ws = info.get("ws")
        if ws and _main_loop and not _main_loop.is_closed():
            try:
                _main_loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(ws.close()))
            except RuntimeError:
                pass

@app.api_route("/xiaozhi/ota", methods=["GET", "POST"])
async def xiaozhi_ota(request: Request):
    """OTA config endpoint for xiaozhi firmware. Returns websocket connection info
    so the device skips activation and connects directly to /ws/xiaozhi.
    The firmware POSTs board info here; we ignore the body and return WS config."""
    host = request.url.hostname or "192.168.1.3"
    ws_url = f"ws://{host}:8000/ws/xiaozhi"
    return JSONResponse({
        "websocket": {
            "url": ws_url,
            "version": 1,
        },
        "server_time": {
            "timestamp": int(datetime.datetime.now().timestamp()),
            "timezone_offset": 480,
        }
    })

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket双向通信端点
    
    客户端发送:
      {"type":"text","message":"你好"}          → 文字对话
      {"type":"audio","data":"base64...","format":"wav"} → 语音对话
      {"type":"interrupt"}                       → 打断当前TTS播放
      {"type":"ping"}                            → 心跳
    
    服务端返回:
      {"type":"asr","text":"识别结果"}            → ASR结果
      {"type":"text","text":"回复文字"}          → 大脑回复(逐句)
      {"type":"audio","data":"base64..."}        → TTS音频(MP3)
      {"type":"done"}                            → 回复完成
      {"type":"error","message":"..."}           → 错误
      {"type":"pong"}                            → 心跳回复
      {"type":"location","lat":31.23,"lng":121.47,"accuracy":10} → 浏览器GPS定位
    """
    await ws.accept()
    # WebSocket认证
    if AUTH_TOKEN:
        ci = ws.client.host if ws.client else ""
        if ci not in ("127.0.0.1", "::1", ""):
            tk = ws.query_params.get("token", "")
            if tk != AUTH_TOKEN:
                await ws.close(code=4001, reason="未授权")
                return
    ws_id = id(ws)
    _ws_clients[ws_id] = {"ws": ws, "interrupt": False, "last_active": time.time(), "stream_task": None}
    log.info(f"[ws] 客户端已连接 (id={ws_id}), 共{len(_ws_clients)}个连接")
    
    # 发送连接确认
    await ws.send_json({"type": "connect", "text": "Charlie已连接", 
                        "time": datetime.datetime.now().isoformat()})
    
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=120)
                _ws_clients[ws_id]["last_active"] = time.time()
            except asyncio.TimeoutError:
                # 2分钟无消息, 发心跳检测
                await ws.send_json({"type": "ping"})
                continue
            
            try:
                data = json.loads(msg)
                # 处理剪贴板内容
                if data.get('type') == 'clipboard':
                    text = data.get('text', '')
                    log.info(f"[ws] 剪贴板: {text[:50]}...")
                    continue
                # 处理设备发现
                if data.get('type') == 'discover':
                    await ws.send_json({"type": "discover_ack", "peers": len(_ws_clients)})
                    continue
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "消息格式错误,需要JSON"})
                continue
            
            mtype = data.get("type", "")
            
            if mtype == "ping":
                await ws.send_json({"type": "pong", "time": datetime.datetime.now().isoformat()})
                continue
            
            if mtype == "interrupt":
                # 设打断标志 + 取消流式任务(后台任务模式下可即时收到)
                interrupted_reply = data.get("interrupted_reply", "")
                _ws_cancel_stream(ws_id)
                _interrupt_telemetry.record(ws_id, interrupted_reply)
                log.info(f"[ws] 客户端请求打断TTS (id={ws_id}), 被打断回复: {interrupted_reply[:60]}")
                await ws.send_json({"type": "interrupted"})
                continue

            if mtype == "location":
                # 浏览器 GPS 定位
                lat = data.get("lat")
                lng = data.get("lng")
                acc = data.get("accuracy", 0)
                if lat is None or lng is None:
                    await ws.send_json({"type": "error", "message": "缺少经纬度"})
                    continue
                _ws_client_locations[ws_id] = {
                    "lat": lat, "lng": lng, "accuracy": acc,
                    "time": datetime.datetime.now().isoformat()
                }
                # 更新用户状态机(位置信息)
                try:
                    from voice_agent import update_user_state
                    update_user_state(location=(lat, lng))
                except Exception:
                    pass
                # 反向地理编码
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
                # 注册到会话组（跨终端同步）
                _ws_join_session(ws_id, session_id)
                allowed, _, _ = _check_session_rate(session_id)
                if not allowed:
                    log.warning(f"[ws] 会话限流 (id={ws_id}, session={session_id[:8]})")
                    await ws.send_json({"type": "error", "message": "请求过于频繁，请稍后再试"})
                    continue
                interrupted_reply = _interrupt_telemetry.record_follow_up(ws_id, text, "text")
                # 取消上一轮流式任务(如有), 重置打断标志
                _ws_cancel_stream(ws_id)
                _ws_clients[ws_id]["interrupt"] = False
                log.info(f"[ws] 文字对话: {text[:40]} (session={session_id[:8]})")
                # 流式处理大脑回复(后台任务, 不阻塞receive循环→可即时响应打断)
                task = asyncio.create_task(_ws_stream_and_send(
                    ws,
                    ws_id,
                    text=text,
                    session_id=session_id,
                    interrupted_reply=interrupted_reply,
                ))
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
                allowed, _, _ = _check_session_rate(ws_session_id)
                if not allowed:
                    log.warning(f"[ws] 会话限流 (id={ws_id}, session={ws_session_id[:8]})")
                    await ws.send_json({"type": "error", "message": "请求过于频繁，请稍后再试"})
                    continue
                # 取消上一轮流式任务(如有), 重置打断标志
                _ws_cancel_stream(ws_id)
                _ws_clients[ws_id]["interrupt"] = False
                log.info(f"[ws] 语音对话: {len(raw)}字节, 格式={fmt}")
                # ASR(内联, ~800ms; 用户刚说完话不会在此期间打断)
                wav = to_wav(raw, fmt)
                if likely_empty_audio(wav):
                    log.info(f"[ws] 本地判定为长静音，短路ASR、大脑和TTS (id={ws_id})")
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
                    for event in _empty_asr_events(False):
                        await ws.send_json(event)
                    continue
                from voice_agent import is_garbled_asr as _is_garbled_asr
                if is_low_intent_asr(asr_text):
                    log.info(f"[ws] ASR为低意图语气词，短路大脑和TTS (id={ws_id})")
                    for event in _low_intent_asr_events(asr_text, False):
                        await ws.send_json(event)
                    continue
                if _is_garbled_asr(asr_text):
                    log.info(f"[ws] ASR为乱码/碎片，短路大脑和TTS (id={ws_id})")
                    for event in _empty_asr_events(False):
                        await ws.send_json(event)
                    continue
                asr_event = {"type": "asr", "text": asr_text}
                await ws.send_json(asr_event)
                # 本地回执先到(给用户即时反馈), 再启动后台流式任务
                await ws.send_json({"type": "ack", "message": ACK_AFTER_ASR_MESSAGE})
                interrupted_reply = _interrupt_telemetry.record_follow_up(ws_id, asr_text, "asr")
                # 流式大脑回复(后台任务, 不阻塞receive循环→可即时响应打断)
                task = asyncio.create_task(_ws_stream_and_send(
                    ws,
                    ws_id,
                    text=asr_text,
                    asr_text=asr_text,
                    session_id=ws_session_id,
                    interrupted_reply=interrupted_reply,
                ))
                _ws_clients[ws_id]["stream_task"] = task
                continue
            
            # 未知类型
            await ws.send_json({"type": "error", "message": f"未知消息类型: {mtype}"})
    
    except WebSocketDisconnect:
        log.info(f"[ws] 客户端断开 (id={ws_id})")
    except Exception as e:
        log.error(f"[ws] 异常 (id={ws_id}): {e}")
    finally:
        _ws_cleanup_after_disconnect(ws_id)
        log.info(f"[ws] 连接清理完成 (id={ws_id}), 剩余{len(_ws_clients)}个")

async def _ws_stream_brain(
    ws_id: int,
    text: str,
    session_id: str = "default",
    interrupted_reply: str = "",
):
    """WebSocket专用流式大脑+TTS生成器(检查打断标志)"""
    from voice_agent import brain_stream_sentences
    
    q = _queue.Queue()
    
    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(
                text,
                session_id,
                interrupted_reply=interrupted_reply,
            ):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", _friendly_brain_error(e), None))
        finally:
            q.put(("done", None, None))
    
    threading.Thread(target=brain_worker, daemon=True).start()
    
    tts_buffer = ""
    tts_failed = False
    first_audio_sent = False  # 首段立即flush(降首音频延迟)
    total_wait = 0
    
    while True:
        # 检查打断标志
        if _ws_clients.get(ws_id, {}).get("interrupt"):
            break
        
        try:
            item = q.get_nowait()
            total_wait = 0
        except _queue.Empty:
            if total_wait >= 120:
                yield {"type": "error", "message": "思考超时"}
                yield {"type": "done"}
                break
            await asyncio.sleep(0.1)
            total_wait += 0.1
            continue
        
        etype, sentence, full_reply = item
        if etype == "done":
            # 推送剩余TTS
            if tts_buffer.strip() and not tts_failed:
                audio_b64, warning = await _synthesize_tts_event(tts_buffer)
                if warning:
                    tts_failed = True
                    yield warning
                elif audio_b64:
                    yield {"type": "audio", "data": audio_b64}
            yield {"type": "done"}
            break
        elif etype == "error":
            yield {"type": "error", "message": sentence}
            yield {"type": "done"}
            break
        elif etype == "sentence":
            # 检查打断
            if _ws_clients.get(ws_id, {}).get("interrupt"):
                break
            yield {"type": "text", "text": sentence}
            if sentence and len(sentence) >= 2 and not sentence.startswith('__MUSIC__') and sentence != '__MUSIC_STOP__':
                tts_buffer = (tts_buffer + "，" + sentence) if tts_buffer else sentence
                if (not first_audio_sent):
                    if tts_failed:
                        tts_buffer = ""
                        first_audio_sent = True
                        continue
                    audio_b64, warning = await _synthesize_tts_event(tts_buffer)
                    tts_buffer = ""
                    first_audio_sent = True
                    if warning:
                        tts_failed = True
                        yield warning
                    elif audio_b64:
                        yield {"type": "audio", "data": audio_b64}
                        # 检查打断
                        if _ws_clients.get(ws_id, {}).get("interrupt"):
                            break


@app.get("/api/search")
async def search_conversation(q: str = "", session_id: str = "default", limit: int = 20, offset: int = 0):
    """搜索对话历史中的关键词(同时搜索内存+文件)"""
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
        # Relevance scoring: exact match > word boundary > substring
        score = 1  # base score
        if content_lower == q_lower:
            score = 100  # exact match
        elif q_lower in content_lower:
            # count occurrences for scoring
            count = content_lower.count(q_lower)
            score = min(50, 10 * count)
            # bonus for match at start
            if content_lower.startswith(q_lower):
                score += 20
        # Extract matched context with highlighting
        idx = content_lower.find(q_lower)
        start = max(0, idx - 25)
        end = min(len(content), idx + len(q) + 25)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""
        ctx = prefix + content[start:idx] + "[" + content[idx:idx+len(q)] + "]" + content[idx+len(q):end] + suffix
        ts = m.get("ts", "")[:19].replace("T", " ") if m.get("ts") else ""
        results.append({
            "role": role, "context": ctx, "full": content[:200],
            "score": score, "index": i, "timestamp": ts
        })
    # Sort by relevance score (descending)
    results.sort(key=lambda r: r["score"], reverse=True)
    # Pagination
    total = len(results)
    paginated = results[offset:offset+limit]
    return {"query": q, "count": len(paginated), "total": total,
            "offset": offset, "limit": limit, "results": paginated}





@app.api_route("/manifest.json", methods=["GET", "HEAD"])
async def manifest(request: Request):
    """PWA manifest for mobile install"""
    return _manifest_response(request)


@app.get("/service-worker.js")
async def service_worker(request: Request):
    """PWA Service Worker for offline support"""
    sw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "service-worker.js")
    if os.path.exists(sw_path):
        with open(sw_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="application/javascript",
                          headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
    return Response(status_code=404)


@app.get("/icon.svg")
async def icon_svg(request: Request):
    """PWA app icon"""
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "icon.svg")
    if os.path.exists(icon_path):
        with open(icon_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="image/svg+xml",
                          headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)
    return _manifest_response(request)



@app.post("/api/brain/restart")
async def restart_brain_api():
    """手动重启大脑(清除旧MCP连接, 下次请求重建)"""
    from voice_agent import restart_brain
    msg = restart_brain()
    log.info(f"[brain] 手动重启: {msg}")
    return {"ok": True, "message": msg}


@app.get("/api/metrics")
async def metrics(request: Request):
    """请求指标: 请求数/错误率/缓存命中/响应时间(p50/p95)"""
    return _json_response(
        request,
        lambda: _metrics.summary(exclude_endpoint="/api/metrics"),
        etag_token=_metrics.token(exclude_endpoint="/api/metrics"),
    )


# ===== 用户偏好管理API =====
class PreferenceRequest(BaseModel):
    """设置偏好请求"""
    key: str = Field(..., min_length=1, max_length=50, description="偏好键名(如'喜欢的食物')")
    value: str = Field(..., min_length=1, max_length=200, description="偏好值(如'意大利菜')")

@app.get("/api/preferences")
async def get_preferences(request: Request):
    """获取所有用户偏好"""
    from voice_agent import preferences_conditional
    prefs, prefs_token = preferences_conditional(
        lambda etag: _if_none_matches(request, etag),
        _weak_etag,
    )
    if prefs is None:
        return _not_modified_response(_weak_etag(prefs_token))
    return _json_response(
        request,
        {"total": len(prefs), "preferences": prefs},
        etag_token=prefs_token,
    )

@app.post("/api/preferences")
async def set_preference_api(req: PreferenceRequest):
    """设置用户偏好"""
    from voice_agent import set_preference
    msg = set_preference(req.key, req.value)
    return {"ok": True, "message": msg, "key": req.key, "value": req.value}

@app.delete("/api/preferences/{key}")
async def del_preference_api(key: str):
    """删除用户偏好"""
    from voice_agent import del_preference
    msg = del_preference(key)
    return {"ok": True, "message": msg}

@app.get("/api/sessions")
async def list_sessions():
    """列出所有活跃会话(多用户监控)"""
    from voice_agent import _session_summaries
    sessions = _session_summaries()
    return {"total": len(sessions), "sessions": sessions}

@app.get("/api/context")
async def get_context(session_id: str = "default"):
    """获取对话上下文摘要(调试用)"""
    from voice_agent import _context_summaries, _history_snapshot, list_preferences, _estimate_msg_tokens as _est_tokens
    hist = _history_snapshot(session_id)
    summary = _context_summaries.get(session_id, "")
    prefs = list_preferences()
    # 估算token使用
    total_tokens = sum(_est_tokens(m) for m in hist) if hist else 0
    return {
        "session_id": session_id[:16] + "..." if len(session_id) > 16 else session_id,
        "history_count": len(hist),
        "estimated_tokens": total_tokens,
        "token_budget": 4000,
        "context_summary": summary[:200] if summary else None,
        "preferences_count": len(prefs),
        "preferences": prefs,
    }

@app.get("/api/decisions")
async def decision_status():
    """决策引擎状态"""
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_decisions", os.path.join(PROJECT_DIR, "magic-decisions.py"))
        if _spec and _spec.loader:
            _dec = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_dec)
            try:
                from voice_agent import get_user_state
                user_state = get_user_state()
            except Exception:
                user_state = {"state": "unknown"}
            rules = _dec.get_rules()
            history = _dec._load_decision_history()
            return {
                "user_state": user_state,
                "rules": rules,
                "history": history,
                "summary": _dec.decisions_summary(),
            }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/memory")
async def memory_status():
    """叙事性记忆状态"""
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_memory", os.path.join(PROJECT_DIR, "magic-memory.py"))
        if _spec and _spec.loader:
            _mem = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mem)
            return _mem.get_memory_summary()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/tunnel")
async def tunnel_status(request: Request):
    """获取Cloudflare Tunnel公网访问地址"""
    cached = _file_not_modified_response(request, TUNNEL_FILE, "tunnel")
    if cached is not None:
        return cached
    tunnel_token = _file_etag_token(TUNNEL_FILE, "tunnel")
    _reload_cors_origins()
    try:
        with open(TUNNEL_FILE, "r", encoding="utf-8") as f:
            url = f.read().strip()
        tunnel_token_after_read = _file_etag_token(TUNNEL_FILE, "tunnel")
        if url:
            return _json_response(
                request,
                {"active": True, "url": url},
                etag_token=tunnel_token if tunnel_token == tunnel_token_after_read else None,
            )
    except Exception:
        pass
    tunnel_token_after_read = _file_etag_token(TUNNEL_FILE, "tunnel")
    return _json_response(
        request,
        {"active": False, "url": None, "message": "隧道未运行, 运行 bash start_tunnel.sh 启动"},
        etag_token=tunnel_token if tunnel_token == tunnel_token_after_read else None,
    )

@app.get("/health")
def health():
    """增强健康检查: 服务状态 + 大脑就绪 + WebSocket连接 + 运行时间"""
    uptime_s = int(time.time() - _start_time) if _start_time else 0
    return {
        "ok": True,
        "service": "magic-phone-voice",
        "version": "3.1.0",
        "uptime_seconds": uptime_s,
        "uptime_human": f"{uptime_s//3600}h{(uptime_s%3600)//60}m",
        "brain_ready": _brain_is_warm(),
        "websocket_clients": _ws_client_count(),
        "sse_clients": sse_client_count(),
        "auth_enabled": bool(AUTH_TOKEN),
    }

def _build_app_icon_png() -> bytes:
    """Build a small inline PNG icon without adding a binary asset to the repo."""
    import struct
    import zlib

    size = 64
    bg = (15, 12, 41)
    fg = (233, 69, 96)
    cx = cy = size // 2
    radius = size // 3

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            color = fg if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 else bg
            raw.extend(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return b"".join((
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
        chunk(b"IEND", b""),
    ))


_APP_ICON_PNG = _build_app_icon_png()
_ICON_HEADERS = {"Cache-Control": "public, max-age=86400"}
_APP_ICON_ETAG = 'W/"' + hashlib.sha256(_APP_ICON_PNG).hexdigest()[:16] + '"'


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def web_client(request: Request):
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "voice.html")
    return _html_response(request, html_path, "voice-html")


@app.api_route("/manage", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def manage_page(request: Request):
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "manage.html")
    return _html_response(request, html_path, "manage-html")


@app.api_route(
    "/favicon.ico",
    methods=["GET", "HEAD"],
)
@app.api_route(
    "/apple-touch-icon.png",
    methods=["GET", "HEAD"],
)
@app.api_route(
    "/apple-touch-icon-precomposed.png",
    methods=["GET", "HEAD"],
)
async def app_icon(request: Request):
    if _if_none_matches(request, _APP_ICON_ETAG):
        headers = dict(_ICON_HEADERS)
        headers["ETag"] = _APP_ICON_ETAG
        headers["Vary"] = "Accept-Encoding"
        return Response(status_code=304, headers=headers)
    content = b"" if request.method == "HEAD" else _APP_ICON_PNG
    headers = dict(_ICON_HEADERS)
    headers["ETag"] = _APP_ICON_ETAG
    headers["Vary"] = "Accept-Encoding"
    headers["Content-Length"] = str(len(_APP_ICON_PNG))
    return Response(content=content, media_type="image/png", headers=headers)

@app.api_route("/test", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def voice_test(request: Request):
    """语音对话测试台 - 调试用, 显示SSE事件流+延迟指标(首音频优化验证)"""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "voice_test.html")
    return _html_response(request, html_path, "voice-test-html")


# ===== 浏览器配置页面 (不懂代码的用户也能设置 API 密钥) =====

# .env 文件路径: frozen 模式下用 sys.executable 目录 (charlie_main.py 也在那里设 cwd),
# 否则用 PROJECT_DIR
if getattr(sys, 'frozen', False):
    _ENV_FILE = os.path.join(os.path.dirname(sys.executable), ".env")
else:
    _ENV_FILE = os.path.join(PROJECT_DIR, ".env")

# 所有配置项及其默认值 (用于首次创建 .env 时写入模板)
_ENV_DEFAULTS = {
    "ARK_KEY": "", "ARK_BASE": "https://ark.cn-beijing.volces.com/api/plan/v3", "ARK_MODEL": "ark-code-latest",
    "BAIDU_APP_ID": "", "BAIDU_API_KEY": "", "BAIDU_SECRET_KEY": "",
    "AMAP_KEY": "",
    "TAVILY_API_KEY": "", "ALIYUN_API_KEY": "",
    
}

def _parse_env_file(path: str) -> dict:
    """解析 .env 文件为 dict (不依赖 python-dotenv, 保证 frozen 模式可用)"""
    result = {}
    if not os.path.exists(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
    except Exception:
        pass
    return result

def _write_env_file(path: str, data: dict) -> None:
    """将 dict 写入 .env 文件, 保留注释和未在表单中的键"""
    existing = _parse_env_file(path)
    existing.update({k: v for k, v in data.items() if v})  # 只更新非空值
    # 确保目录存在
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Charlie 语音助手 — 环境变量配置 (通过浏览器配置页面生成)\n\n")
        for k, v in existing.items():
            f.write(f"{k}={v}\n")

@app.api_route("/setup", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def setup_page(request: Request):
    """浏览器配置页面 — 用户通过网页填写 API 密钥"""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "setup.html")
    return _html_response(request, html_path, "setup-html")

@app.get("/api/setup")
async def get_setup():
    """读取当前 .env 配置 (隐藏值为空的可选项)"""
    log.info(f"[setup] _ENV_FILE={_ENV_FILE}, exists={os.path.exists(_ENV_FILE)}, cwd={os.getcwd()}")
    data = _parse_env_file(_ENV_FILE)
    # 补全默认值
    for k, v in _ENV_DEFAULTS.items():
        if k not in data:
            data[k] = v
    return data

@app.post("/api/setup")
async def post_setup(request: Request):
    """保存配置到 .env 文件"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求数据格式错误"}, status_code=400)
    # 只允许写入白名单中的键
    safe_data = {k: str(v).strip() for k, v in data.items() if k in _ENV_DEFAULTS}
    # 检查必需项
    required = ["ARK_KEY", "BAIDU_APP_ID", "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "AMAP_KEY"]
    missing = [k for k in required if not safe_data.get(k)]
    if missing:
        return JSONResponse({"ok": False, "error": f"缺少必需配置: {', '.join(missing)}"}, status_code=400)
    try:
        _write_env_file(_ENV_FILE, safe_data)
        log.info(f"[setup] 配置已保存到 {_ENV_FILE}")
        return {"ok": True, "message": "配置已保存，需要重启 Charlie 生效"}
    except Exception as e:
        log.error(f"[setup] 保存失败: {e}")
        return JSONResponse({"ok": False, "error": f"保存失败: {e}"}, status_code=500)


@app.get("/api/protocols")
async def protocols_status():
    """场景协议列表"""
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_scenes", os.path.join(PROJECT_DIR, "magic-scenes.py"))
        if _spec and _spec.loader:
            _sc = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_sc)
            protocols = _sc._load_protocols()
            result = []
            for key, proto in protocols.items():
                result.append({
                    "key": key,
                    "name": proto.get("name", key),
                    "triggers": ", ".join(proto.get("triggers", [])),
                    "step_count": len(proto.get("steps", [])),
                    "is_builtin": key in _sc._BUILTIN_PROTOCOLS,
                })
            return {"protocols": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/protocols/learn")
async def protocols_learn(body: dict):
    """学习新场景协议"""
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_scenes", os.path.join(PROJECT_DIR, "magic-scenes.py"))
        if _spec and _spec.loader:
            _sc = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_sc)
            result = _sc.learn_protocol(
                body.get("name", ""),
                body.get("trigger_words", ""),
                body.get("steps_description", "")
            )
            return {"ok": True, "message": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolution")
async def evolution_status():
    """进化系统状态"""
    try:
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("magic_evolution", os.path.join(PROJECT_DIR, "magic-evolution.py"))
        if _spec and _spec.loader:
            _evo = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_evo)
            data = _evo._load_evolution_data()
            patterns = data.get("usage_patterns", {})
            adaptation = data.get("adaptation_state", {})
            learned = data.get("learned_preferences", {})
            return {
                "total_conversations": patterns.get("total_conversations", 0),
                "response_style": adaptation.get("response_style", "default"),
                "topic_count": len(patterns.get("top_topics", [])),
                "preferences": learned,
                "active_hours": patterns.get("active_hours", []),
                "top_topics": patterns.get("top_topics", []),
            }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolution/learn")
async def evolution_learn():
    """触发进化学习"""
    try:
        from voice_agent import _get_brain
        brain = _get_brain("magic-evolution")
        for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
            pass
        return {"ok": True, "message": "学习完成"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/wake/toggle")
async def wake_toggle_api(enabled: bool = None):
    import local_wake
    current = local_wake.toggle_wake(enabled)
    return {"enabled": current}


@app.get("/api/wake/status")
async def wake_status_api():
    import local_wake
    return local_wake.wake_status()


@app.post("/api/user/switch")
async def switch_user_api(user_id: str = "default"):
    """切换当前用户"""
    from voice_agent import set_current_user, get_current_user
    set_current_user(user_id)
    return {"user_id": get_current_user(), "message": f"已切换到用户: {user_id}"}


@app.get("/api/user/current")
async def current_user_api():
    """获取当前用户"""
    from voice_agent import get_current_user
    return {"user_id": get_current_user()}


# ===== 日志查看器 =====

@app.get("/api/logs")
async def get_logs(lines: int = 100, filter: str = ""):
    """读取应用日志，支持行数和关键词过滤"""
    log_path = os.path.join(_LOG_DIR, "app.log")
    if not os.path.exists(log_path):
        return {"lines": [], "total": 0}
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        if filter:
            all_lines = [l for l in all_lines if filter in l]
        tail = all_lines[-lines:]
        return {"lines": tail, "total": len(tail), "file": log_path}
    except Exception as e:
        return {"error": str(e), "lines": [], "total": 0}


# ===== TTS 音色切换 =====

AVAILABLE_TTS_VOICES = {
    "Cherry": "Cherry - 自然女声",
    "Stella": "Stella - 温柔女声",
    "Alex": "Alex - 沉稳男声",
    "Vega": "Vega - 活力女声",
    "Nova": "Nova - 甜美女声",
    "Echo": "Echo - 中性声",
}

@app.get("/api/tts/voices")
async def list_tts_voices():
    """列出可用 TTS 音色"""
    current = voice_agent.TTS_VOICE
    voices = []
    for key, desc in AVAILABLE_TTS_VOICES.items():
        voices.append({"id": key, "name": desc, "current": key == current})
    return {"voices": voices, "current": current}


@app.post("/api/tts/voice")
async def set_tts_voice(voice_id: str = "Cherry"):
    """切换 TTS 音色"""
    if voice_id not in AVAILABLE_TTS_VOICES:
        return JSONResponse({"error": f"未知音色: {voice_id}"}, status_code=400)
    voice_agent.TTS_VOICE = voice_id
    voice_agent._tts_cache.clear()
    log.info(f"[tts] 音色已切换为: {voice_id}")
    return {"ok": True, "voice": voice_id, "name": AVAILABLE_TTS_VOICES[voice_id]}


# ===== MCP 工具开关 =====

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    """列出所有可用 MCP 服务器及其状态"""
    from voice_agent import _build_brain, _brains
    all_mcp = _build_brain.__defaults__[0] if hasattr(_build_brain, '__defaults__') else {}
    # 从环境变量读取启用的 MCP 列表
    enabled_env = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life,magic-scenes,magic-evolution,magic-summary,magic-wardrobe,magic-browser,magic-apps,magic-feishu,magic-douyin,magic-taobao").split(",")
    enabled_env = [s.strip() for s in enabled_env if s.strip()]

    mcp_servers = {
        "amap-maps": "高德地图/天气",
        "magic-info": "信息查询(时间/天气/新闻/翻译)",
        "magic-music": "音乐播放",
        "magic-reminder": "提醒/定时器",
        "magic-notes": "备忘录",
        "magic-system": "系统控制(音量/语速)",
        "magic-life": "生活服务(外卖/充电桩)",
        "magic-scenes": "场景自动化",
        "magic-apps": "App控制",
        "magic-feishu": "飞书集成",
        "magic-douyin": "抖音",
        "magic-taobao": "淘宝/京东",
        "magic-evolution": "自进化",
        "magic-summary": "每日摘要",
        "magic-wardrobe": "穿搭推荐",
        "magic-browser": "浏览器控制",
        "baize-skills": "互联网搜索",
        "filesystem": "文件系统",
        "ac-control": "空调控制",
        "mimo-vision": "视觉识别",
    }
    result = []
    for key, name in mcp_servers.items():
        enabled = key in enabled_env
        cached = key in _brains
        result.append({
            "id": key,
            "name": name,
            "enabled": enabled,
            "cached": cached,
        })
    return {"servers": result, "enabled_list": ",".join(enabled_env)}


@app.post("/api/mcp/toggle")
async def toggle_mcp_server(server_id: str = "", enabled: bool = True):
    """启用或禁用某个 MCP 服务器"""
    if not server_id:
        return JSONResponse({"error": "缺少 server_id 参数"}, status_code=400)
    current = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life,magic-scenes,magic-evolution,magic-summary,magic-wardrobe,magic-browser,magic-apps,magic-feishu,magic-douyin,magic-taobao")
    enabled_list = [s.strip() for s in current.split(",") if s.strip()]
    if enabled and server_id not in enabled_list:
        enabled_list.append(server_id)
        log.info(f"[mcp] 启用 MCP: {server_id}")
    elif not enabled and server_id in enabled_list:
        enabled_list.remove(server_id)
        log.info(f"[mcp] 禁用 MCP: {server_id}")
    # 更新环境变量 (运行时生效, 不持久化)
    new_value = ",".join(enabled_list)
    os.environ["MCP_SERVERS"] = new_value
    # 清除缓存大脑, 下次请求重建
    from voice_agent import restart_brain
    restart_brain()
    return {"ok": True, "server_id": server_id, "enabled": enabled, "enabled_list": enabled_list}


# Register xiaozhi-compatible WebSocket endpoint
register_xiaozhi_routes(app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=http_port())
