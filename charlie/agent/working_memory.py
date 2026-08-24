"""agent/working_memory.py - 当前会话的短期记忆（Working Memory）

Working Memory 是会话级别的短期记忆，特点：
- 会话开始时为空
- 会话结束时清空
- 存储本轮对话的关键事实、意图、话题
- 不持久化到磁盘（纯内存）
"""
import threading
import logging
from typing import Optional

log = logging.getLogger("magic")

# 模块级工作记忆（单例）
_working_memory = {
    "session_facts": {},    # 本轮对话中提到的事实 {key: value}
    "intent_stack": [],     # 用户意图栈（用于指代消解）
    "last_topic": "",       # 最后话题
    "last_entity": "",      # 最后提到的实体（人/物/地点）
    "turn_count": 0,        # 本轮对话轮数
    "clarification_count": 0,  # 澄清次数
}
_wm_lock = threading.Lock()


def reset() -> None:
    """清空工作记忆（会话结束时调用）"""
    with _wm_lock:
        _working_memory["session_facts"].clear()
        _working_memory["intent_stack"].clear()
        _working_memory["last_topic"] = ""
        _working_memory["last_entity"] = ""
        _working_memory["turn_count"] = 0
        _working_memory["clarification_count"] = 0


def get() -> dict:
    """获取工作记忆快照"""
    with _wm_lock:
        return {
            "session_facts": dict(_working_memory["session_facts"]),
            "intent_stack": list(_working_memory["intent_stack"]),
            "last_topic": _working_memory["last_topic"],
            "last_entity": _working_memory["last_entity"],
            "turn_count": _working_memory["turn_count"],
            "clarification_count": _working_memory["clarification_count"],
        }


def update(
    facts: Optional[dict] = None,
    intent: str = "",
    topic: str = "",
    entity: str = "",
    increment_turn: bool = True,
) -> None:
    """更新工作记忆"""
    with _wm_lock:
        if facts:
            _working_memory["session_facts"].update(facts)
        if intent and intent != "none" and (not _working_memory["intent_stack"] or _working_memory["intent_stack"][-1] != intent):
            _working_memory["intent_stack"].append(intent)
        if topic:
            _working_memory["last_topic"] = topic
        if entity:
            _working_memory["last_entity"] = entity
        if increment_turn:
            _working_memory["turn_count"] += 1


# 别名：兼容外部调用习惯（如 magic-memory.py 中的 update_working_memory）
update_working_memory = update


def add_fact(key: str, value: str) -> None:
    """添加会话事实"""
    update(facts={key: value})


def get_fact(key: str, default: str = "") -> str:
    """获取会话事实"""
    with _wm_lock:
        return _working_memory["session_facts"].get(key, default)


def pop_intent() -> str:
    """弹出最后意图（用于指代消解后清理）"""
    with _wm_lock:
        if _working_memory["intent_stack"]:
            return _working_memory["intent_stack"].pop()
        return ""


def current_topic() -> str:
    """获取当前话题"""
    with _wm_lock:
        return _working_memory["last_topic"]


def last_entity() -> str:
    """获取最后提到的实体"""
    with _wm_lock:
        return _working_memory["last_entity"]


def turn_count() -> int:
    """获取本轮对话轮数"""
    with _wm_lock:
        return _working_memory["turn_count"]


def increment_clarification() -> int:
    """增加澄清次数，返回当前值"""
    with _wm_lock:
        _working_memory["clarification_count"] += 1
        return _working_memory["clarification_count"]


def get_all() -> dict:
    """获取工作记忆完整快照（深拷贝 session_facts 和 intent_stack）。

    用于跨 session 隔离：在 brain_stream_sentences 入口保存，
    finally 块中 restore 恢复，避免 A 用户的工作记忆被 B 覆盖。
    """
    with _wm_lock:
        return {
            "session_facts": dict(_working_memory["session_facts"]),
            "intent_stack": list(_working_memory["intent_stack"]),
            "last_topic": _working_memory["last_topic"],
            "last_entity": _working_memory["last_entity"],
            "turn_count": _working_memory["turn_count"],
            "clarification_count": _working_memory["clarification_count"],
        }


def restore(data: dict) -> None:
    """用快照数据恢复工作记忆（原子替换内部状态）。

    参数:
      data: get_all() 或 get() 返回的 dict
    """
    if not data:
        return
    with _wm_lock:
        _working_memory["session_facts"] = dict(data.get("session_facts", {}))
        _working_memory["intent_stack"] = list(data.get("intent_stack", []))
        _working_memory["last_topic"] = data.get("last_topic", "")
        _working_memory["last_entity"] = data.get("last_entity", "")
        _working_memory["turn_count"] = data.get("turn_count", 0)
        _working_memory["clarification_count"] = data.get("clarification_count", 0)
