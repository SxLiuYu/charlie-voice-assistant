"""
助手小子 - 实时语音服务
POST /api/voice    : 音频进 → ASR → 大脑(GLM-5.2+MCP) → TTS → 音频出
POST /api/chat     : 纯文字进 → 大脑 → 文字出
GET  /api/reminders: 待办列表
POST /api/reminders: 添加提醒
DEL  /api/reminders/{id}: 删除/完成提醒
GET  /              : Web自动监听客户端(免点击对话)
GET  /health        : 健康检查

后台调度器: 每30s检查reminders.json，到期提醒自动TTS+afplay播报
"""
import os, sys, subprocess, tempfile, json, threading, time, datetime, logging, asyncio
import queue as _queue
import base64 as _b64enc
from contextlib import asynccontextmanager
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, WebSocket, WebSocketDisconnect
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

# ===== 文件日志(持久化, 含uvicorn错误堆栈, 防止traceback丢失) =====
import logging.handlers as _loghandlers
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
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
from app.audio import to_wav, _wav_to_mp3, MAX_AUDIO_SIZE
from app.auth import _client_ip, _is_local_request, _check_auth, _sanitize_text, AUTH_TOKEN
from app.brain_health import _brain_is_warm, _get_brain_health, _warmup_brain
from app.state import (_metrics, _ws_clients, _sse_clients, _rate_buckets, _session_buckets,
    _RATE_GENERAL, _RATE_VOICE, _RATE_WINDOW, _RATE_PER_SESSION, MAX_REQUEST_BODY, _ws_client_count)
from app.reminders import REMINDERS_FILE, _load_reminders, _save_reminders, _cleanup_old_reminders
from app.routes.system import system_router

# ===== 请求指标追踪 =====

_start_time = time.time()  # 服务启动时间(用于健康检查uptime)


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
        cleanup_temp_files()
        truncate_history_file(REMINDERS_FILE.replace("reminders.json", "conversation_history.json"), 100)
        _start_scheduler()
        _start_proactive()
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
        ("GLM_KEY", "GLM-5.2大脑"),
        ("TTS_KEY", "TTS语音合成"),
        ("ASR_KEY", "ASR语音识别"),
        ("AMAP_KEY", "高德地图"),
    ]
    optional = [
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
        log.error(f"缺少{len(missing)}个必需密钥！请检查.env文件")
        log.error("复制 .env.example 为 .env 并填入密钥")
    return len(missing) == 0

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

class BrainRestartRequest(BaseModel):
    """大脑重启请求(预留)"""
    force: bool = Field(default=False, description="强制重启")

app = FastAPI(title="助手小子语音服务", lifespan=lifespan)
app.include_router(system_router)

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
from fastapi.middleware.cors import CORSMiddleware
# CORS: 动态允许来源(localhost + tunnel + 局域网)
_cors_origins = [
    "*",  # 开发阶段仍允许所有(生产环境可移除)
    "http://localhost:8000",
    "http://localhost:8443",
    "https://localhost:8443",
]
# 动态加载tunnel URL
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel_url.txt")) as f:
        _tunnel = f.read().strip()
        if _tunnel:
            _cors_origins.append(_tunnel)
except Exception:
    pass

app.add_middleware(CORSMiddleware,
    allow_origins=_cors_origins,
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
    log.info(f"[{rid}] {request.method} {request.url.path}")
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
        _metrics.record(request.url.path, dur, ok=False)
        log.error(f"[{rid}] 异常: {e} ({dur:.0f}ms)")
        raise
    dur = (_t.time() - start) * 1000
    ok = response.status_code < 500
    _metrics.record(request.url.path, dur, ok=ok)
    log.info(f"[{rid}] {request.method} {request.url.path} → {response.status_code} {dur:.0f}ms")
    response.headers["X-Request-ID"] = rid
    return response

MAX_TEXT_LENGTH = 500  # 文字输入上限

# ===== 音频转wav =====


# ===== 提醒管理 =====



# ===== 通知队列(Web客户端可轮询获取主动通知) =====
_notifications = []
MAX_NOTIFICATIONS = 20

def _add_notification(text: str, ntype: str = "reminder"):
    """添加通知到队列+SSE推送"""
    _notifications.append({
        "text": text, "type": ntype,
        "time": datetime.datetime.now().isoformat()
    })
    if len(_notifications) > MAX_NOTIFICATIONS:
        _notifications.pop(0)
    _push_notification_to_sse(text, ntype)  # SSE实时推送

# ===== SSE实时通知推送 =====
_main_loop = None  # 主线程event loop(启动时捕获)

def _push_notification_to_sse(text: str, ntype: str = "reminder"):
    """推送通知到所有已连接的SSE客户端(线程安全)"""
    global _main_loop
    if _main_loop is None:
        return  # 没有SSE客户端或loop未初始化
    for client_q in list(_sse_clients):
        try:
            _main_loop.call_soon_threadsafe(
                client_q.put_nowait, {"text": text, "type": ntype,
                    "time": datetime.datetime.now().isoformat()})
        except Exception:
            try: _sse_clients.remove(client_q)
            except: pass

def _play_reminder_audio(text: str):
    """生成提醒语音并播放到默认音频输出(AirPods/扬声器)"""
    try:
        from voice_agent import tts
        _add_notification(text, "reminder")
        log.info(f"[reminder] TTS生成: {text}")
        audio = tts(f"主人，提醒您：{text}")
        if not audio or len(audio) < 100:
            log.warning("[reminder] TTS返回空音频，跳过播放")
            return
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir="/tmp")
        tmp.write(audio)
        tmp.close()
        log.info(f"[reminder] 播放提醒语音 {len(audio)}字节: {text}")
        subprocess.run(["afplay", tmp.name], timeout=30, capture_output=True)
        os.unlink(tmp.name)
        log.info("[reminder] 播放完成")
    except Exception as e:
        log.error(f"[reminder] 播放失败: {e}")

def _reminder_scheduler():
    """后台守护线程：每30s检查到期提醒，自动播报"""
    log.info("[reminder] 提醒调度器已启动，每30秒检查一次")
    cleanup_counter = 0
    while True:
        try:
            cleanup_counter += 1
            if cleanup_counter >= 20:
                cleanup_counter = 0
                from utils import cleanup_temp_files, truncate_history_file
                cleanup_temp_files()
                truncate_history_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_history.json"), 100)
            reminders = _load_reminders()
            now = datetime.datetime.now()
            changed = False
            for r in reminders:
                if r.get("done"):
                    continue
                due_str = r.get("due", "")
                if not due_str:
                    continue
                try:
                    due = datetime.datetime.fromisoformat(due_str)
                except Exception:
                    continue
                if now >= due:
                    text = r.get("text", "提醒")
                    rid = r.get("id", 0)
                    log.info(f"[reminder] 提醒到期(id={rid}): {text} (due={due_str})")
                    # 异步播放（不阻塞调度器循环）
                    threading.Thread(target=_play_reminder_audio, args=(text,), daemon=True).start()
                    r["done"] = True
                    r["triggered_at"] = now.isoformat()
                    changed = True
            if changed:
                _save_reminders(reminders)
                log.info("[reminder] 已更新reminders.json")
        except Exception as e:
            log.error(f"[reminder] 调度器异常: {e}")
        time.sleep(30)

def _start_scheduler():
    t = threading.Thread(target=_reminder_scheduler, daemon=True)
    t.start()

# ===== 主动建议(天气/时间感知) =====
AMAP_KEY = os.getenv("AMAP_KEY", "")
SUGGEST_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suggestions_state.json")
SUGGESTIONS_STATE = {"last_weather_check": 0, "last_rain_suggest": "", "last_time_suggest": "", "last_health_alert": ""}

def _load_suggest_state():
    global SUGGESTIONS_STATE
    try:
        with open(SUGGEST_STATE_FILE, "r") as f:
            SUGGESTIONS_STATE.update(json.load(f))
    except Exception:
        pass

def _save_suggest_state():
    try:
        with open(SUGGEST_STATE_FILE, "w") as f:
            json.dump(SUGGESTIONS_STATE, f)
    except Exception:
        pass

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

def _proactive_suggestions():
    """后台守护线程：定时检查天气和时间，主动推送建议"""
    log.info("[suggest] 主动建议系统已启动，每小时检查天气，定时推送建议")
    while True:
        try:
            now = datetime.datetime.now()
            hour = now.hour
            today = now.strftime("%Y-%m-%d")

            # 1. 天气建议(每小时检查一次)
            if time.time() - SUGGESTIONS_STATE["last_weather_check"] > 3600:
                SUGGESTIONS_STATE["last_weather_check"] = time.time()
                casts = _get_weather()
                for c in casts:
                    weather = c.get("dayweather", "") + " " + c.get("nightweather", "")
                    if ("雨" in weather or "雪" in weather) and SUGGESTIONS_STATE["last_rain_suggest"] != today:
                        SUGGESTIONS_STATE["last_rain_suggest"] = today
                        _save_suggest_state()
                        msg = f"主人，今天天气预报有{weather}，出门记得带伞哦。"
                        _add_notification(msg, "weather")
                        log.info(f"[suggest] 主动天气建议: {msg}")
                        threading.Thread(target=_play_reminder_audio, args=(msg,), daemon=True).start()
                        break

            # 2. 时间建议(每天只推一次)
            if hour >= 23 and hour < 24 and SUGGESTIONS_STATE["last_time_suggest"] != today + "_late":
                SUGGESTIONS_STATE["last_time_suggest"] = today + "_late"
                _save_suggest_state()
                msg = "主人，已经23点了，该休息了，明天的事明天再说。"
                _add_notification(msg, "bedtime")
                log.info(f"[suggest] 主动休息建议: {msg}")
                threading.Thread(target=_play_reminder_audio, args=(msg,), daemon=True).start()
            elif 8 <= hour < 10 and SUGGESTIONS_STATE["last_time_suggest"] != today + "_morning":
                SUGGESTIONS_STATE["last_time_suggest"] = today + "_morning"
                _save_suggest_state()
                casts = _get_weather()
                w = casts[0].get("dayweather", "") if casts else ""
                temp = casts[0].get("daytemp", "") if casts else ""
                # 晨报：天气 + 今日待办
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
                threading.Thread(target=_play_reminder_audio, args=(msg,), daemon=True).start()

            # 3. 系统健康监控(CPU>90%或内存>95%时告警，每小时最多一次)
            import psutil
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory().percent
            health_key = today + f"_health_{hour}"
            if (cpu > 90 or mem > 95) and SUGGESTIONS_STATE.get("last_health_alert", "") != health_key:
                SUGGESTIONS_STATE["last_health_alert"] = health_key
                _save_suggest_state()
                msg = f"主人，系统资源紧张：CPU使用率{cpu:.0f}%，内存{mem:.0f}%，建议关闭一些不必要的程序。"
                _add_notification(msg, "health")
                log.warning(f"[suggest] 系统健康告警: {msg}")
                threading.Thread(target=_play_reminder_audio, args=(msg,), daemon=True).start()

            # 4. 基于用户偏好的主动建议(每天最多一次/偏好)
            try:
                from voice_agent import list_preferences
                prefs = list_preferences()
                for pkey, pval in prefs.items():
                    pref_suggest_key = today + f"_pref_{pkey[:10]}"
                    if SUGGESTIONS_STATE.get(f"last_pref_{pkey[:10]}", "") == pref_suggest_key:
                        continue
                    # 根据偏好类型和时间生成建议
                    suggestion = None
                    if "下班" in pkey or "下班" in pval:
                        if 17 <= hour < 19:  # 下午5-7点
                            suggestion = f"主人，快到下班时间了({pval})，需要我帮你查查路况或叫个车吗？"
                    elif "食物" in pkey or "喜欢吃" in pkey:
                        if 11 <= hour < 13 or 17 <= hour < 19:  # 饭点
                            suggestion = f"主人，到饭点了，我记得你喜欢{pval}，要不要我帮你找附近的餐厅？"
                    elif "运动" in pkey or "锻炼" in pkey:
                        if 6 <= hour < 8 or 18 <= hour < 20:  # 运动时间
                            suggestion = f"主人，是你平时的运动时间，今天别忘了{pval}哦。"
                    elif "睡" in pkey or "休息" in pkey:
                        if 22 <= hour < 24:  # 晚上10-12点
                            suggestion = f"主人，你设定了{pkey}为{pval}，该准备休息了。"
                    if suggestion:
                        SUGGESTIONS_STATE[f"last_pref_{pkey[:10]}"] = pref_suggest_key
                        _save_suggest_state()
                        _add_notification(suggestion, "preference")
                        log.info(f"[suggest] 偏好建议({pkey}): {suggestion}")
                        threading.Thread(target=_play_reminder_audio, args=(suggestion,), daemon=True).start()
                        break  # 每次只推一条
            except Exception as e:
                log.debug(f"[suggest] 偏好建议检查异常: {e}")

        except Exception as e:
            log.error(f"[suggest] 主动建议异常: {e}")
        time.sleep(60)  # 每分钟检查一次时间条件

def _start_proactive():
    t = threading.Thread(target=_proactive_suggestions, daemon=True)
    t.start()



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
    from voice_agent import voice_loop
    try:
        text, reply, audio_out = await asyncio.wait_for(
            asyncio.to_thread(voice_loop, wav, "wav"), timeout=60)
        mp3_out = _wav_to_mp3(audio_out)
        log.info(f"/api/voice 完成: 识别={text[:30]} 回复={reply[:30]} WAV={len(audio_out)}→MP3={len(mp3_out)}字节")
        import base64 as _b64
        return {"text": text, "reply": reply, "audio": _b64.b64encode(mp3_out).decode(), "format": "mp3"}
    except asyncio.TimeoutError:
        log.error("/api/voice 超时(60s)")
        return JSONResponse({"error": "处理超时，请重试"}, status_code=504)
    except Exception as e:
        log.error(f"/api/voice 异常: {e}")
        from utils import sanitize_error
        return JSONResponse({"error": sanitize_error(str(e))}, status_code=500)

# ===== 流式端点: 大脑逐句产出 → TTS批量推送(SSE) =====
_TTS_BATCH_SIZE = 50  # TTS批量大小(字符数)，平衡延迟与效率

def _flush_tts_buffer(tts_buffer: str) -> str:
    """清理并生成TTS音频，返回base64 MP3(空则返回'')"""
    from voice_agent import tts_to_mp3, _clean_for_tts
    cleaned = _clean_for_tts(tts_buffer)
    if not cleaned or len(cleaned) < 2:
        return ""
    mp3 = tts_to_mp3(cleaned)
    if not mp3 or len(mp3) < 100:
        return ""
    return _b64enc.b64encode(mp3).decode()

async def _stream_brain_tts(text: str, asr_text: str = "", session_id: str = "default"):
    """
    流式大脑+TTS生成器(SSE事件流)。
    大脑在后台线程逐句产出 → 文本事件即时推送(显示) → TTS批量推送(音频)。
    yield: SSE格式的data行。
    """
    from voice_agent import brain_stream_sentences
    
    q = _queue.Queue()
    
    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(text, session_id):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", str(e)[:60], None))
        finally:
            q.put(("done", None, None))
    
    threading.Thread(target=brain_worker, daemon=True).start()
    
    # 如果有ASR结果，先推送
    if asr_text:
        yield f'data: {json.dumps({"type":"asr","text":asr_text}, ensure_ascii=False)}\n\n'
    
    tts_buffer = ""
    first_audio_sent = False  # 首段立即flush(降首音频延迟)
    total_wait = 0
    HEARTBEAT_INTERVAL = 5  # 每5秒发心跳
    MAX_WAIT = 120  # 总超时120秒
    
    while True:
        try:
            item = q.get_nowait()  # 非阻塞检查队列
            total_wait = 0  # 收到数据,重置计时
        except _queue.Empty:
            # 队列空, 发心跳保活
            if total_wait >= MAX_WAIT:
                yield f'data: {json.dumps({"type":"error","message":"思考超时"}, ensure_ascii=False)}\n\n'
                yield 'data: {"type":"done"}\n\n'
                break
            yield ': heartbeat\n\n'  # SSE心跳(注释,客户端忽略,保活连接)
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            total_wait += HEARTBEAT_INTERVAL
            continue
        
        etype, sentence, full_reply = item
        if etype == "done":
            # 推送剩余TTS
            if tts_buffer.strip():
                audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
                if audio_b64:
                    yield f'data: {json.dumps({"type":"audio","audio":audio_b64}, ensure_ascii=False)}\n\n'
            yield 'data: {"type":"done"}\n\n'
            break
        elif etype == "error":
            yield f'data: {json.dumps({"type":"error","message":sentence}, ensure_ascii=False)}\n\n'
            yield 'data: {"type":"done"}\n\n'
            break
        elif etype == "sentence":
            # 文本事件即时推送(用于显示)
            yield f'data: {json.dumps({"type":"text","text":sentence}, ensure_ascii=False)}\n\n'
            # 积累TTS缓冲区
            from voice_agent import _clean_for_tts
            cleaned = _clean_for_tts(sentence)
            if cleaned and len(cleaned) >= 2:
                tts_buffer = (tts_buffer + "，" + cleaned) if tts_buffer else cleaned
                # 首段立即flush(不等50字,降低首音频延迟); 后续按50字批
                if (not first_audio_sent) or (len(tts_buffer) >= _TTS_BATCH_SIZE):
                    audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
                    tts_buffer = ""
                    first_audio_sent = True
                    if audio_b64:
                        yield f'data: {json.dumps({"type":"audio","audio":audio_b64}, ensure_ascii=False)}\n\n'

@app.post("/api/chat/stream")
async def chat_stream_api(req: ChatRequest):
    """流式文字对话: 文字进 → 大脑逐句产出 → TTS批量推送(SSE)"""
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    session_id = req.session_id
    log.info(f"/api/chat/stream 流式对话: {text[:40]} (session={session_id[:8]})")
    return StreamingResponse(_stream_brain_tts(text, session_id=session_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/voice/stream")
async def voice_stream_api(file: UploadFile = File(...), session_id: str = "default"):
    """流式语音对话: 音频进 → ASR → 大脑逐句 → TTS批量(SSE)"""
    data = await file.read()
    ext = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    if len(data) > MAX_AUDIO_SIZE:
        return JSONResponse({"error": f"音频过大", "status_code": 413}, status_code=413)
    log.info(f"/api/voice/stream 收到音频: {len(data)}字节, 格式={ext}")
    
    wav = to_wav(data, ext)
    from voice_agent import asr
    try:
        asr_text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "语音识别超时"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": f"识别失败: {e}"}, status_code=500)
    
    if not asr_text:
        asr_text = "(未识别到语音)"
    
    return StreamingResponse(_stream_brain_tts(asr_text, asr_text, session_id=session_id), 
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/chat")
async def chat_api(req: ChatRequest):
    text = _sanitize_text(req.message, MAX_TEXT_LENGTH)
    from voice_agent import brain
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(brain, text, req.session_id), timeout=30)
        return {"reply": reply}
    except asyncio.TimeoutError:
        log.error("/api/chat 超时(30s)")
        return JSONResponse({"error": "思考超时，请重试"}, status_code=504)
    except Exception as e:
        log.error(f"/api/chat 异常: {e}")
        # 优雅降级: 返回友好提示而非500错误
        fallback = "抱歉，我现在有点忙不过来，请稍等一下再试。"
        try:
            from voice_agent import _rotate_glm_key
            _rotate_glm_key()  # 尝试切换密钥
            log.info("[chat] 密钥已轮换, 下次请求将使用新密钥")
        except Exception:
            pass
        return {"reply": fallback, "degraded": True}

@app.post("/api/reset")
async def reset_conversation(session_id: str = "default"):
    """清空指定会话的对话历史"""
    from voice_agent import reset_history
    reset_history(session_id)
    return {"ok": True, "message": "对话已重置", "session_id": session_id}


@app.get("/api/reminders")
async def list_reminders():
    data = _load_reminders()
    pending = [r for r in data if not r.get("done")]
    return {"total": len(data), "pending": len(pending), "reminders": data}

@app.post("/api/reminders")
async def add_reminder(req: ReminderRequest):
    text = _sanitize_text(req.text, 200)
    time_str = _sanitize_text(req.time, 50)
    # 复用共享时间解析工具
    due = None
    if time_str:
        from utils import parse_time_str
        due = parse_time_str(time_str)
    data = _load_reminders()
    rid = int(datetime.datetime.now().timestamp())
    data.append({"id": rid, "text": text, "time": time_str, "due": due, "done": False})
    _save_reminders(data)
    when = f"，提醒时间{due.replace('T', ' ')}" if due else (f"（时间'{time_str}'未解析出时刻）" if time_str else "")
    return {"ok": True, "id": rid, "message": f"已添加提醒：{text}{when}"}

@app.delete("/api/reminders/{rid}")
async def delete_reminder(rid: int):
    data = _load_reminders()
    found = False
    for r in data:
        if r.get("id") == rid:
            r["done"] = True
            r["completed_at"] = datetime.datetime.now().isoformat()
            found = True
            break
    if not found:
        raise HTTPException(404, "提醒不存在")
    _save_reminders(data)
    return {"ok": True, "message": f"提醒{rid}已标记完成"}

@app.get("/api/conversation")
async def get_conversation(page: int = 1, limit: int = 50, session_id: str = "default"):
    """获取对话历史(支持分页)
    page: 页码(从1开始), limit: 每页条数(默认50)
    """
    from voice_agent import _get_history
    hist = _get_history(session_id)
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
    from voice_agent import tts
    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(tts, text), timeout=30)
        if not audio:
            return JSONResponse({"error": "TTS生成失败"}, status_code=500)
        mp3 = _wav_to_mp3(audio)
        return Response(content=mp3, media_type="audio/mpeg")
    except asyncio.TimeoutError:
        return JSONResponse({"error": "TTS超时"}, status_code=504)

@app.post("/api/asr")
async def asr_api(file: UploadFile = File(...)):
    """音频 → 文字"""
    data = await file.read()
    ext = (file.filename or "audio.wav").rsplit(".", 1)[-1].lower()
    wav = to_wav(data, ext)
    from voice_agent import asr
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(asr, wav, "wav"), timeout=30)
        return {"text": text}
    except asyncio.TimeoutError:
        return JSONResponse({"error": "ASR超时"}, status_code=504)

@app.get("/api/export")
async def export_conversation(format: str = "txt"):
    """导出对话历史(支持txt/markdown/json格式)"""
    from voice_agent import _history
    if not _history:
        return Response(content="(对话历史为空)".encode("utf-8"), media_type="text/plain")
    
    if format == "json":
        import json as _j
        return Response(content=_j.dumps(hist, ensure_ascii=False, indent=2).encode("utf-8"),
                       media_type="application/json",
                       headers={"Content-Disposition": "attachment; filename=conversation.json"})
    
    if format in ("markdown", "md"):
        lines = ["# 助手小子 · 对话记录\n"]
        for m in _history:
            role = "🙋 我" if m.get("role") == "user" else "🤖 助手小子"
            lines.append(f"### {role}\n\n{m.get('content', '')}\n")
        lines.append(f"\n---\n*共{len(hist)}条消息 · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}导出*")
        text = "\n".join(lines)
        return Response(content=text.encode("utf-8"), media_type="text/markdown",
                       headers={"Content-Disposition": "attachment; filename=conversation.md"})
    
    # 默认txt格式(带时间戳)
    lines = []
    for m in hist:
        role = "我" if m.get("role") == "user" else "助手小子"
        ts = m.get("ts", "")[:19].replace("T", " ") if m.get("ts") else ""
        ts_prefix = f"[{ts}] " if ts else ""
        lines.append(f"[{role}] {ts_prefix}{m.get('content', '')}")
    text = "\n\n".join(lines)
    return Response(content=text.encode("utf-8"), media_type="text/plain",
                   headers={"Content-Disposition": "attachment; filename=conversation.txt"})

@app.get("/api/notifications")
async def get_notifications():
    """获取并清空通知队列(Web客户端轮询用)"""
    notifs = list(_notifications)
    _notifications.clear()
    return {"count": len(notifs), "notifications": notifs}

@app.get("/api/events")
async def sse_events():
    """SSE实时通知流(Web客户端用EventSource连接, 免轮询)"""
    import asyncio
    from starlette.responses import StreamingResponse

    queue = asyncio.Queue()
    _sse_clients.append(queue)

    async def event_stream():
        try:
            # 发送连接确认
            yield f"data: {json.dumps({'type':'connect','text':'已连接','time':datetime.datetime.now().isoformat()})}\n\n"
            while True:
                try:
                    notif = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(notif, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield f"data: {json.dumps({'type':'heartbeat','text':'','time':''})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _sse_clients:
                _sse_clients.remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})




# ===== API令牌认证(保护公网访问) =====



# ===== 输入清洗(防XSS/注入) =====

# ===== WebSocket 双向通信(实时语音/文字, 支持打断TTS) =====
_WS_STALE_TIMEOUT = 300  # 5分钟无活动视为过期

def _ws_cleanup_stale():
    """清理过期的WebSocket连接(5分钟无活动)"""
    now = time.time()
    stale = [sid for sid, info in _ws_clients.items()
             if now - info.get("last_active", now) > _WS_STALE_TIMEOUT]
    for sid in stale:
        try:
            ws = _ws_clients[sid].get("ws")
            if ws:
                import asyncio
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(asyncio.ensure_future, ws.close())
        except Exception:
            pass
        _ws_clients.pop(sid, None)
    if stale:
        log.info(f"[ws] 清理{len(stale)}个过期连接, 剩余{len(_ws_clients)}个")

def _start_ws_cleanup():
    """启动WebSocket过期连接清理线程(每60秒)"""
    def _cleanup_loop():
        while True:
            time.sleep(60)
            _ws_cleanup_stale()
    threading.Thread(target=_cleanup_loop, daemon=True).start()

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
    _ws_clients[ws_id] = {"ws": ws, "interrupt": False, "last_active": time.time()}
    log.info(f"[ws] 客户端已连接 (id={ws_id}), 共{len(_ws_clients)}个连接")
    
    # 发送连接确认
    await ws.send_json({"type": "connect", "text": "助手小子已连接", 
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
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "消息格式错误,需要JSON"})
                continue
            
            mtype = data.get("type", "")
            
            if mtype == "ping":
                await ws.send_json({"type": "pong", "time": datetime.datetime.now().isoformat()})
                continue
            
            if mtype == "interrupt":
                # 设置打断标志, 流式生成器会检查
                _ws_clients[ws_id]["interrupt"] = True
                log.info(f"[ws] 客户端请求打断TTS (id={ws_id})")
                await ws.send_json({"type": "interrupted"})
                continue
            
            if mtype == "text":
                text = data.get("message", "").strip()
                session_id = data.get("session_id", "default")
                if not text:
                    await ws.send_json({"type": "error", "message": "消息不能为空"})
                    continue
                if len(text) > MAX_TEXT_LENGTH:
                    await ws.send_json({"type": "error", "message": f"输入过长(上限{MAX_TEXT_LENGTH}字)"})
                    continue
                # 重置打断标志
                _ws_clients[ws_id]["interrupt"] = False
                log.info(f"[ws] 文字对话: {text[:40]} (session={session_id[:8]})")
                
                # 流式处理大脑回复
                async for event in _ws_stream_brain(ws_id, text, "", session_id):
                    await ws.send_json(event)
                    # 检查打断
                    if _ws_clients[ws_id]["interrupt"]:
                        await ws.send_json({"type": "interrupted"})
                        break
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
                # 重置打断标志
                _ws_clients[ws_id]["interrupt"] = False
                log.info(f"[ws] 语音对话: {len(raw)}字节, 格式={fmt}")
                
                # ASR
                wav = to_wav(raw, fmt)
                from voice_agent import asr
                try:
                    asr_text = await asyncio.wait_for(asyncio.to_thread(asr, wav, "wav"), timeout=30)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "error", "message": "语音识别超时"})
                    continue
                if not asr_text:
                    asr_text = "(未识别到语音)"
                await ws.send_json({"type": "asr", "text": asr_text})
                
                # 流式大脑回复
                ws_session_id = data.get("session_id", "default")
                async for event in _ws_stream_brain(ws_id, asr_text, asr_text, ws_session_id):
                    await ws.send_json(event)
                    if _ws_clients[ws_id]["interrupt"]:
                        await ws.send_json({"type": "interrupted"})
                        break
                continue
            
            # 未知类型
            await ws.send_json({"type": "error", "message": f"未知消息类型: {mtype}"})
    
    except WebSocketDisconnect:
        log.info(f"[ws] 客户端断开 (id={ws_id})")
    except Exception as e:
        log.error(f"[ws] 异常 (id={ws_id}): {e}")
    finally:
        _ws_clients.pop(ws_id, None)
        log.info(f"[ws] 连接清理完成 (id={ws_id}), 剩余{len(_ws_clients)}个")

async def _ws_stream_brain(ws_id: int, text: str, asr_text: str = "", session_id: str = "default"):
    """WebSocket专用流式大脑+TTS生成器(检查打断标志)"""
    from voice_agent import brain_stream_sentences, _clean_for_tts
    
    q = _queue.Queue()
    
    def brain_worker():
        try:
            for sentence, full_reply in brain_stream_sentences(text, session_id):
                q.put(("sentence", sentence, full_reply))
        except Exception as e:
            q.put(("error", str(e)[:60], None))
        finally:
            q.put(("done", None, None))
    
    threading.Thread(target=brain_worker, daemon=True).start()
    
    # 如果有ASR结果, 先推送
    if asr_text:
        yield {"type": "asr", "text": asr_text}
    
    tts_buffer = ""
    first_audio_sent = False  # 首段立即flush(降首音频延迟)
    total_wait = 0
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
            if tts_buffer.strip():
                audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
                if audio_b64:
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
            cleaned = _clean_for_tts(sentence)
            if cleaned and len(cleaned) >= 2:
                tts_buffer = (tts_buffer + "，" + cleaned) if tts_buffer else cleaned
                if (not first_audio_sent) or (len(tts_buffer) >= _TTS_BATCH_SIZE):
                    audio_b64 = await asyncio.to_thread(_flush_tts_buffer, tts_buffer)
                    tts_buffer = ""
                    first_audio_sent = True
                    if audio_b64:
                        yield {"type": "audio", "data": audio_b64}
                        # 检查打断
                        if _ws_clients.get(ws_id, {}).get("interrupt"):
                            break


@app.get("/api/search")
async def search_conversation(q: str = "", session_id: str = "default", limit: int = 20, offset: int = 0):
    """搜索对话历史中的关键词(同时搜索内存+文件)"""
    if not q:
        return JSONResponse({"error": "请提供搜索关键词?q=xxx"}, status_code=400)
    from voice_agent import _get_history, HISTORY_FILE
    # 合并内存历史和文件历史(去重)
    hist = _get_history(session_id)
    all_history = list(hist)
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            file_data = json.load(f)
        # 新格式: dict {session_id: [messages]}
        if isinstance(file_data, dict):
            file_hist = file_data.get(session_id, [])
        # 旧格式: list [messages]
        elif isinstance(file_data, list):
            file_hist = file_data
        else:
            file_hist = []
        # 使用文件中更完整的历史
        if len(file_hist) > len(all_history):
            all_history = file_hist
    except Exception:
        pass
    results = []
    for i, m in enumerate(all_history):
        content = m.get("content", "")
        content_lower = content.lower()
        q_lower = q.lower()
        if q_lower not in content_lower:
            continue
        role = "我" if m.get("role") == "user" else "助手小子"
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





@app.get("/manifest.json")
async def manifest():
    """PWA manifest for mobile install"""
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "助手小子",
        "short_name": "助手小子",
        "description": "中国版贾维斯 - AI语音助理",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f0c29",
        "theme_color": "#e94560",
    })



@app.post("/api/brain/restart")
async def restart_brain_api():
    """手动重启大脑(清除旧MCP连接, 下次请求重建)"""
    from voice_agent import restart_brain
    msg = restart_brain()
    log.info(f"[brain] 手动重启: {msg}")
    return {"ok": True, "message": msg}


@app.get("/api/metrics")
async def metrics():
    """请求指标: 请求数/错误率/缓存命中/响应时间(p50/p95)"""
    return _metrics.summary()


# ===== 用户偏好管理API =====
class PreferenceRequest(BaseModel):
    """设置偏好请求"""
    key: str = Field(..., min_length=1, max_length=50, description="偏好键名(如'喜欢的食物')")
    value: str = Field(..., min_length=1, max_length=200, description="偏好值(如'意大利菜')")

@app.get("/api/preferences")
async def get_preferences():
    """获取所有用户偏好"""
    from voice_agent import list_preferences
    prefs = list_preferences()
    return {"total": len(prefs), "preferences": prefs}

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
    from voice_agent import _sessions
    sessions = []
    for sid, hist in _sessions.items():
        sessions.append({
            "session_id": sid[:16] + "..." if len(sid) > 16 else sid,
            "message_count": len(hist),
            "last_message": hist[-1].get("content", "")[:50] if hist else "",
        })
    return {"total": len(sessions), "sessions": sessions}

@app.get("/api/context")
async def get_context(session_id: str = "default"):
    """获取对话上下文摘要(调试用)"""
    from voice_agent import _context_summaries, _get_history, list_preferences, _estimate_msg_tokens as _est_tokens
    hist = _get_history(session_id)
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

@app.get("/api/tunnel")
async def tunnel_status():
    """获取Cloudflare Tunnel公网访问地址"""
    tunnel_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel_url.txt")
    try:
        with open(tunnel_file, "r") as f:
            url = f.read().strip()
        if url:
            return {"active": True, "url": url}
    except Exception:
        pass
    return {"active": False, "url": None, "message": "隧道未运行, 运行 bash start_tunnel.sh 启动"}

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
        "sse_clients": len(_sse_clients),
        "auth_enabled": bool(AUTH_TOKEN),
    }

@app.get("/", response_class=HTMLResponse)
async def web_client():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "voice.html")
    with open(html_path, encoding="utf-8") as f: return f.read()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
