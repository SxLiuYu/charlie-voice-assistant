"""响应缓存: LRU + TTL"""
import time
import threading
from typing import Optional
from collections import OrderedDict

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_TTL = int(__import__('os').getenv("LLM_CACHE_TTL", "300"))
_CACHE_MAX = int(__import__('os').getenv("LLM_CACHE_MAX", "500"))

def _cache_get(text: str) -> Optional[str]:
    key = f"text\x00{text.strip().lower()}"
    with _cache_lock:
        if key in _cache:
            reply, ts = _cache[key]
            if time.time() - ts < _CACHE_TTL:
                _cache.move_to_end(key)
                return reply
            del _cache[key]
    return None

def _cache_get_interrupted(text: str, interrupted_reply: str) -> Optional[str]:
    key = f"interrupted\x00{interrupted_reply.strip()[:200].lower()}\x00{text.strip().lower()}"
    with _cache_lock:
        if key in _cache:
            reply, ts = _cache[key]
            if time.time() - ts < _CACHE_TTL:
                _cache.move_to_end(key)
                return reply
            del _cache[key]
    return None

def _cache_set(text: str, reply: str, interrupted_reply: str = "") -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.popitem(last=False)
        if interrupted_reply.strip():
            key = f"interrupted\x00{interrupted_reply.strip()[:200].lower()}\x00{text.strip().lower()}"
        else:
            key = f"text\x00{text.strip().lower()}"
        _cache[key] = (reply, time.time())
        _cache.move_to_end(key)