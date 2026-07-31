"""只读系统路由(APIRouter)

依赖均来自 app 子模块(brain_health/reminders/state), 不反向依赖 voice_server, 避免循环import.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from app.brain_health import _brain_is_warm, _get_brain_health
from app.reminders import _load_reminders
from app.state import (_metrics, _ws_clients, _sse_clients, _rate_buckets,
    _session_buckets, _ws_client_count, _RATE_GENERAL, _RATE_VOICE, _RATE_PER_SESSION)

system_router = APIRouter(tags=["system"])

def _pref_count() -> int:
    """获取用户偏好数量"""
    try:
        from voice_agent import list_preferences
        return len(list_preferences())
    except Exception:
        return 0

@system_router.get("/api/version")
async def version():
    return {
        "name": "助手小子",
        "version": "3.1.0",
        "brain": "GLM-5.2 + Qwen-Agent + 4 MCP (可配置)",
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
        "brain_health": _get_brain_health(),
        "websocket_connections": _ws_client_count(),
        "rate_limit": {
            "tracked_ips": len(_rate_buckets),
            "tracked_sessions": len(_session_buckets),
            "general_limit": _RATE_GENERAL,
            "voice_limit": _RATE_VOICE,
            "session_limit": _RATE_PER_SESSION,
        },
    }

@system_router.get("/dashboard", response_class=HTMLResponse)
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
    m = _metrics.summary()
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>助手小子 · 监控面板</title><meta http-equiv="refresh" content="10">
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
<h1>🎛️ 助手小子 · 监控面板</h1>
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
<div class="metric"><span>模型</span><span class="val">GLM-5.2 + 4 MCP (可配置)</span></div>
<div class="metric"><span>预热状态</span><span class="tag {'ok' if brain_warm else 'warn'}">{'✅ 已就绪' if brain_warm else '⏳ 预热中'}</span></div>
<div class="metric"><span>对话历史</span><span class="val">{len(_history)} 条</span></div>
<div class="metric"><span>语音引擎</span><span class="val">qwen3-asr/tts-flash</span></div>
<div class="metric"><span>用户偏好</span><span class="val">{_pref_count()} 项</span></div>
</div>
<div class="card"><h3>⏰ 提醒 ({len(pending)} 待办)</h3>
{"".join(f'<div class="rem">📌 {r["text"]} <span style="color:#888">⏰{r.get("due","")[:16].replace("T"," ")}</span></div>' for r in pending[:5]) or '<div class="rem" style="color:#666">暂无待办</div>'}
<a href="/api/reminders" style="font-size:12px">查看全部 →</a>
</div>
<div class="card"><h3>🔧 API 端点 (27个 + WS)</h3>
<div class="metric"><span>语音</span><span class="val">/api/voice /api/voice/stream</span></div>
<div class="metric"><span>TTS/ASR</span><span class="val">/api/tts /api/asr</span></div>
<div class="metric"><span>对话</span><span class="val">/api/chat /api/chat/stream</span></div>
<div class="metric"><span>提醒/搜索</span><span class="val">/api/reminders /api/search /api/export</span></div>
<div class="metric"><span>实时/系统</span><span class="val">/api/events /api/metrics /api/status</span></div>
<div class="metric"><span>WebSocket</span><span class="val">/ws (双向通信)</span></div>
</div>
<div class="card"><h3>🔌 实时连接</h3>
<div class="metric"><span>WebSocket</span><span class="val">{len(_ws_clients)} 个连接</span></div>
<div class="metric"><span>SSE通知</span><span class="val">{len(_sse_clients)} 个连接</span></div>
<div class="metric"><span>限流IP</span><span class="val">{len(_rate_buckets)} 个</span></div>
<div class="metric"><span>限流策略</span><span class="val">普通{_RATE_GENERAL}/min 语音{_RATE_VOICE}/min</span></div>
</div>
<div class="card"><h3>📊 请求指标</h3>
<div class="metric"><span>总请求</span><span class="val">{m["total_requests"]}</span></div>
<div class="metric"><span>错误</span><span class="val">{m["total_errors"]}</span></div>
<div class="metric"><span>缓存命中</span><span class="val">{m["cache_hits"]}</span></div>
<div class="metric"><span>平均响应</span><span class="val">{m["avg_response_ms"]}ms</span></div>
<div class="metric"><span>P95响应</span><span class="val">{m["p95_response_ms"]}ms</span></div>
<a href="/api/metrics" style="font-size:12px">详情 →</a>
</div>
</div>
</body></html>"""
