"""
魔幻手机 - 实时语音服务
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
from contextlib import asynccontextmanager
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import Response, HTMLResponse, JSONResponse
import uvicorn
import requests
import fcntl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("magic")

@asynccontextmanager
async def lifespan(app):
    """启动+关闭生命周期"""
    # === 启动 ===
    if os.environ.get("SKIP_BACKGROUND") == "1":
        log.info("后台调度器跳过(SKIP_BACKGROUND=1，由HTTP进程管理)")
    else:
        global _main_loop
        _main_loop = asyncio.get_event_loop()
        _start_scheduler()
        _start_proactive()
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
app = FastAPI(title="魔幻手机语音服务", lifespan=lifespan)

# CORS: 允许跨域访问(手机/其他设备)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"])

# 请求日志中间件
@app.middleware("http")
async def request_logger(request: Request, call_next):
    import uuid, time as _t
    rid = str(uuid.uuid4())[:8]
    start = _t.time()
    log.info(f"[{rid}] {request.method} {request.url.path}")
    response = await call_next(request)
    dur = (_t.time() - start) * 1000
    log.info(f"[{rid}] {request.method} {request.url.path} → {response.status_code} {dur:.0f}ms")
    response.headers["X-Request-ID"] = rid
    return response

REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 10MB 音频上限
MAX_TEXT_LENGTH = 500  # 文字输入上限

# ===== 音频转wav =====
def to_wav(data: bytes, ext: str) -> bytes:
    if ext in ("wav", "wave"):
        return data
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(data)
        inp = f.name
    out = inp + ".wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-ar", "16000", "-ac", "1", "-f", "wav", out],
                       capture_output=True, timeout=15)
        with open(out, "rb") as f: return f.read()
    except Exception:
        return data
    finally:
        for p in (inp, out):
            try: os.unlink(p)
            except: pass

def _wav_to_mp3(wav_data: bytes, bitrate: str = "32k") -> bytes:
    """WAV音频转MP3(语音级32kbps,约6x压缩), 减少网络传输"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data); inp = f.name
    out = inp.replace(".wav", ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", inp, "-b:a", bitrate, "-ac", "1", out],
                       capture_output=True, timeout=10)
        with open(out, "rb") as f: return f.read()
    except Exception:
        return wav_data  # 失败返回原始WAV
    finally:
        for p in (inp, out):
            try: os.unlink(p)
            except: pass

# ===== 提醒管理 =====
def _load_reminders():
    """带文件锁的提醒加载"""
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except Exception:
        return []

def _save_reminders(data):
    """带文件锁的提醒保存"""
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)

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
_sse_clients = []  # 已连接的SSE客户端队列列表
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
    while True:
        try:
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
AMAP_KEY = os.getenv("AMAP_KEY", "REDACTED")
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

        except Exception as e:
            log.error(f"[suggest] 主动建议异常: {e}")
        time.sleep(60)  # 每分钟检查一次时间条件

def _start_proactive():
    t = threading.Thread(target=_proactive_suggestions, daemon=True)
    t.start()

def _brain_is_warm():
    """检查大脑是否已预热"""
    try:
        from voice_agent import _brain
        return _brain is not None
    except Exception:
        return False

# ===== 预热大脑(修复asyncio子线程问题) =====
def _warmup_brain():
    """后台预启动大脑+6MCP，首请求省~9秒"""
    def _w():
        log.info("[warmup] 预启动大脑+6MCP...")
        try:
            # 为子线程创建独立event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from voice_agent import _build_brain
            import voice_agent
            voice_agent._brain = _build_brain()
            # 跑一次简单请求预热
            for rsp in voice_agent._brain.run([{'role': 'user', 'content': '你好'}]):
                pass
            log.info("[warmup] 大脑+6MCP预启动完成，首请求将更快")
        except Exception as e:
            log.warning(f"[warmup] 预热失败(不影响使用): {e}")
    threading.Thread(target=_w, daemon=True).start()

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
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/chat")
async def chat_api(req: Request):
    body = await req.json()
    text = body.get("message", "")
    if len(text) > MAX_TEXT_LENGTH:
        return JSONResponse({"error": f"输入过长({len(text)}字), 上限{MAX_TEXT_LENGTH}字"}, status_code=413)
    from voice_agent import brain
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(brain, text), timeout=30)
        return {"reply": reply}
    except asyncio.TimeoutError:
        log.error("/api/chat 超时(30s)")
        return JSONResponse({"error": "思考超时，请重试"}, status_code=504)
    except Exception as e:
        log.error(f"/api/chat 异常: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/reset")
async def reset_conversation():
    """清空对话历史，开始新对话"""
    from voice_agent import reset_history
    reset_history()
    return {"ok": True, "message": "对话已重置"}

@app.get("/api/status")
async def system_status():
    """系统状态(设备+服务+提醒)"""
    import psutil, socket, platform
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        "device": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_cores": psutil.cpu_count(),
        "memory_total_gb": round(vm.total / 1073741824, 1),
        "memory_used_gb": round((vm.total - vm.available) / 1073741824, 1),
        "memory_percent": vm.percent,
        "disk_percent": disk.percent,
        "reminders_pending": len([r for r in _load_reminders() if not r.get("done")]),
        "brain_ready": _brain_is_warm(),
    }

@app.get("/api/reminders")
async def list_reminders():
    data = _load_reminders()
    pending = [r for r in data if not r.get("done")]
    return {"total": len(data), "pending": len(pending), "reminders": data}

@app.post("/api/reminders")
async def add_reminder(req: Request):
    body = await req.json()
    text = body.get("text", "").strip()
    time_str = body.get("time", "").strip()
    if not text:
        raise HTTPException(400, "text不能为空")
    if len(text) > 200:
        raise HTTPException(400, "提醒内容过长(上限200字)")
    # 复用baize_skills的时间解析
    due = None
    if time_str:
        import re
        t = datetime.datetime.now()
        tg = t
        ok = False
        mm = re.search(r"(\d+)\s*分(?:钟)?后", time_str)
        if mm:
            tg += datetime.timedelta(minutes=int(mm.group(1)))
            ok = True
        hh = re.search(r"(\d+)\s*(?:小时|个小时)后", time_str)
        if hh:
            tg += datetime.timedelta(hours=int(hh.group(1)))
            ok = True
        dd = re.search(r"(\d+)\s*天后", time_str)
        if dd:
            tg += datetime.timedelta(days=int(dd.group(1)))
            ok = True
        for w, n in [("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)]:
            if w in time_str:
                tg += datetime.timedelta(days=n)
                ok = True
                break
        tm = re.search(r"(\d{1,2})\s*[点时:：]\s*(\d{0,2})", time_str)
        if tm:
            h = int(tm.group(1))
            mi = int(tm.group(2)) if tm.group(2) else (30 if "半" in time_str else 0)
            if ("下午" in time_str or "晚上" in time_str) and h < 12:
                h += 12
            tg = tg.replace(hour=h, minute=mi, second=0, microsecond=0)
            ok = True
        if ok:
            due = tg.isoformat()
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
async def get_conversation():
    """获取当前对话历史"""
    from voice_agent import _history
    return {"history": _history, "count": len(_history)}

@app.post("/api/tts")
async def tts_api(req: Request):
    """文字 → 语音(WAV)"""
    body = await req.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse({"error": "text不能为空"}, status_code=400)
    if len(text) > MAX_TEXT_LENGTH:
        return JSONResponse({"error": f"文本过长({len(text)}字), 上限{MAX_TEXT_LENGTH}字"}, status_code=413)
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
async def export_conversation():
    """导出对话历史为文本"""
    from voice_agent import _history
    lines = []
    for m in _history:
        role = "我" if m.get("role") == "user" else "魔幻手机"
        lines.append(f"[{role}] {m.get('content', '')}")
    text = "\n\n".join(lines) if lines else "(对话历史为空)"
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

@app.get("/api/search")
async def search_conversation(q: str = ""):
    """搜索对话历史中的关键词"""
    if not q:
        return JSONResponse({"error": "请提供搜索关键词?q=xxx"}, status_code=400)
    from voice_agent import _history
    results = []
    for i, m in enumerate(_history):
        content = m.get("content", "")
        if q.lower() in content.lower():
            role = "我" if m.get("role") == "user" else "魔幻手机"
            # 提取匹配上下文
            idx = content.lower().find(q.lower())
            start = max(0, idx - 20)
            end = min(len(content), idx + len(q) + 20)
            ctx = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
            results.append({"role": role, "context": ctx, "full": content[:200]})
    return {"query": q, "count": len(results), "results": results}

@app.get("/api/version")
async def version():
    return {
        "name": "魔幻手机",
        "version": "2.1.0",
        "brain": "GLM-5.2 + Qwen-Agent + 6 MCP",
        "voice": "qwen3-asr/tts-flash (finna)",
        "features": ["语音对话", "对话记忆", "主动提醒", "天气告警", "每日晨报", "系统监控", "CORS"],
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """系统监控面板"""
    import psutil, socket, platform
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    cpu = psutil.cpu_percent(interval=0.5)
    rems = _load_reminders()
    pending = [r for r in rems if not r.get("done")]
    brain_warm = _brain_is_warm()
    from voice_agent import _history
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>魔幻手机 · 监控面板</title><meta http-equiv="refresh" content="10">
<style>*{{margin:0;box-sizing:border-box}}body{{font-family:-apple-system,sans-serif;
background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);color:#eee;min-height:100vh;padding:20px}}
h1{{font-size:22px;margin-bottom:4px;background:linear-gradient(90deg,#e94560,#f5a623);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.sub{{color:#888;font-size:12px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;max-width:900px}}
.card{{background:rgba(255,255,255,.05);border-radius:12px;padding:16px;border:1px solid rgba(255,255,255,.1)}}
.card h3{{color:#f5a623;font-size:14px;margin-bottom:8px}}
.metric{{display:flex;justify-content:space-between;margin:4px 0;font-size:13px}}
.metric .val{{color:#4e9;font-weight:bold}}
.bar{{height:8px;background:rgba(255,255,255,.1);border-radius:4px;margin:4px 0;overflow:hidden}}
.bar div{{height:100%;border-radius:4px;transition:width .3s}}
.green{{background:#4e9}}.yellow{{background:#f5a623}}.red{{background:#e94560}}
.tag{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;margin:2px}}
.ok{{background:rgba(78,255,153,.2);color:#4e9}}.warn{{background:rgba(245,166,35,.2);color:#f5a623}}
a{{color:#6cf;text-decoration:none}}a:hover{{text-decoration:underline}}
.rem{{padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:13px}}
</style></head><body>
<h1>🎛️ 魔幻手机 · 监控面板</h1>
<div class="sub">自动刷新10秒 | <a href="/">语音客户端</a> | <a href="/docs">API文档</a> | <a href="/api/status">JSON状态</a></div>
<div class="grid">
<div class="card"><h3>🖥️ 系统</h3>
<div class="metric"><span>设备</span><span class="val">{socket.gethostname()}</span></div>
<div class="metric"><span>系统</span><span class="val">{platform.system()} {platform.release()}</span></div>
<div class="metric"><span>CPU</span><span class="val">{cpu}%</span></div>
<div class="bar"><div class="{'green' if cpu<70 else 'yellow' if cpu<90 else 'red'}" style="width:{cpu}%"></div></div>
<div class="metric"><span>内存</span><span class="val">{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.0f}GB ({vm.percent}%)</span></div>
<div class="bar"><div class="{'green' if vm.percent<80 else 'yellow' if vm.percent<90 else 'red'}" style="width:{vm.percent}%"></div></div>
<div class="metric"><span>磁盘</span><span class="val">{disk.used//1073741824:.0f}/{disk.total//1073741824:.0f}GB ({disk.percent}%)</span></div>
<div class="bar"><div class="{'green' if disk.percent<80 else 'yellow' if disk.percent<90 else 'red'}" style="width:{disk.percent}%"></div></div>
</div>
<div class="card"><h3>🧠 大脑</h3>
<div class="metric"><span>模型</span><span class="val">GLM-5.2 + 6 MCP</span></div>
<div class="metric"><span>预热状态</span><span class="tag {'ok' if brain_warm else 'warn'}">{'✅ 已就绪' if brain_warm else '⏳ 预热中'}</span></div>
<div class="metric"><span>对话历史</span><span class="val">{len(_history)} 条</span></div>
<div class="metric"><span>语音引擎</span><span class="val">qwen3-asr/tts-flash</span></div>
</div>
<div class="card"><h3>⏰ 提醒 ({len(pending)} 待办)</h3>
{"".join(f'<div class="rem">📌 {r["text"]} <span style="color:#888">⏰{r.get("due","")[:16].replace("T"," ")}</span></div>' for r in pending[:5]) or '<div class="rem" style="color:#666">暂无待办</div>'}
<a href="/api/reminders" style="font-size:12px">查看全部 →</a>
</div>
<div class="card"><h3>🔧 API 端点 (13个)</h3>
<div class="metric"><span>语音</span><span class="val">/api/voice /api/tts /api/asr</span></div>
<div class="metric"><span>对话</span><span class="val">/api/chat /api/reset /api/conversation /api/export</span></div>
<div class="metric"><span>提醒</span><span class="val">/api/reminders (GET/POST/DELETE)</span></div>
<div class="metric"><span>系统</span><span class="val">/api/status /api/version /health</span></div>
</div>
</div>
</body></html>"""

@app.get("/manifest.json")
async def manifest():
    """PWA manifest for mobile install"""
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "魔幻手机",
        "short_name": "魔幻手机",
        "description": "中国版贾维斯 - AI语音助理",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f0c29",
        "theme_color": "#e94560",
    })

@app.get("/health")
def health():
    return {"ok": True, "service": "magic-phone-voice"}

@app.get("/", response_class=HTMLResponse)
async def web_client():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "voice.html")
    with open(html_path, encoding="utf-8") as f: return f.read()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
