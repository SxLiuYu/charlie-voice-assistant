"""Management/config/PWA routes: setup, esp32, preferences, behaviors, protocols, evolution, logs, tts, mcp, internal push.

Extracted from voice_server.py.
"""
import os, sys, json, time, hashlib, logging, asyncio, struct, zlib, platform
import requests

from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.http_helpers import (
    json_response, html_response, file_not_modified_response, file_etag_token,
    weak_etag, if_none_matches, not_modified_response, etag_headers, manifest_response,
)
from app.state import _metrics, sse_client_count, _ws_client_count
from app.auth import AUTH_TOKEN
from app.brain_health import _brain_is_warm
from app.config import http_port, https_port
from app.notifications import async_push_tts_to_xiaozhi, _get_lan_ip
from app import env_catalog
from app.background_task import BackgroundTask

log = logging.getLogger("magic")

router = APIRouter(tags=["manage"])

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUNNEL_FILE = os.path.join(PROJECT_DIR, "tunnel_url.txt")
_LOG_DIR = os.environ.get("ASSISTANT_KID_LOG_DIR", os.path.join(PROJECT_DIR, "logs"))
_start_time = time.time()  # voice_server.py will update this

if getattr(sys, 'frozen', False):
    _ENV_FILE = os.path.join(os.path.dirname(sys.executable), ".env")
else:
    _ENV_FILE = os.path.join(PROJECT_DIR, ".env")

def set_start_time(t):
    global _start_time
    _start_time = t

# ===== PWA / static routes =====

@router.api_route("/manifest.json", methods=["GET", "HEAD"])
async def manifest(request: Request):
    return manifest_response(request)

@router.get("/service-worker.js")
async def service_worker(request: Request):
    sw_path = os.path.join(PROJECT_DIR, "web", "service-worker.js")
    if os.path.exists(sw_path):
        with open(sw_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="application/javascript",
                          headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
    return Response(status_code=404)

@router.get("/icon.svg")
async def icon_svg(request: Request):
    icon_path = os.path.join(PROJECT_DIR, "web", "icon.svg")
    if os.path.exists(icon_path):
        with open(icon_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="image/svg+xml",
                          headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)

def _build_app_icon_png() -> bytes:
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
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"".join((
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
        chunk(b"IEND", b""),
    ))

_APP_ICON_PNG = _build_app_icon_png()
_ICON_HEADERS = {"Cache-Control": "public, max-age=86400"}
_APP_ICON_ETAG = 'W/"' + hashlib.sha256(_APP_ICON_PNG).hexdigest()[:16] + '"'

@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def web_client(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "voice.html")
    return html_response(request, html_path, "voice-html")

@router.api_route("/manage", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def manage_page(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "manage.html")
    return html_response(request, html_path, "manage-html")

@router.api_route("/favicon.ico", methods=["GET", "HEAD"])
@router.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"])
@router.api_route("/apple-touch-icon-precomposed.png", methods=["GET", "HEAD"])
async def app_icon(request: Request):
    if if_none_matches(request, _APP_ICON_ETAG):
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

@router.api_route("/test", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def voice_test(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "voice_test.html")
    return html_response(request, html_path, "voice-test-html")

@router.api_route("/setup", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def setup_page(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "setup.html")
    return html_response(request, html_path, "setup-html")

@router.api_route("/welcome", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def welcome_page(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "welcome.html")
    return html_response(request, html_path, "welcome-html")

# ===== Metrics / health / tunnel =====

@router.get("/api/metrics")
async def metrics(request: Request):
    return json_response(request, lambda: _metrics.summary(exclude_endpoint="/api/metrics"),
                        etag_token=_metrics.token(exclude_endpoint="/api/metrics"))

@router.get("/api/tunnel")
async def tunnel_status(request: Request):
    cached = file_not_modified_response(request, TUNNEL_FILE, "tunnel")
    if cached is not None:
        return cached
    tunnel_token = file_etag_token(TUNNEL_FILE, "tunnel")
    # Lazy import to avoid circular dependency
    try:
        import voice_server
        voice_server._reload_cors_origins()
    except Exception:
        pass
    try:
        with open(TUNNEL_FILE, "r", encoding="utf-8") as f:
            url = f.read().strip()
        tunnel_token_after_read = file_etag_token(TUNNEL_FILE, "tunnel")
        if url:
            return json_response(request, {"active": True, "url": url},
                                etag_token=tunnel_token if tunnel_token == tunnel_token_after_read else None)
    except Exception:
        pass
    tunnel_token_after_read = file_etag_token(TUNNEL_FILE, "tunnel")
    return json_response(request, {"active": False, "url": None, "message": "隧道未运行, 运行 bash start_tunnel.sh 启动"},
                        etag_token=tunnel_token if tunnel_token == tunnel_token_after_read else None)

@router.get("/health")
def health():
    uptime_s = int(time.time() - _start_time) if _start_time else 0
    return {
        "ok": True, "service": "magic-phone-voice", "version": "3.2.0",
        "uptime_seconds": uptime_s, "uptime_human": f"{uptime_s//3600}h{(uptime_s%3600)//60}m",
        "brain_ready": _brain_is_warm(), "websocket_clients": _ws_client_count(),
        "sse_clients": sse_client_count(), "auth_enabled": bool(AUTH_TOKEN),
    }

# ===== Preferences =====
class PreferenceRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=50)
    value: str = Field(..., min_length=1, max_length=200)

@router.get("/api/preferences")
async def get_preferences(request: Request):
    from voice_agent import preferences_conditional
    prefs, prefs_token = preferences_conditional(lambda etag: if_none_matches(request, etag), weak_etag)
    if prefs is None:
        return not_modified_response(weak_etag(prefs_token))
    return json_response(request, {"total": len(prefs), "preferences": prefs}, etag_token=prefs_token)

@router.post("/api/preferences")
async def set_preference_api(req: PreferenceRequest):
    from voice_agent import set_preference
    msg = set_preference(req.key, req.value)
    return {"ok": True, "message": msg, "key": req.key, "value": req.value}

@router.delete("/api/preferences/{key}")
async def del_preference_api(key: str):
    from voice_agent import del_preference
    msg = del_preference(key)
    return {"ok": True, "message": msg}

@router.get("/api/sessions")
async def list_sessions():
    from voice_agent import _session_summaries
    sessions = _session_summaries()
    return {"total": len(sessions), "sessions": sessions}

@router.get("/api/context")
async def get_context(session_id: str = "default"):
    from voice_agent import _context_summaries, _history_snapshot, list_preferences, _estimate_msg_tokens as _est_tokens
    hist = _history_snapshot(session_id)
    summary = _context_summaries.get(session_id, "")
    prefs = list_preferences()
    total_tokens = sum(_est_tokens(m) for m in hist) if hist else 0
    return {
        "session_id": session_id[:16] + "..." if len(session_id) > 16 else session_id,
        "history_count": len(hist), "estimated_tokens": total_tokens, "token_budget": 4000,
        "context_summary": summary[:200] if summary else None,
        "preferences_count": len(prefs), "preferences": prefs,
    }

# ===== Behaviors / decisions / devices =====
@router.get("/api/decisions")
async def decision_status():
    try:
        from app import load_magic_module
        _dec = load_magic_module("magic_decisions", "magic-decisions.py")
        if _dec:
            try:
                from voice_agent import get_user_state
                user_state = get_user_state()
            except Exception:
                user_state = {"state": "unknown"}
            return {"user_state": user_state, "rules": _dec.get_rules(), "history": _dec._load_decision_history(), "summary": _dec.decisions_summary()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/devices")
async def list_devices():
    try:
        from app.state import snapshot_xiaozhi_clients
        from app.mqtt_server import get_server
        ws_clients = snapshot_xiaozhi_clients()
        ws_devices = [{"id": cid[:16] + "...", "type": "websocket", "connected_at": info.get("connected_at", ""), "device_key": info.get("device_key", "")} for cid, info in ws_clients.items()]
        mqtt_server = get_server()
        mqtt_devices = []
        if mqtt_server:
            import app.mqtt_server as _mqtt
            for did, sess in _mqtt._sessions.items():
                addr = sess.get("addr")
                mqtt_devices.append({"id": did, "type": "mqtt", "udp_addr": f"{addr[0]}:{addr[1]}" if addr else "waiting...", "connected_at": sess.get("timestamp", 0), "since_seconds": round(time.time() - sess.get("timestamp", time.time()), 1)})
        return {"total": len(ws_devices) + len(mqtt_devices), "websocket_count": len(ws_devices), "mqtt_count": len(mqtt_devices), "websocket_devices": ws_devices, "mqtt_devices": mqtt_devices, "mqtt_server_running": mqtt_server is not None, "mqtt_udp_port": mqtt_server.udp_port if mqtt_server else 0}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/decisions/config")
async def decisions_config():
    try:
        from app import load_magic_module
        _dec = load_magic_module("magic_decisions", "magic-decisions.py")
        if not _dec:
            return {"error": "magic-decisions模块未加载"}
        rules = _dec.get_rules()
        rule_summary = [{"id": r["id"], "priority": r.get("priority", 0), "confirm": r.get("confirm", False), "condition": r.get("condition", {}).get("check_desc", "")} for r in rules]
        feedback_file = os.path.join(PROJECT_DIR, "data", "decision_feedback.json")
        feedback = {}
        try:
            with open(feedback_file, "r") as f:
                feedback = json.load(f)
        except Exception:
            pass
        return {"cooldown_hours": 12, "rule_count": len(rule_summary), "rules": rule_summary, "feedback": feedback}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/behaviors")
async def behaviors_status():
    try:
        import datetime as _dt
        from collections import Counter
        history = {}
        try:
            hf = os.path.join(PROJECT_DIR, "data", "decision_history.json")
            with open(hf, "r") as f:
                history = json.load(f)
        except Exception:
            pass
        trigger_hours = []
        trigger_counts = Counter()
        for rid, info in history.items():
            if info.get("trigger_time"):
                try:
                    t = _dt.datetime.fromisoformat(info["trigger_time"])
                    trigger_hours.append(t.hour)
                    trigger_counts[rid] += 1
                except Exception:
                    pass
        hour_dist = dict(Counter(trigger_hours)) if trigger_hours else {}
        top_triggered = trigger_counts.most_common(5)
        feedback = {}
        try:
            ff = os.path.join(PROJECT_DIR, "data", "decision_feedback.json")
            with open(ff, "r") as f:
                feedback = json.load(f)
        except Exception:
            pass
        feedback_scores = {}
        for r in (history if isinstance(history, dict) else {}):
            fb = feedback.get(r, {"positive": 0, "negative": 0})
            total = fb["positive"] + fb["negative"]
            score = fb["positive"] / total if total > 0 else None
            feedback_scores[r] = {"score": round(score, 2) if score is not None else None, "total": total}
        return {"decision_triggers": top_triggered, "hour_distribution": hour_dist, "feedback_scores": feedback_scores, "total_decisions_triggered": sum(trigger_counts.values())}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/habits")
async def habits_status():
    try:
        import datetime as _dt
        habits_file = os.path.join(PROJECT_DIR, "data", "habits.json")
        if not os.path.exists(habits_file):
            return {"total": 0, "habits": {}, "insight": "暂无习惯数据，开始记录吧"}
        with open(habits_file, "r", encoding="utf-8") as f:
            habits = json.load(f)
        today = _dt.date.today()
        result = {}
        for habit, data in habits.items():
            logs = data.get("logs", [])
            streak = 0
            check = today
            for d_str in sorted(set(logs), reverse=True):
                try:
                    d = _dt.date.fromisoformat(d_str)
                    if (check - d).days <= streak:
                        streak += 1
                    else:
                        break
                except Exception:
                    break
            result[habit] = {"total_days": len(logs), "streak": streak, "week_days": len([d for d in logs if _dt.date.fromisoformat(d) >= today - _dt.timedelta(days=today.weekday())]) if logs else 0, "last_log": sorted(logs)[-1] if logs else None, "created": data.get("created", ""), "completed_today": today.strftime("%Y-%m-%d") in logs}
        return {"total": len(result), "habits": result, "insight": f"已记录{sum(r['total_days'] for r in result.values())}次习惯打卡" if result else ""}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/memory")
async def memory_status():
    try:
        from app import load_magic_module
        _mem = load_magic_module("magic_memory", "magic-memory.py")
        if _mem:
            return _mem.get_memory_summary()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ===== Setup / config =====
def _parse_env_file(path):
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

def _write_env_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    original_lines = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                original_lines = f.readlines()
        except Exception:
            original_lines = []
    existing_keys = set()
    out_lines = []
    pending_updates = dict(data)
    for raw_line in original_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(raw_line)
            continue
        k = stripped.split("=", 1)[0].strip()
        existing_keys.add(k)
        if k in data and data[k]:
            out_lines.append(f"{k}={data[k]}\n")
            pending_updates.pop(k, None)
        else:
            out_lines.append(raw_line)
    new_added = False
    for entry in env_catalog.all_entries():
        if entry.name in pending_updates and entry.name not in existing_keys:
            if not new_added and out_lines and not out_lines[-1].endswith("\n\n"):
                out_lines.append("\n# === 通过 setup 页面新增 ===\n")
                new_added = True
            out_lines.append(f"{entry.name}={pending_updates[entry.name]}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)

def _reload_runtime_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=True)
        log.info(f"[setup] os.environ 已从 {_ENV_FILE} 重载")
    except Exception as e:
        log.warning(f"[setup] load_dotenv 重载失败: {e}")
    try:
        from app import llm_config as _llm_cfg
        _llm_cfg.reload()
    except Exception as e:
        log.warning(f"[setup] llm_config.reload 失败: {e}")
    try:
        from agent import asr_tts as _asr_tts
        _asr_tts.reload()
    except Exception as e:
        log.warning(f"[setup] asr_tts.reload 失败: {e}")
    try:
        import voice_agent
        voice_agent.reload_brain_config()
    except Exception as e:
        log.warning(f"[setup] reload_brain_config 失败: {e}")

_SETUP_WHITELIST = set(env_catalog.setup_whitelist_keys())

@router.get("/api/welcome/status")
async def welcome_status():
    from app.llm_config import ollama_online
    status = env_catalog.render_welcome_status()
    status["has_env"] = os.path.exists(_ENV_FILE)
    status["ollama_online"] = ollama_online()
    return status

@router.get("/api/setup")
async def get_setup():
    data = _parse_env_file(_ENV_FILE)
    for entry in env_catalog.all_entries():
        if entry.name not in data:
            data[entry.name] = entry.default
    data["__demo_mode"] = env_catalog.demo_mode_active()
    data["__llm_available"] = env_catalog.llm_available()
    data["__missing_required"] = [e.name for e in env_catalog.missing_required()]
    return data

@router.post("/api/setup")
async def post_setup(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求数据格式错误"}, status_code=400)
    safe_data = {k: str(v).strip() for k, v in data.items() if k in _SETUP_WHITELIST and str(v).strip()}
    demo_accept = str(data.get("demo_accept", "false")).lower() in ("1", "true", "yes", "on")
    try:
        _write_env_file(_ENV_FILE, safe_data)
        _reload_runtime_env()
        existing = _parse_env_file(_ENV_FILE)
        llm_ready = bool(existing.get("ARK_KEY")) or bool(existing.get("GLM_KEY"))
        return {"ok": True, "message": "配置已保存并即时生效", "llm_ready": llm_ready}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存失败: {e}"}, status_code=500)

@router.post("/api/setup/verify")
async def verify_setup(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    results = {}
    def _check_glm():
        from app import llm_config as _lc
        ok, msg = _lc.verify_glm_key(body.get("GLM_KEY", ""), body.get("GLM_MODEL", ""))
        results["glm"] = {"ok": ok, "message": msg}
    def _check_baidu():
        from agent import asr_tts as _at
        ok, msg = _at.verify_baidu_key(body.get("BAIDU_APP_ID", ""), body.get("BAIDU_API_KEY", ""), body.get("BAIDU_SECRET_KEY", ""))
        results["baidu"] = {"ok": ok, "message": msg}
    tasks = []
    if body.get("GLM_KEY") or env_catalog.is_configured("GLM_KEY"):
        tasks.append(_check_glm)
    if body.get("BAIDU_API_KEY") or env_catalog.is_configured("BAIDU_API_KEY"):
        tasks.append(_check_baidu)
    loop = asyncio.get_event_loop()
    await asyncio.gather(*[loop.run_in_executor(None, t) for t in tasks])
    all_ok = all(r.get("ok") for r in results.values()) if results else True
    return {"ok": all_ok, "results": results}

@router.get("/api/setup/mcp-status")
async def setup_mcp_status():
    return {
        "groups": [{"key": g, "label": env_catalog.group_label(g), "entries": [{"name": e.name, "configured": e.is_set, "required": e.required, "demo_supported": e.demo_supported, "description": e.description, "get_guide": e.get_guide} for e in env_catalog.entries_for_group(g)]} for g in env_catalog.groups_in_order() if env_catalog.entries_for_group(g)],
        "demo_mode": env_catalog.demo_mode_active(),
        "llm_available": env_catalog.llm_available(),
    }

# ===== ESP32 =====
_esp32_flash = BackgroundTask("esp32_flash")

@router.api_route("/esp32-setup", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def esp32_setup_page(request: Request):
    html_path = os.path.join(PROJECT_DIR, "web", "esp32_setup.html")
    return html_response(request, html_path, "esp32-setup-html")

@router.get("/api/esp32/detect-port")
async def esp32_detect_port():
    import glob as _glob
    ports = []
    system = platform.system()
    if system == "Windows":
        for i in range(1, 257):
            port = f"COM{i}"
            if os.path.exists(port):
                ports.append({"device": port, "board": "ESP32"})
    else:
        for pattern in ["/dev/cu.usbmodem*", "/dev/cu.usbserial*"]:
            for dev in _glob.glob(pattern):
                ports.append({"device": dev, "board": "ESP32"})
    return {"ports": ports}

@router.post("/api/esp32/flash")
async def esp32_flash(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求数据格式错误"}, status_code=400)
    port = data.get("port", "")
    if not port:
        return JSONResponse({"ok": False, "error": "缺少串口字段: port"}, status_code=422)
    if not _esp32_flash.start(_esp32_flash_worker, port):
        return {"started": False, "message": "已有烧录在进行中"}
    return {"started": True, "message": "烧录已启动"}

def _find_esp32_firmware():
    fw_name = "charlie-esp32-flash-16MB.bin"
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", PROJECT_DIR)
        candidates.append(os.path.join(meipass, "firmware", fw_name))
        candidates.append(os.path.join(os.path.dirname(sys.executable), "firmware", fw_name))
    candidates.append(os.path.join(os.path.dirname(PROJECT_DIR), "firmware", fw_name))
    candidates.append(os.path.join(PROJECT_DIR, "firmware", fw_name))
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"固件 {fw_name} 未找到。")

def _esp32_flash_worker(port):
    fw_path = _find_esp32_firmware()
    _esp32_flash.update_progress(f"读取固件 {os.path.basename(fw_path)}")
    _esp32_flash.update_progress("esptool 烧录中（约 30-60 秒，请勿断开设备）")
    import esptool
    argv = ["--chip", "esp32s3", "-p", port, "-b", "115200", "write_flash", "--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", "16MB", "0x0", fw_path]
    try:
        esptool.main(argv)
    except SystemExit as e:
        if getattr(e, "code", 0) not in (0, None):
            raise RuntimeError(f"esptool 退出码 {e.code}")
    _esp32_flash.update_progress("烧录完成")
    _esp32_flash.set_result("flashed", True)
    lan_ip = _get_lan_ip() or "127.0.0.1"
    try:
        r = requests.get(f"http://{lan_ip}:{http_port()}/xiaozhi/ota", timeout=3)
        _esp32_flash.set_result("connectivity", r.status_code == 200)
    except Exception:
        _esp32_flash.set_result("connectivity", False)

@router.get("/api/esp32/config-info")
async def esp32_config_info():
    lan_ip = _get_lan_ip() or ""
    port = http_port()
    return {"lan_ip": lan_ip, "http_port": port, "ota_url": f"http://{lan_ip}:{port}/xiaozhi/ota", "ws_url": f"ws://{lan_ip}:{port}/ws/xiaozhi", "ap_prefix": "lc-s3-wifi-1.54tft-", "portal_url": "http://192.168.4.1"}

@router.get("/api/esp32/flash-status")
async def esp32_flash_status():
    return _esp32_flash.status()

# ===== Model download =====
_model_download = BackgroundTask("model_download")

def _check_model_exists():
    model_path = os.getenv("SENSE_VOICE_MODEL", os.path.join(PROJECT_DIR, "models", "sense-voice"))
    return os.path.exists(os.path.join(model_path, "model.int8.onnx"))

def _download_model_worker():
    import subprocess
    script = os.path.join(PROJECT_DIR, "scripts", "download-models.py")
    subprocess.run([sys.executable, script], check=True, capture_output=True, text=True, cwd=PROJECT_DIR)

@router.post("/api/setup/download-model")
async def download_model():
    import voice_server
    if _model_download.is_active():
        return {"started": False, "message": "已有下载在进行中"}
    if voice_server._check_model_exists():
        return {"started": False, "message": "模型已存在", "model_exists": True}
    _model_download.start(_download_model_worker)
    return {"started": True, "message": "下载已启动（237MB，后台进行）"}

@router.get("/api/setup/download-status")
async def download_status():
    import voice_server
    s = _model_download.status()
    return {"downloading": s["active"], "error": s["error"], "model_exists": voice_server._check_model_exists()}

# ===== Protocols / evolution =====
@router.get("/api/protocols")
async def protocols_status():
    try:
        from app import load_magic_module
        _sc = load_magic_module("magic_scenes", "magic-scenes.py")
        if _sc:
            protocols = _sc._load_protocols()
            result = [{"key": key, "name": proto.get("name", key), "triggers": ", ".join(proto.get("triggers", [])), "step_count": len(proto.get("steps", [])), "is_builtin": key in _sc._BUILTIN_PROTOCOLS} for key, proto in protocols.items()]
            return {"protocols": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/protocols/learn")
async def protocols_learn(body: dict):
    try:
        from app import load_magic_module
        _sc = load_magic_module("magic_scenes", "magic-scenes.py")
        if _sc:
            result = _sc.learn_protocol(body.get("name", ""), body.get("trigger_words", ""), body.get("steps_description", ""))
            return {"ok": True, "message": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/evolution")
async def evolution_status():
    try:
        from app import load_magic_module
        _evo = load_magic_module("magic_evolution", "magic-evolution.py")
        if _evo:
            data = _evo._load_evolution_data()
            patterns = data.get("usage_patterns", {})
            adaptation = data.get("adaptation_state", {})
            learned = data.get("learned_preferences", {})
            return {"total_conversations": patterns.get("total_conversations", 0), "response_style": adaptation.get("response_style", "default"), "topic_count": len(patterns.get("top_topics", [])), "preferences": learned, "active_hours": patterns.get("active_hours", []), "top_topics": patterns.get("top_topics", [])}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/evolution/learn")
async def evolution_learn():
    try:
        from voice_agent import _get_brain
        brain = _get_brain("magic-evolution")
        for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
            pass
        return {"ok": True, "message": "学习完成"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ===== Wake / user / logs =====
@router.post("/api/wake/toggle")
async def wake_toggle_api(enabled: bool = None):
    import local_wake
    current = local_wake.toggle_wake(enabled)
    return {"enabled": current}

@router.get("/api/wake/status")
async def wake_status_api():
    import local_wake
    return local_wake.wake_status()

@router.post("/api/user/switch")
async def switch_user_api(user_id: str = "default"):
    from voice_agent import set_current_user, get_current_user
    set_current_user(user_id)
    return {"user_id": get_current_user(), "message": f"已切换到用户: {user_id}"}

@router.get("/api/user/current")
async def current_user_api():
    from voice_agent import get_current_user
    return {"user_id": get_current_user()}

@router.get("/api/logs")
async def get_logs(lines: int = 100, filter: str = ""):
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

# ===== TTS voices =====
AVAILABLE_TTS_VOICES = {
    "Cherry": "Cherry - 自然女声", "Stella": "Stella - 温柔女声",
    "Alex": "Alex - 沉稳男声", "Vega": "Vega - 活力女声",
    "Nova": "Nova - 甜美女声", "Echo": "Echo - 中性声",
}

@router.get("/api/tts/voices")
async def list_tts_voices():
    import voice_agent
    current = voice_agent.TTS_VOICE
    voices = [{"id": key, "name": desc, "current": key == current} for key, desc in AVAILABLE_TTS_VOICES.items()]
    return {"voices": voices, "current": current}

@router.post("/api/tts/voice")
async def set_tts_voice(voice_id: str = "Cherry"):
    import voice_agent
    if voice_id not in AVAILABLE_TTS_VOICES:
        return JSONResponse({"error": f"未知音色: {voice_id}"}, status_code=400)
    voice_agent.TTS_VOICE = voice_id
    voice_agent._tts_cache.clear()
    return {"ok": True, "voice": voice_id, "name": AVAILABLE_TTS_VOICES[voice_id]}

# ===== MCP =====
@router.get("/api/mcp/servers")
async def list_mcp_servers():
    from voice_agent import _build_brain, _brains
    enabled_env = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life,magic-scenes,magic-evolution,magic-summary,magic-wardrobe,magic-browser,magic-apps,magic-feishu,magic-douyin,magic-taobao").split(",")
    enabled_env = [s.strip() for s in enabled_env if s.strip()]
    mcp_servers = {
        "amap-maps": "高德地图/天气", "magic-info": "信息查询(时间/天气/新闻/翻译)",
        "magic-music": "音乐播放", "magic-reminder": "提醒/定时器", "magic-notes": "备忘录",
        "magic-system": "系统控制(音量/语速)", "magic-life": "生活服务(外卖/充电桩)",
        "magic-scenes": "场景自动化", "magic-apps": "App控制", "magic-feishu": "飞书集成",
        "magic-douyin": "抖音", "magic-taobao": "淘宝/京东", "magic-evolution": "自进化",
        "magic-summary": "每日摘要", "magic-wardrobe": "穿搭推荐", "magic-browser": "浏览器控制",
        "baize-skills": "互联网搜索", "filesystem": "文件系统", "ac-control": "空调控制", "mimo-vision": "视觉识别",
    }
    result = [{"id": key, "name": name, "enabled": key in enabled_env, "cached": key in _brains} for key, name in mcp_servers.items()]
    return {"servers": result, "enabled_list": ",".join(enabled_env)}

@router.post("/api/mcp/toggle")
async def toggle_mcp_server(server_id: str = "", enabled: bool = True):
    if not server_id:
        return JSONResponse({"error": "缺少 server_id 参数"}, status_code=400)
    current = os.getenv("MCP_SERVERS", "amap-maps,baize-skills,filesystem,magic-music,magic-reminder,magic-notes,magic-system,magic-info,magic-life,magic-scenes,magic-evolution,magic-summary,magic-wardrobe,magic-browser,magic-apps,magic-feishu,magic-douyin,magic-taobao")
    enabled_list = [s.strip() for s in current.split(",") if s.strip()]
    if enabled and server_id not in enabled_list:
        enabled_list.append(server_id)
    elif not enabled and server_id in enabled_list:
        enabled_list.remove(server_id)
    os.environ["MCP_SERVERS"] = ",".join(enabled_list)
    from voice_agent import restart_brain
    restart_brain()
    return {"ok": True, "server_id": server_id, "enabled": enabled, "enabled_list": enabled_list}

# ===== Internal xiaozhi push =====
@router.post("/api/internal/xiaozhi-push")
async def _internal_xiaozhi_push(payload: dict, request: Request):
    internal_token = os.getenv("INTERNAL_API_TOKEN", "")
    if internal_token:
        auth = request.headers.get("X-Internal-Token", "")
        if auth != internal_token:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    else:
        client_ip = request.client.host if request.client else ""
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    text = payload.get("text", "")
    mp3_b64 = payload.get("mp3", "")
    if not text or not mp3_b64:
        return {"ok": False, "error": "missing text or mp3"}
    import base64 as _b64
    from app.state import snapshot_xiaozhi_clients
    mp3_data = _b64.b64decode(mp3_b64)
    clients = snapshot_xiaozhi_clients()
    pushed = 0
    for cid, info in clients.items():
        ws = info["ws"]
        try:
            await async_push_tts_to_xiaozhi(ws, text, mp3_data)
            pushed += 1
        except Exception as e:
            log.warning(f"[xiaozhi-push] 内部转发失败 {cid}: {e}")
    mqtt_pushed = False
    try:
        from app.mqtt_server import push_tts_to_mqtt
        mqtt_pushed = push_tts_to_mqtt(text, mp3_data)
        if mqtt_pushed:
            pushed += 1
    except Exception:
        pass
    return {"ok": True, "pushed": pushed, "total": len(clients), "mqtt": mqtt_pushed}
