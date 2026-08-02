"""只读系统路由(APIRouter)

依赖来自 app 子模块和 voice_agent, 但不导入 voice_server, 避免循环 import.
"""
import html
import ipaddress
import os
import platform
import socket
import threading
import time

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
import psutil
import voice_agent

from app.config import http_port, https_port
from app.brain_health import _get_brain_health
from app.reminders import _load_reminders, proactive_lock_status, scheduler_lock_status
from app.state import (_metrics, _poll_telemetry, _ws_clients, _rate_buckets,
    _session_buckets, _ws_client_count, _RATE_GENERAL, _RATE_VOICE, _RATE_PER_SESSION,
    _interrupt_telemetry, sse_client_count)

system_router = APIRouter(tags=["system"])

# Establish a non-blocking CPU baseline at import time. Status endpoints then
# read CPU usage since the previous sample without blocking each request for
# 0.5s.
psutil.cpu_percent(interval=None)


class PollingTelemetryRequest(BaseModel):
    event: Literal["paused", "resumed", "error"]
    job: Literal["reminders", "preferences", "tunnel"] | None = Field(default=None)

    @model_validator(mode="after")
    def require_job_unless_resumed(self):
        if self.event != "resumed" and not self.job:
            raise ValueError("job is required for polling telemetry events")
        return self

_VIRTUAL_INTERFACE_PREFIXES = (
    "lo", "utun", "bridge", "awdl", "llw", "anpi", "ap", "gif", "stf"
)
_PREFERRED_INTERFACE_PREFIXES = ("en", "eth", "wlan")
_LAN_ACCESS_TTL_SECONDS = 2.0
_lan_access_cache = {"at": 0.0, "key": None, "value": None}
_lan_access_lock = threading.Lock()
_host_metadata_cache = None


def _invalidate_lan_access_cache():
    with _lan_access_lock:
        _lan_access_cache["at"] = 0.0
        _lan_access_cache["key"] = None
        _lan_access_cache["value"] = None


def _lan_access_cache_key() -> tuple:
    return (http_port(), https_port(), bool(os.getenv("AUTH_TOKEN", "").strip()))


def _lan_access() -> dict:
    """Return the best LAN URL without shelling out or creating runtime files."""
    now = time.time()
    cache_key = _lan_access_cache_key()
    with _lan_access_lock:
        cached = _lan_access_cache["value"]
        if (
            cached is not None
            and _lan_access_cache["key"] == cache_key
            and now - _lan_access_cache["at"] < _LAN_ACCESS_TTL_SECONDS
        ):
            return dict(cached)

    candidates = []
    for interface, addresses in psutil.net_if_addrs().items():
        name = interface.lower()
        if name.startswith(_VIRTUAL_INTERFACE_PREFIXES):
            continue
        for addr in addresses:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address.split("%", 1)[0]
            try:
                parsed = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if (
                parsed.is_loopback or parsed.is_link_local or parsed.is_multicast
                or parsed.is_reserved or parsed.is_unspecified or not parsed.is_private
            ):
                continue
            preferred = 0 if name.startswith(_PREFERRED_INTERFACE_PREFIXES) else 1
            candidates.append((preferred, interface, ip))

    interface = None
    lan_ip = None
    http_url = None
    https_url = None
    if candidates:
        _, interface, lan_ip = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
        http_url = f"http://{lan_ip}:{http_port()}"
        https_url = f"https://{lan_ip}:{https_port()}"

    result = {
        "hostname": socket.gethostname(),
        "interface": interface,
        "lan_ip": lan_ip,
        "http_url": http_url,
        "https_url": https_url,
        "auth_required": bool(os.getenv("AUTH_TOKEN", "").strip()),
        "access_hint": "手机和电脑连接同一 Wi-Fi/局域网；若打不开，先关闭手机代理或 VPN。HTTPS 使用自签证书时需在手机上确认继续访问。",
    }
    with _lan_access_lock:
        _lan_access_cache["at"] = now
        _lan_access_cache["key"] = cache_key
        _lan_access_cache["value"] = dict(result)
    return result

def _reminder_summary(reminders, include_items=False):
    pending = []
    delivery = {"active": 0, "delivering": 0, "retry": 0, "failed": 0}
    for reminder in reminders:
        if not isinstance(reminder, dict):
            continue
        if not reminder.get("done"):
            if include_items:
                pending.append(reminder)
            else:
                pending.append(True)

        state = reminder.get("delivery_state")
        if state in ("delivering", "retry"):
            delivery[state] += 1
            delivery["active"] += 1
        elif state == "failed":
            delivery["failed"] += 1

    return pending, delivery


def _host_metadata():
    global _host_metadata_cache
    if _host_metadata_cache is None:
        _host_metadata_cache = {
            "device": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
        }
    return _host_metadata_cache

def _render_dashboard_reminder(reminder):
    text = html.escape(str(reminder.get("text", "未命名提醒")))
    due_value = reminder.get("due")
    due_text = str(due_value) if due_value else ""
    due = html.escape(due_text[:16].replace("T", " "))
    due_html = f' <span style="color:#888">⏰{due}</span>' if due else ""
    return f'<div class="rem">📌 {text}{due_html}</div>'

def _pref_count() -> int:
    """获取用户偏好数量"""
    try:
        return voice_agent.preference_count()
    except Exception:
        return 0

@system_router.get("/api/version")
async def version():
    return {
        "name": "Charlie",
        "version": "3.1.0",
        "brain": "deepseek-v4-flash + Qwen-Agent + 4 MCP (可配置)",
        "voice": "qwen3-asr/tts-flash (finna)",
        "features": ["流式语音对话", "流式文字对话", "大脑逐句产出", "TTS批量推送",
                     "语音对话", "对话记忆", "对话搜索", "主动提醒", "天气告警", "每日晨报", "系统监控",
                     "SSE实时推送", "WebSocket双向通信", "TTS打断", "限流防护", "CORS加固", "PWA移动端",
                     "响应缓存", "看门狗", "MP3压缩", "线程池", "Markdown清理TTS", "逗号软分割",
                     "连接重试", "文件锁", "大脑断路器", "多用户会话隔离", "唤醒词检测",
                     "API密钥故障转移", "输入清洗XSS防护", "结构化日志", "优雅降级",
                     "对话时间戳", "Token感知截断", "连接池调优", "对话导出分页", "用户偏好系统", "对话上下文摘要", "会话级限流", "API令牌认证"],
        "streaming": {
            "chat": "/api/chat/stream (SSE: text+audio+done)",
            "voice": "/api/voice/stream (SSE: asr+text+audio+done)",
            "websocket": "/ws (双向: text/audio/interrupt)",
            "tts_batch_size": "50字/块",
        }
    }

@system_router.get("/api/status")
async def system_status():
    """系统状态(设备+服务+提醒)"""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    rems = _load_reminders()
    pending, reminder_delivery = _reminder_summary(rems)
    brain_status = _get_brain_health()
    host = _host_metadata()
    scheduler = scheduler_lock_status()
    proactive_suggestions = proactive_lock_status()
    return {
        "device": host["device"],
        "os": host["os"],
        "network": _lan_access(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(),
        "memory_total_gb": round(vm.total / 1073741824, 1),
        "memory_used_gb": round((vm.total - vm.available) / 1073741824, 1),
        "memory_percent": vm.percent,
        "disk_percent": disk.percent,
        "reminders_pending": len(pending),
        "reminder_delivery": reminder_delivery,
        "brain_ready": brain_status["ready"],
        "brain_health": brain_status,
        "brain_status": brain_status,
        "tts": voice_agent.tts_status(),
        "intent_classifier": voice_agent.intent_classifier_status(),
        "scheduler": scheduler,
        "proactive_suggestions": proactive_suggestions,
        "polling": _poll_telemetry.summary(),
        "interrupts": _interrupt_telemetry.summary(),
        "websocket_connections": _ws_client_count(),
        "metrics": _metrics.summary(exclude_endpoint="/api/metrics"),
        "rate_limit": {
            "tracked_ips": len(_rate_buckets),
            "tracked_sessions": len(_session_buckets),
            "general_limit": _RATE_GENERAL,
            "voice_limit": _RATE_VOICE,
            "session_limit": _RATE_PER_SESSION,
        },
    }


@system_router.post("/api/polling-telemetry", status_code=202)
async def report_polling_telemetry(payload: PollingTelemetryRequest):
    if payload.event in ("error", "errors"):
        _poll_telemetry.record_failure(payload.job)
    else:
        _poll_telemetry.record(payload.event, payload.job)
    return {"ok": True}

@system_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """系统监控面板"""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    cpu = psutil.cpu_percent(interval=None)
    network = _lan_access()
    rems = _load_reminders()
    pending, reminder_delivery = _reminder_summary(rems, include_items=True)
    brain_status = voice_agent.brain_status()
    brain_warm = brain_status["ready"]
    _history = voice_agent._history
    tts = voice_agent.tts_status()
    intent = voice_agent.intent_classifier_status()
    scheduler = scheduler_lock_status()
    proactive_suggestions = proactive_lock_status()
    polling = _poll_telemetry.summary()
    polling_totals = polling.get("totals", {})
    interrupts = _interrupt_telemetry.summary()
    interrupt_follow_up = interrupts.get("last_follow_up")
    if interrupt_follow_up:
        interrupted_reply = html.escape(str(interrupt_follow_up.get("interrupted_reply", ""))[:80])
        follow_up_text = html.escape(str(interrupt_follow_up.get("text", ""))[:80])
        follow_up_source = "语音" if interrupt_follow_up.get("source") == "asr" else "文字"
        interrupt_context = f"{interrupted_reply} → {follow_up_source}: {follow_up_text}"
    else:
        last_reply = html.escape(str(interrupts.get("last_reply", ""))[:80])
        interrupt_context = last_reply or "暂无"
    m = _metrics.summary(exclude_endpoint="/api/metrics", include_endpoints=False)
    host = _host_metadata()
    tts_label = "TTS 冷却中" if tts.get("active") else "TTS 正常"
    tts_class = "warn" if tts.get("active") else "ok"
    tts_remaining = f"{tts.get('remaining_seconds', 0):g} 秒"
    intent_label = "意图分类熔断中" if intent.get("circuit_open") else "意图分类正常"
    intent_class = "warn" if intent.get("circuit_open") else "ok"
    scheduler_label = "调度器锁已占用" if scheduler.get("locked") else "调度器待命"
    scheduler_class = "warn" if scheduler.get("locked") else "ok"
    scheduler_owner = scheduler.get("owner_pid") or "无"
    lock_file = html.escape(str(scheduler.get("lock_file", "")))
    proactive_label = "主动建议锁已占用" if proactive_suggestions.get("locked") else "主动建议待命"
    proactive_class = "warn" if proactive_suggestions.get("locked") else "ok"
    proactive_owner = proactive_suggestions.get("owner_pid") or "无"
    proactive_lock_file = html.escape(str(proactive_suggestions.get("lock_file", "")))
    conditional_requests = m.get("conditional_requests", 0)
    not_modified = m.get("not_modified", m.get("cache_hits", 0))
    not_modified_rate = m.get("not_modified_rate", 0)
    lan_http = html.escape(network.get("http_url") or "未检测到局域网 IP")
    lan_https = html.escape(network.get("https_url") or "未检测到局域网 IP")
    lan_hint = html.escape(network.get("access_hint", ""))
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Charlie · 监控面板</title><meta http-equiv="refresh" content="10">
<style>*{{margin:0;box-sizing:border-box}}body{{font-family:-apple-system,sans-serif;
background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);color:#eee;min-height:100vh;padding:20px}}
h1{{font-size:22px;margin-bottom:4px;background:linear-gradient(90deg,#e94560,#f5a623);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.sub{{color:#888;font-size:12px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;max-width:900px}}
.card{{background:rgba(255,255,255,.05);border-radius:12px;padding:16px;border:1px solid rgba(255,255,255,.1)}}
.card h3{{color:#f5a623;font-size:14px;margin-bottom:8px}}
.metric{{display:flex;justify-content:space-between;gap:12px;margin:4px 0;font-size:13px}}
.metric .val{{color:#4e9;font-weight:bold}}
.metric .val{{word-break:break-all;text-align:right}}
.bar{{height:8px;background:rgba(255,255,255,.1);border-radius:4px;margin:4px 0;overflow:hidden}}
.bar div{{height:100%;border-radius:4px;transition:width .3s}}
.green{{background:#4e9}}.yellow{{background:#f5a623}}.red{{background:#e94560}}
.tag{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;margin:2px}}
.ok{{background:rgba(78,255,153,.2);color:#4e9}}.warn{{background:rgba(245,166,35,.2);color:#f5a623}}
a{{color:#6cf;text-decoration:none}}a:hover{{text-decoration:underline}}
.rem{{padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:13px}}
</style></head><body>
<h1>🎛️ Charlie · 监控面板</h1>
<div class="sub">自动刷新10秒 | <a href="/">语音客户端</a> | <a href="/docs">API文档</a> | <a href="/api/status">JSON状态</a></div>
<div class="grid">
<div class="card"><h3>🖥️ 系统</h3>
<div class="metric"><span>设备</span><span class="val">{host['device']}</span></div>
<div class="metric"><span>系统</span><span class="val">{host['os']}</span></div>
<div class="metric"><span>局域网 HTTP</span><span class="val">{lan_http}</span></div>
<div class="metric"><span>局域网 HTTPS</span><span class="val">{lan_https}</span></div>
<div class="metric"><span>访问提示</span><span class="val" style="font-weight:400">{lan_hint}</span></div>
<div class="metric"><span>CPU</span><span class="val">{cpu}%</span></div>
<div class="bar"><div class="{'green' if cpu<70 else 'yellow' if cpu<90 else 'red'}" style="width:{cpu}%"></div></div>
<div class="metric"><span>内存</span><span class="val">{(vm.total-vm.available)//1073741824:.1f}/{vm.total//1073741824:.0f}GB ({vm.percent}%)</span></div>
<div class="bar"><div class="{'green' if vm.percent<80 else 'yellow' if vm.percent<90 else 'red'}" style="width:{vm.percent}%"></div></div>
<div class="metric"><span>磁盘</span><span class="val">{disk.used//1073741824:.0f}/{disk.total//1073741824:.0f}GB ({disk.percent}%)</span></div>
<div class="bar"><div class="{'green' if disk.percent<80 else 'yellow' if disk.percent<90 else 'red'}" style="width:{disk.percent}%"></div></div>
</div>
<div class="card"><h3>🧠 大脑</h3>
<div class="metric"><span>模型</span><span class="val">deepseek-v4-flash + 4 MCP (可配置)</span></div>
<div class="metric"><span>预热状态</span><span class="tag {'ok' if brain_warm else 'warn'}">{'✅ 已就绪' if brain_warm else '⏳ 预热中'}</span></div>
<div class="metric"><span>对话历史</span><span class="val">{len(_history)} 条</span></div>
<div class="metric"><span>语音引擎</span><span class="val">qwen3-asr/tts-flash</span></div>
<div class="metric"><span>用户偏好</span><span class="val">{_pref_count()} 项</span></div>
</div>
<div class="card"><h3>🩺 运行健康</h3>
<div class="metric"><span>TTS</span><span class="tag {tts_class}">{tts_label}</span></div>
<div class="metric"><span>冷却剩余</span><span class="val">{tts_remaining}</span></div>
<div class="metric"><span>意图分类</span><span class="tag {intent_class}">{intent_label}</span></div>
<div class="metric"><span>连续失败</span><span class="val">{intent.get('consecutive_failures', 0)}/{intent.get('failure_threshold', 0)}</span></div>
<div class="metric"><span>TTS连续失败</span><span class="val">{tts.get('consecutive_failures', 0)}/{tts.get('failure_threshold', 0)}</span></div>
<div class="metric"><span>大脑累计失败</span><span class="val">{brain_status.get('total_failures', 0)}</span></div>
<div class="metric"><span>熔断剩余</span><span class="val">{intent.get('remaining_seconds', 0):g} 秒</span></div>
<div class="metric"><span>提醒调度</span><span class="tag {scheduler_class}">{scheduler_label}</span></div>
<div class="metric"><span>主动建议</span><span class="tag {proactive_class}">{proactive_label}</span></div>
<div class="metric"><span>提醒投递</span><span class="val">重试中 {reminder_delivery['retry']} · 失败 {reminder_delivery['failed']}</span></div>
<div class="metric"><span>轮询暂停</span><span class="val">暂停 {polling_totals.get('paused', 0)} · 恢复 {polling_totals.get('resumed', 0)}</span></div>
<div class="metric"><span>轮询退避</span><span class="val">退避 {polling_totals.get('backoff', 0)} · 失败 {polling_totals.get('errors', 0)}</span></div>
<div class="metric"><span>语音打断</span><span class="val">打断 {interrupts.get('total', 0)} · 带回复 {interrupts.get('with_reply', 0)}</span></div>
<div class="metric"><span>语音打断意图</span><span class="val" title="{html.escape(str(interrupts.get('last_reply', '')))}">{interrupt_context}</span></div>
<div class="metric"><span>调度持有进程</span><span class="val">PID {scheduler_owner}</span></div>
<div class="metric"><span>主动建议进程</span><span class="val">PID {proactive_owner}</span></div>
<div class="metric"><span>调度锁文件</span><span class="val" title="{lock_file}">{lock_file}</span></div>
<div class="metric"><span>建议锁文件</span><span class="val" title="{proactive_lock_file}">{proactive_lock_file}</span></div>
</div>
<div class="card"><h3>⏰ 提醒 ({len(pending)} 待办)</h3>
{"".join(_render_dashboard_reminder(r) for r in pending[:5]) or '<div class="rem" style="color:#666">暂无待办</div>'}
<a href="/api/reminders" style="font-size:12px">查看全部 →</a>
</div>
<div class="card"><h3>🔧 API 端点 (28个 + WS)</h3>
<div class="metric"><span>语音</span><span class="val">/api/voice /api/voice/stream</span></div>
<div class="metric"><span>TTS/ASR</span><span class="val">/api/tts /api/asr</span></div>
<div class="metric"><span>对话</span><span class="val">/api/chat /api/chat/stream</span></div>
<div class="metric"><span>提醒/搜索</span><span class="val">/api/reminders /api/search /api/export</span></div>
<div class="metric"><span>实时/系统</span><span class="val">/api/events /api/metrics /api/status /api/polling-telemetry</span></div>
<div class="metric"><span>WebSocket</span><span class="val">/ws (双向通信)</span></div>
</div>
<div class="card"><h3>🔌 实时连接</h3>
<div class="metric"><span>WebSocket</span><span class="val">{len(_ws_clients)} 个连接</span></div>
<div class="metric"><span>SSE通知</span><span class="val">{sse_client_count()} 个连接</span></div>
<div class="metric"><span>限流IP</span><span class="val">{len(_rate_buckets)} 个</span></div>
<div class="metric"><span>限流策略</span><span class="val">普通{_RATE_GENERAL}/min 语音{_RATE_VOICE}/min</span></div>
</div>
<div class="card"><h3>📊 请求指标</h3>
<div class="metric"><span>总请求</span><span class="val">{m["total_requests"]}</span></div>
<div class="metric"><span>错误</span><span class="val">{m["total_errors"]}</span></div>
<div class="metric"><span>304命中</span><span class="val">{not_modified}/{conditional_requests} ({not_modified_rate}%)</span></div>
<div class="metric"><span>平均响应</span><span class="val">{m["avg_response_ms"]}ms</span></div>
<div class="metric"><span>P95响应</span><span class="val">{m["p95_response_ms"]}ms</span></div>
<a href="/api/metrics" style="font-size:12px">详情 →</a>
</div>
</div>
</body></html>"""
