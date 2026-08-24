"""共享运行态: 指标 / 限流桶 / WebSocket&SSE连接 / 请求上限

这些对象在模块加载时创建一次; voice_server 与各 route 模块通过
`from app.state import _ws_clients` 等共享同一对象(原地修改, 不重绑).
"""
import asyncio
import logging
from collections import deque
import threading
import time
log = logging.getLogger("magic")

MAX_REQUEST_BODY = 15 * 1024 * 1024  # 15MB 请求体上限(含音频)

class Metrics:
    """轻量级请求指标追踪"""
    def __init__(self):
        self.requests: dict = {}
        self.errors = 0
        self.cache_hits = 0
        self.conditional_requests = 0
        self.not_modified = 0
        self.total_requests = 0
        self.revision = 0
        self.response_times = deque(maxlen=100)
        self._latency_stats: tuple[float, float] | None = None

    def record(self, endpoint: str, duration_ms: float, ok: bool = True,
               conditional: bool = False, not_modified: bool = False,
               include_latency: bool = True, include_in_metrics: bool = True):
        self.total_requests += 1
        if include_in_metrics:
            self.revision += 1
        if conditional:
            self.conditional_requests += 1
        if not_modified:
            self.not_modified += 1
            self.cache_hits = self.not_modified
        if not ok:
            self.errors += 1
        if endpoint not in self.requests:
            self.requests[endpoint] = {
                "count": 0,
                "total_ms": 0,
                "errors": 0,
                "conditional": 0,
                "not_modified": 0,
            }
        self.requests[endpoint]["count"] += 1
        self.requests[endpoint]["total_ms"] += duration_ms
        if conditional:
            self.requests[endpoint]["conditional"] += 1
        if not_modified:
            self.requests[endpoint]["not_modified"] += 1
        if not ok:
            self.requests[endpoint]["errors"] += 1
        if include_latency:
            self.response_times.append(duration_ms)
            self._latency_stats = None

    def cache_hit(self):
        self.cache_hits += 1

    def token(self, exclude_endpoint: str | None = None) -> str:
        """Opaque cache token for metrics payloads without building them."""
        total_requests = self.total_requests
        errors = self.errors
        conditional_requests = self.conditional_requests
        not_modified = self.not_modified
        if exclude_endpoint is not None and exclude_endpoint in self.requests:
            d = self.requests[exclude_endpoint]
            total_requests -= d["count"]
            errors -= d["errors"]
            conditional_requests -= d["conditional"]
            not_modified -= d["not_modified"]
        parts = [self.revision, total_requests, errors, conditional_requests,
                 not_modified, self.cache_hits]
        return "metrics:" + ":".join(str(part) for part in parts)

    def summary(
        self,
        exclude_endpoint: str | None = None,
        include_endpoints: bool = True,
    ) -> dict:
        times = self.response_times
        if self._latency_stats is None and times:
            sorted_times = sorted(times)
            avg_ms = sum(times) / len(times)
            p95 = sorted_times[int(len(times) * 0.95)] if len(times) >= 20 else 0
            self._latency_stats = (avg_ms, p95)
        elif self._latency_stats is not None:
            avg_ms, p95 = self._latency_stats
        else:
            avg_ms = 0
            p95 = 0
        total_requests = self.total_requests
        total_errors = self.errors
        conditional_requests = self.conditional_requests
        not_modified = self.not_modified
        endpoints = {} if include_endpoints else None
        for ep, d in self.requests.items():
            if ep == exclude_endpoint:
                total_requests -= d["count"]
                total_errors -= d["errors"]
                conditional_requests -= d["conditional"]
                not_modified -= d["not_modified"]
                continue
            if include_endpoints:
                endpoints[ep] = {
                    "count": d["count"],
                    "avg_ms": round(d["total_ms"] / d["count"], 1) if d["count"] else 0,
                    "errors": d["errors"],
                    "conditional": d["conditional"],
                    "not_modified": d["not_modified"],
                    "not_modified_rate": round(d["not_modified"] * 100 / d["conditional"], 1) if d["conditional"] else 0,
                }
        result = {
            "total_requests": total_requests,
            "total_errors": total_errors,
            "cache_hits": not_modified,
            "conditional_requests": conditional_requests,
            "not_modified": not_modified,
            "not_modified_rate": round(not_modified * 100 / conditional_requests, 1) if conditional_requests else 0,
            "avg_response_ms": round(avg_ms, 1),
            "p95_response_ms": round(p95, 1),
        }
        if include_endpoints:
            result["endpoints"] = endpoints
        return result

_metrics = Metrics()

# ===== 前端轮询可观测性(页面隐藏暂停/失败退避) =====
_POLL_TELEMETRY_EVENTS = ("paused", "resumed", "backoff", "errors")
_POLL_TELEMETRY_EVENT_ALIASES = {
    "paused": "paused",
    "resumed": "resumed",
    "backoff": "backoff",
    "error": "errors",
    "errors": "errors",
}
_POLL_TELEMETRY_JOBS = ("reminders", "preferences", "tunnel")


class PollTelemetry:
    """记录浏览器端轮询暂停、恢复和失败退避，供状态页排查降载是否生效。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.totals = {event: 0 for event in _POLL_TELEMETRY_EVENTS}
            self.jobs = {
                job: {event: 0 for event in _POLL_TELEMETRY_EVENTS if event != "resumed"}
                for job in _POLL_TELEMETRY_JOBS
            }
            self.last_event = None

    def record(self, event: str, job: str | None = None):
        normalized = _POLL_TELEMETRY_EVENT_ALIASES.get(event, event)
        if normalized not in _POLL_TELEMETRY_EVENTS:
            raise ValueError(f"unknown polling telemetry event: {event}")
        if job is not None and job not in _POLL_TELEMETRY_JOBS:
            raise ValueError(f"unknown polling telemetry job: {job}")
        if normalized != "resumed" and not job:
            raise ValueError("polling telemetry job is required")
        if normalized == "resumed":
            job = None
        with self._lock:
            self.totals[normalized] += 1
            if job:
                self.jobs[job][normalized] += 1
            self.last_event = {
                "event": normalized,
                "job": job,
                "at": time.time(),
            }

    def record_failure(self, job: str):
        """Record a failed poll and the resulting backoff scheduling together."""
        if job not in _POLL_TELEMETRY_JOBS:
            raise ValueError(f"unknown polling telemetry job: {job}")
        with self._lock:
            self.totals["errors"] += 1
            self.totals["backoff"] += 1
            self.jobs[job]["errors"] += 1
            self.jobs[job]["backoff"] += 1
            self.last_event = {
                "event": "errors",
                "job": job,
                "at": time.time(),
            }

    def summary(self) -> dict:
        with self._lock:
            return {
                "totals": dict(self.totals),
                "jobs": {
                    job: dict(events) for job, events in self.jobs.items()
                },
                "last_event": dict(self.last_event) if self.last_event else None,
            }


_poll_telemetry = PollTelemetry()

# ===== WebSocket 打断上下文可观测性 =====
_MAX_INTERRUPT_REPLY_CHARS = 200


class InterruptTelemetry:
    """记录最近一次 WebSocket 打断和被截断的被打断回复。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.total = 0
            self.with_reply = 0
            self.last_reply = ""
            self.last_ws_id = None
            self.last_at = None
            self.last_follow_up = None
            self._pending_replies = {}

    def record(self, ws_id: int, interrupted_reply: str | None):
        reply = (interrupted_reply or "").strip()[:_MAX_INTERRUPT_REPLY_CHARS]
        with self._lock:
            self.total += 1
            if reply:
                self.with_reply += 1
                self.last_reply = reply
                self._pending_replies[ws_id] = reply
            else:
                self._pending_replies.pop(ws_id, None)
            self.last_ws_id = ws_id
            self.last_at = time.time()

    def record_follow_up(self, ws_id: int, text: str | None, source: str) -> str:
        follow_up = (text or "").strip()[:_MAX_INTERRUPT_REPLY_CHARS]
        if not follow_up:
            return ""
        with self._lock:
            interrupted_reply = self._pending_replies.pop(ws_id, None)
            if not interrupted_reply:
                return ""
            self.last_follow_up = {
                "text": follow_up,
                "source": source,
                "interrupted_reply": interrupted_reply,
                "ws_id": ws_id,
                "at": time.time(),
            }
            return interrupted_reply

    def discard_pending(self, ws_id: int):
        with self._lock:
            self._pending_replies.pop(ws_id, None)

    def summary(self) -> dict:
        with self._lock:
            return {
                "total": self.total,
                "with_reply": self.with_reply,
                "last_reply": self.last_reply,
                "last_ws_id": self.last_ws_id,
                "last_at": self.last_at,
                "last_follow_up": dict(self.last_follow_up) if self.last_follow_up else None,
                "max_reply_chars": _MAX_INTERRUPT_REPLY_CHARS,
            }


_interrupt_telemetry = InterruptTelemetry()

# ===== 限流(每IP每分钟60普通+10语音, 每会话30) =====
_rate_buckets = {}        # {ip: {"voice":[ts], "general":[ts]}}
_RATE_GENERAL = 60
_RATE_VOICE = 10
_RATE_WINDOW = 60
_RATE_PER_SESSION = 30
_RATE_LOCK = threading.Lock()  # 限流计数器锁
_session_buckets = {}      # {session_id: [ts]}

# ===== 实时连接 =====
_sse_clients = []          # SSE客户端队列列表
_ws_clients = {}           # {ws_id: {"ws":ws,"interrupt":False,"last_active":...}}
_ws_session_groups = {}    # {session_id: [ws_id, ...]} — 跨终端会话组
_ws_client_locations = {}  # {ws_id: {"lat":...,"lng":...,"accuracy":...,"time":...}} — 客户端位置
_sse_clients_lock = threading.Lock()
_ws_clients_lock = threading.RLock()

# ===== xiaozhi ESP32 WebSocket 连接（主动推送用） =====
_xiaozhi_clients = {}       # {client_id: {"ws": ws, "loop": event_loop}}
_xiaozhi_lock = threading.Lock()

# 待推送队列：ESP32未连接时暂存，连接时flush
_xiaozhi_pending: list[dict] = []  # [{"text": str, "mp3": bytes, "ts": float}]
_PENDING_TTL = 300  # 5分钟内的待推项才补发（给MQTT wake + ESP32建连留余量）


def register_xiaozhi_client(client_id: str, ws, loop) -> None:
    """注册 xiaozhi ESP32 WebSocket 连接，并 flush 待推送队列"""
    with _xiaozhi_lock:
        _xiaozhi_clients[client_id] = {"ws": ws, "loop": loop}
        pending = list(_xiaozhi_pending)
        _xiaozhi_pending.clear()
    log.info(f"[xiaozhi] 客户端已注册: {client_id} (总计: {len(_xiaozhi_clients)}, 待推送: {len(pending)})")
    # 返回 pending 给调用方 flush（避免在锁内做异步操作）
    return pending


def enqueue_xiaozhi_pending(text: str, mp3: bytes) -> int:
    """ESP32未连接时，将推送暂存队列。返回队列长度。"""
    import time
    with _xiaozhi_lock:
        now = time.time()
        # 清理过期项
        _xiaozhi_pending[:] = [p for p in _xiaozhi_pending if now - p["ts"] < _PENDING_TTL]
        _xiaozhi_pending.append({"text": text, "mp3": mp3, "ts": now})
        return len(_xiaozhi_pending)


def unregister_xiaozhi_client(client_id: str) -> None:
    """注销 xiaozhi ESP32 WebSocket 连接"""
    with _xiaozhi_lock:
        _xiaozhi_clients.pop(client_id, None)
    log.info(f"[xiaozhi] 客户端已注销: {client_id}")


def snapshot_xiaozhi_clients() -> dict:
    """获取当前连接的 xiaozhi 客户端快照"""
    with _xiaozhi_lock:
        return dict(_xiaozhi_clients)


def xiaozhi_client_count() -> int:
    """当前连接的 xiaozhi 客户端数量"""
    with _xiaozhi_lock:
        return len(_xiaozhi_clients)


def register_sse_client(client_q: asyncio.Queue) -> None:
    with _sse_clients_lock:
        if client_q not in _sse_clients:
            _sse_clients.append(client_q)


def unregister_sse_client(client_q: asyncio.Queue) -> None:
    with _sse_clients_lock:
        try:
            _sse_clients.remove(client_q)
        except ValueError:
            pass


def snapshot_sse_clients() -> list[asyncio.Queue]:
    with _sse_clients_lock:
        return list(_sse_clients)


def sse_client_count() -> int:
    with _sse_clients_lock:
        return len(_sse_clients)


# ===== 连续对话模式 (Continuous Conversation Mode) =====
# 来自 gitee assistant-x-openclaw 的「连续对话与打断机制」思路。
# 会话级状态：上次活跃时间 + 连续模式过期时间。
# 当用户在某会话内连续互动时，无需重新唤醒即可继续对话。

_CONTINUOUS_MODE_TTL = 8  # 连续对话保持秒数（每轮交互后重置）
_continuous_mode_lock = threading.Lock()
_continuous_mode: dict[str, float] = {}  # session_id -> expires_at


def enter_continuous_mode(session_id: str) -> None:
    """进入/续期连续对话模式"""
    expires = time.time() + _CONTINUOUS_MODE_TTL
    with _continuous_mode_lock:
        _continuous_mode[session_id] = expires


def exit_continuous_mode(session_id: str) -> None:
    """退出连续对话模式"""
    with _continuous_mode_lock:
        _continuous_mode.pop(session_id, None)


def is_continuous_mode(session_id: str) -> bool:
    """检查会话是否处于连续对话模式"""
    expires = _continuous_mode.get(session_id)
    if expires is None:
        return False
    if time.time() > expires:
        # 过期自动清理
        with _continuous_mode_lock:
            _continuous_mode.pop(session_id, None)
        return False
    return True


def refresh_continuous_mode(session_id: str) -> bool:
    """刷新连续对话模式，返回是否处于连续模式"""
    if is_continuous_mode(session_id):
        enter_continuous_mode(session_id)
        return True
    return False


def _ws_client_count() -> int:
    """当前活跃WebSocket连接数"""
    return len(_ws_clients)
