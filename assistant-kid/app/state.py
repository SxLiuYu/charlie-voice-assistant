"""共享运行态: 指标 / 限流桶 / WebSocket&SSE连接 / 请求上限

这些对象在模块加载时创建一次; voice_server 与各 route 模块通过
`from app.state import _ws_clients` 等共享同一对象(原地修改, 不重绑).
"""
import logging
log = logging.getLogger("magic")

MAX_REQUEST_BODY = 15 * 1024 * 1024  # 15MB 请求体上限(含音频)

class Metrics:
    """轻量级请求指标追踪"""
    def __init__(self):
        self.requests: dict = {}
        self.errors = 0
        self.cache_hits = 0
        self.total_requests = 0
        self.response_times: list = []

    def record(self, endpoint: str, duration_ms: float, ok: bool = True):
        self.total_requests += 1
        if not ok:
            self.errors += 1
        if endpoint not in self.requests:
            self.requests[endpoint] = {"count": 0, "total_ms": 0, "errors": 0}
        self.requests[endpoint]["count"] += 1
        self.requests[endpoint]["total_ms"] += duration_ms
        if not ok:
            self.requests[endpoint]["errors"] += 1
        self.response_times.append(duration_ms)
        if len(self.response_times) > 100:
            self.response_times = self.response_times[-100:]

    def cache_hit(self):
        self.cache_hits += 1

    def summary(self) -> dict:
        import statistics
        times = self.response_times
        avg_ms = statistics.mean(times) if times else 0
        p95 = sorted(times)[int(len(times) * 0.95)] if len(times) >= 20 else 0
        endpoints = {}
        for ep, d in self.requests.items():
            endpoints[ep] = {
                "count": d["count"],
                "avg_ms": round(d["total_ms"] / d["count"], 1) if d["count"] else 0,
                "errors": d["errors"],
            }
        return {
            "total_requests": self.total_requests,
            "total_errors": self.errors,
            "cache_hits": self.cache_hits,
            "avg_response_ms": round(avg_ms, 1),
            "p95_response_ms": round(p95, 1),
            "endpoints": endpoints,
        }

_metrics = Metrics()

# ===== 限流(每IP每分钟60普通+10语音, 每会话30) =====
_rate_buckets = {}        # {ip: {"voice":[ts], "general":[ts]}}
_RATE_GENERAL = 60
_RATE_VOICE = 10
_RATE_WINDOW = 60
_RATE_PER_SESSION = 30
_session_buckets = {}      # {session_id: [ts]}

# ===== 实时连接 =====
_sse_clients = []          # SSE客户端队列列表
_ws_clients = {}           # {ws_id: {"ws":ws,"interrupt":False,"last_active":...}}

def _ws_client_count() -> int:
    """当前活跃WebSocket连接数"""
    return len(_ws_clients)
