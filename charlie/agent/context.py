"""agent/context.py - 多模态上下文融合

聚合：时间 + 位置 + 天气 + 设备状态 + 最近对话 + 用户状态 + 待办事项
将上下文格式化为系统 prompt 片段。
"""
import os
import datetime
import logging
import time
import threading
from typing import Any, Optional

log = logging.getLogger("magic")

# ===== 上下文缓存 =====
_context_cache: dict[str, Any] = {}
_context_cache_ts: float = 0.0
_context_cache_lock = threading.Lock()
_context_ttl = 60  # 秒
_context_refresh_thread: Optional[threading.Thread] = None
_context_refresh_lock = threading.Lock()
_context_fail_count = 0          # 连续刷新失败次数
_context_backoff_until = 0.0     # 退避截止时间戳
_CONTEXT_FAIL_BACKOFF = 300      # 连续失败3次后，5分钟内不再尝试刷新


def _refresh_context_cache() -> None:
    """后台线程：刷新上下文缓存（含天气网络请求）。"""
    global _context_cache, _context_cache_ts
    global _context_fail_count, _context_backoff_until
    try:
        ctx: dict[str, Any] = {}
        # 1. 时间
        try:
            now = datetime.datetime.now()
            ctx["time"] = {
                "now": now.strftime("%Y-%m-%d %H:%M"),
                "hour": now.hour,
                "weekday": now.weekday(),
                "period": _period_of_day(now.hour),
                "is_weekend": now.weekday() >= 5,
            }
        except Exception:
            ctx["time"] = {}

        # 2. 位置
        try:
            from app.geo import locate_text
            loc = locate_text()
            ctx["location"] = loc if loc else ""
        except Exception:
            ctx["location"] = ""

        # 3. 天气（可能阻塞，放在后台线程中）
        try:
            from agent.weather import direct_weather_play
            weather = direct_weather_play("")
            ctx["weather"] = weather if weather else ""
        except Exception:
            ctx["weather"] = ""

        # 4. 设备状态
        try:
            from agent.device_control import direct_ac_control
            ctx["device"] = {"ac": "unknown"}
        except Exception:
            ctx["device"] = {}

        # 5. 最近对话摘要
        try:
            from agent.history import _get_history
            hist = _get_history()
            recent = [m.get("content", "") for m in hist[-6:] if isinstance(m, dict)]
            ctx["recent_conversation"] = recent
        except Exception:
            ctx["recent_conversation"] = []

        # 6. 用户状态
        try:
            from agent.state import get_user_state
            ctx["user_state"] = get_user_state()
        except Exception:
            ctx["user_state"] = {}

        # 7. 待办事项（通过 app.reminders 加载，复用锁和畸形过滤）
        try:
            from app.reminders import _load_reminders
            now = datetime.datetime.now()
            pending = []
            for r in _load_reminders():
                if isinstance(r, dict) and not r.get("done"):
                    due = str(r.get("due", ""))[:10]
                    if due == now.strftime("%Y-%m-%d"):
                        pending.append(str(r.get("text", ""))[:40])
            ctx["todos"] = pending
        except Exception:
            ctx["todos"] = []

        with _context_cache_lock:
            _context_cache = ctx
            _context_cache_ts = time.time()
        _context_fail_count = 0
        _context_backoff_until = 0.0
    except Exception:
        # 刷新失败退避：连续失败3次后 5 分钟内不再发起网络刷新，
        # 避免天气 API 持续不可用时每次构建 system msg 都空转线程
        _context_fail_count += 1
        if _context_fail_count >= 3:
            _context_backoff_until = time.time() + _CONTEXT_FAIL_BACKOFF
            log.warning("[context] 刷新连续失败%d次, 退避%d秒", _context_fail_count, _CONTEXT_FAIL_BACKOFF)


def get_context() -> dict[str, Any]:
    """聚合所有上下文信息，返回结构化字典。

    每个字段失败时返回空值/默认值，不影响其他字段。
    天气查询在缓存过期时由后台线程异步刷新，当前请求返回不含天气的上下文。
    """
    global _context_cache, _context_cache_ts, _context_refresh_thread
    global _context_backoff_until
    # TTL 缓存：60 秒内重复调用直接返回，避免每次构建 system message 都发网络请求
    with _context_cache_lock:
        if _context_cache and time.time() - _context_cache_ts < _context_ttl:
            return dict(_context_cache)
        # stale-while-revalidate: 缓存过期但有旧值时，后台刷新并返回旧值
        stale_ctx = dict(_context_cache) if _context_cache else None
    # 后台线程刷新完整缓存（含天气）；退避期内跳过（天气 API 持续不可用时避免空转）
    if time.time() >= _context_backoff_until:
        with _context_refresh_lock:
            if _context_refresh_thread is None or not _context_refresh_thread.is_alive():
                _context_refresh_thread = threading.Thread(
                    target=_refresh_context_cache, daemon=True
                )
                _context_refresh_thread.start()
    # 有旧缓存则返回旧值（stale-while-revalidate），无缓存才构建不含天气的临时上下文
    if stale_ctx:
        return stale_ctx
    ctx: dict[str, Any] = {}
    # 1. 时间
    try:
        now = datetime.datetime.now()
        ctx["time"] = {
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "hour": now.hour,
            "weekday": now.weekday(),
            "period": _period_of_day(now.hour),
            "is_weekend": now.weekday() >= 5,
        }
    except Exception:
        ctx["time"] = {}
    # 2. 位置：用缓存值（后台线程刷新），不阻塞首音频关键路径
    with _context_cache_lock:
        ctx["location"] = _context_cache.get("location", "") if _context_cache else ""
    # 3. 天气（跳过，由后台线程刷新）
    ctx["weather"] = ""
    # 4. 设备状态
    try:
        from agent.device_control import direct_ac_control
        ctx["device"] = {"ac": "unknown"}
    except Exception:
        ctx["device"] = {}
    # 5. 最近对话摘要
    try:
        from agent.history import _get_history
        hist = _get_history()
        recent = [m.get("content", "") for m in hist[-6:] if isinstance(m, dict)]
        ctx["recent_conversation"] = recent
    except Exception:
        ctx["recent_conversation"] = []
    # 6. 用户状态
    try:
        from agent.state import get_user_state
        ctx["user_state"] = get_user_state()
    except Exception:
        ctx["user_state"] = {}
    # 7. 待办事项（通过 app.reminders 加载，复用锁和畸形过滤）
    try:
        from app.reminders import _load_reminders
        now = datetime.datetime.now()
        pending = []
        for r in _load_reminders():
            if isinstance(r, dict) and not r.get("done"):
                due = str(r.get("due", ""))[:10]
                if due == now.strftime("%Y-%m-%d"):
                    pending.append(str(r.get("text", ""))[:40])
        ctx["todos"] = pending
    except Exception:
        ctx["todos"] = []
    return ctx


def invalidate_context_cache() -> None:
    """清除上下文缓存，用于测试或系统刷新。"""
    global _context_cache, _context_cache_ts
    with _context_cache_lock:
        _context_cache = {}
        _context_cache_ts = 0.0


def format_context_for_prompt(ctx: Optional[dict[str, Any]] = None) -> str:
    """将上下文格式化为系统 prompt 片段。"""
    if ctx is None:
        ctx = get_context()
    parts: list[str] = []

    # 时间
    time_info = ctx.get("time")
    if time_info:
        parts.append(
            f"当前时间：{time_info.get('now', '')}（{time_info.get('period', '')}）"
        )

    # 位置
    location = ctx.get("location")
    if location:
        parts.append(f"当前位置：{location}")

    # 天气
    weather = ctx.get("weather")
    if weather:
        parts.append(f"天气：{weather}")

    # 用户状态
    user_state = ctx.get("user_state", {})
    state = user_state.get("state", "")
    if state:
        state_map = {
            "home_awake": "在家活跃",
            "home_resting": "在家休息",
            "home_sleeping": "在家休息/睡眠",
            "working": "工作中",
            "away": "外出",
        }
        label = state_map.get(state, state)
        parts.append(f"用户状态：{label}（置信度{user_state.get('confidence', 0):.0%}）")

    # 待办
    todos = ctx.get("todos", [])
    if todos:
        parts.append("今日待办：" + "、".join(todos[:5]))

    # 最近对话（精简）
    recent = ctx.get("recent_conversation", [])
    if recent:
        last = "；".join(recent[-2:])
        parts.append(f"最近对话：{last}")

    return "\n".join(parts) if parts else ""


def _period_of_day(hour: int) -> str:
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "深夜"
