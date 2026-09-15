"""agent/working_memory.py - 当前会话的短期记忆（Working Memory）

Working Memory 是会话级别的短期记忆，特点：
- 会话开始时为空
- 会话结束时清空
- 存储本轮对话的关键事实、意图、话题
- 不持久化到磁盘（纯内存）

线程安全设计：
- 每个会话有独立的 WM 数据 dict（_session_data 按 session_id 隔离）
- 每个线程通过 thread-local 持有自己的 active session_id
- 跨通道（飞书 + ESP32）并发 brain 调用时各自操作独立会话槽，互不串台
"""
import threading
import logging
from typing import Optional

log = logging.getLogger("magic")

_wm_lock = threading.Lock()


def _new_wm() -> dict:
    """创建空白工作记忆槽。"""
    return {
        "session_facts": {},    # 本轮对话中提到的事实 {key: value}
        "intent_stack": [],     # 用户意图栈（用于指代消解）
        "last_topic": "",       # 最后话题
        "last_entity": "",      # 最后提到的实体（人/物/地点）
        "turn_count": 0,        # 本轮对话轮数
        "clarification_count": 0,  # 澄清次数
    }


# 按 session_id 隔离的 WM 数据（替代旧的单槽 _working_memory + _session_store）
_session_data: dict[str, dict] = {}
_SESSION_STORE_MAX = 16

# 每线程的 active session_id（thread-local，消除跨通道竞态）
_session_local = threading.local()


def _get_session() -> str:
    """返回当前线程的 active session_id。"""
    return getattr(_session_local, "session", "default")


def _ensure_session(session_id: str) -> dict:
    """获取或创建指定会话的 WM dict（调用方须持有 _wm_lock）。"""
    if session_id not in _session_data:
        _session_data[session_id] = _new_wm()
        while len(_session_data) > _SESSION_STORE_MAX:
            _session_data.pop(next(iter(_session_data)))
    return _session_data[session_id]


def reset() -> None:
    """清空当前线程会话的工作记忆。"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        wm["session_facts"].clear()
        wm["intent_stack"].clear()
        wm["last_topic"] = ""
        wm["last_entity"] = ""
        wm["turn_count"] = 0
        wm["clarification_count"] = 0


def get() -> dict:
    """获取当前线程会话的工作记忆快照。"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return {
            "session_facts": dict(wm["session_facts"]),
            "intent_stack": list(wm["intent_stack"]),
            "last_topic": wm["last_topic"],
            "last_entity": wm["last_entity"],
            "turn_count": wm["turn_count"],
            "clarification_count": wm["clarification_count"],
        }


def update(
    facts: Optional[dict] = None,
    intent: str = "",
    topic: str = "",
    entity: str = "",
    increment_turn: bool = True,
) -> None:
    """更新当前线程会话的工作记忆。"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        if facts:
            wm["session_facts"].update(facts)
        if intent and intent != "none" and (not wm["intent_stack"] or wm["intent_stack"][-1] != intent):
            wm["intent_stack"].append(intent)
        if topic:
            wm["last_topic"] = topic
        if entity:
            wm["last_entity"] = entity
        if increment_turn:
            wm["turn_count"] += 1


# 别名：兼容外部调用习惯（如 magic-memory.py 中的 update_working_memory）
update_working_memory = update


def add_fact(key: str, value: str) -> None:
    """添加会话事实"""
    update(facts={key: value})


def get_fact(key: str, default: str = "") -> str:
    """获取会话事实"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return wm["session_facts"].get(key, default)


def pop_intent() -> str:
    """弹出最后意图（用于指代消解后清理）"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        if wm["intent_stack"]:
            return wm["intent_stack"].pop()
        return ""


def current_topic() -> str:
    """获取当前话题"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return wm["last_topic"]


def last_entity() -> str:
    """获取最后提到的实体"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return wm["last_entity"]


def turn_count() -> int:
    """获取本轮对话轮数"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return wm["turn_count"]


def increment_clarification() -> int:
    """增加澄清次数，返回当前值"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        wm["clarification_count"] += 1
        return wm["clarification_count"]


def get_all() -> dict:
    """获取当前线程会话的工作记忆完整快照。"""
    with _wm_lock:
        wm = _ensure_session(_get_session())
        return {
            "session_facts": dict(wm["session_facts"]),
            "intent_stack": list(wm["intent_stack"]),
            "last_topic": wm["last_topic"],
            "last_entity": wm["last_entity"],
            "turn_count": wm["turn_count"],
            "clarification_count": wm["clarification_count"],
        }


def switch_session(session_id: str) -> None:
    """切换当前线程的 active session。

    thread-local 设计：每个线程独立维护自己的 active session，
    跨通道并发 brain 调用时各自操作独立会话槽，互不串台。
    """
    if not session_id:
        return
    _session_local.session = session_id
    with _wm_lock:
        _ensure_session(session_id)


def restore(data: dict) -> None:
    """用快照数据恢复当前线程会话的工作记忆（原子替换）。"""
    if not data:
        return
    with _wm_lock:
        wm = _ensure_session(_get_session())
        wm["session_facts"] = dict(data.get("session_facts", {}))
        wm["intent_stack"] = list(data.get("intent_stack", []))
        wm["last_topic"] = data.get("last_topic", "")
        wm["last_entity"] = data.get("last_entity", "")
        wm["turn_count"] = data.get("turn_count", 0)
        wm["clarification_count"] = data.get("clarification_count", 0)
