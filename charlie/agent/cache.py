"""响应缓存: LRU + TTL

key 维度 = 命名空间 + session_id + 角色 + 文本。
语音对话大量短指代句（"明天呢"/"那上海呢"）高度依赖会话上下文，
角色切换也会改变人设语气——key 缺少任一维度都会跨会话/角色串台。
"""
import os
import time
import threading
from typing import Optional
from collections import OrderedDict

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", "300"))
_CACHE_MAX = int(os.getenv("LLM_CACHE_MAX", "500"))


def _cache_key(namespace: str, text: str, session_id: str,
               interrupted_reply: str = "") -> str:
    try:
        from agent.roles import get_current_role
        role = get_current_role() or ""
    except Exception:
        role = ""
    return (f"{namespace}\x00{session_id}\x00{role}\x00"
            f"{interrupted_reply.strip()[:200].lower()}\x00{text.strip().lower()}")


def _cache_get(text: str, session_id: str = "default") -> Optional[str]:
    key = _cache_key("text", text, session_id)
    with _cache_lock:
        if key in _cache:
            reply, ts = _cache[key]
            if time.time() - ts < _CACHE_TTL:
                _cache.move_to_end(key)
                return reply
            del _cache[key]
    return None

def _cache_get_interrupted(text: str, interrupted_reply: str,
                           session_id: str = "default") -> Optional[str]:
    key = _cache_key("interrupted", text, session_id, interrupted_reply)
    with _cache_lock:
        if key in _cache:
            reply, ts = _cache[key]
            if time.time() - ts < _CACHE_TTL:
                _cache.move_to_end(key)
                return reply
            del _cache[key]
    return None

def _cache_set(text: str, reply: str, interrupted_reply: str = "",
               session_id: str = "default") -> None:
    namespace = "interrupted" if interrupted_reply.strip() else "text"
    key = _cache_key(namespace, text, session_id, interrupted_reply)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.popitem(last=False)
        _cache[key] = (reply, time.time())
        _cache.move_to_end(key)
