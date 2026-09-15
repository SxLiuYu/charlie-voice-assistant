"""对话历史: 多会话持久化"""
import os
import json
import tempfile
import threading
import time
try:
    import fcntl
except ImportError:  # Windows 无 fcntl
    import fcntl_compat as fcntl
import logging
from contextlib import contextmanager
from typing import Optional

log = logging.getLogger("magic")

_history = []
_sessions = {"default": _history}
MAX_HISTORY = 30
MAX_SESSIONS = 10
_history_lock = threading.Lock()

def _data_dir() -> str:
    return os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _get_history_file():
    uid = os.environ.get("CHARLIE_USER_ID", "default")
    if uid == "default":
        return os.path.join(_data_dir(), "conversation_history.json")
    return os.path.join(_data_dir(), f"conversation_history_{uid}.json")

def _get_history_lock_file():
    return _get_history_file() + ".lock"

# 向后兼容导出（供 voice_agent.py 等 import）
HISTORY_FILE = _get_history_file()
HISTORY_LOCK_FILE = HISTORY_FILE + ".lock"

REMINDERS_FILE = os.path.join(_data_dir(), "reminders.json")
# 签名缓存按 uid 分键，防止切换用户后误判 sig
_history_file_signatures: dict[str, Optional[tuple]] = {}
_history_file_caches: dict[str, dict] = {}
_history_file_lock = threading.Lock()
_history_save_seq = 0
_history_append_count = 0  # 每 N 次 _append_history 后自动落盘，防止崩溃丢失
HISTORY_SAVE_EVERY = 2     # 每 2 轮对话（4 条消息）落盘一次，减少崩溃丢失
_context_summaries = {}  # key = session_id

def _history_file_sig() -> Optional[tuple]:
    path = _get_history_file()
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

@contextmanager
def _locked_history_file(shared: bool = False):
    lock_path = _get_history_lock_file()
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

def _read_history_file_locked() -> Optional[dict]:
    uid = os.environ.get("CHARLIE_USER_ID", "default")
    sig = _history_file_sig()
    if sig is None:
        _history_file_signatures.pop(uid, None)
        _history_file_caches.pop(uid, None)
        return None
    if sig == _history_file_signatures.get(uid) and uid in _history_file_caches:
        return _history_file_caches[uid]
    path = _get_history_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        _history_file_signatures.pop(uid, None)
        _history_file_caches.pop(uid, None)
        return None
    if isinstance(data, dict):
        parsed = data
    elif isinstance(data, list):
        parsed = {"default": data}
    else:
        parsed = {}
    normalized = {}
    for session_id, messages in parsed.items():
        if isinstance(messages, list):
            normalized[session_id] = messages
    _history_file_signatures[uid] = sig
    _history_file_caches[uid] = normalized
    return normalized

def _read_history_file() -> Optional[dict]:
    with _history_file_lock, _locked_history_file(shared=True):
        return _read_history_file_locked()

def _evict_overflow_sessions_locked() -> None:
    """维持会话数硬上限（须持有 _history_lock）。

    用 while 收敛：其它创建路径（如 reset_history 直建）可能已让字典
    超过 MAX_SESSIONS，逐个删除最旧的非 default 会话直到达标。
    """
    while len(_sessions) > MAX_SESSIONS:
        for k in list(_sessions.keys()):
            if k != "default":
                del _sessions[k]
                break
        else:
            break

def _get_history(session_id: str = "default") -> list:
    with _history_lock:
        if session_id not in _sessions:
            _sessions[session_id] = []
            _evict_overflow_sessions_locked()
        return _sessions[session_id]

def _history_snapshot(session_id: str = "default") -> list:
    hist = _get_history(session_id)
    with _history_lock:
        return [dict(m) for m in hist]

def _searchable_history(session_id: str = "default") -> list:
    hist = _get_history(session_id)
    with _history_lock:
        mem_copy = [dict(m) for m in hist]
    if mem_copy:
        return mem_copy
    try:
        file_data = _read_history_file()
    except Exception:
        file_data = None
    if file_data:
        disk_msgs = file_data.get(session_id, [])
        if isinstance(disk_msgs, list) and disk_msgs:
            return disk_msgs
    return []

def get_recent_user_message(session_id: str = "default") -> str:
    """返回指定会话中最后一条用户消息, 无则返回空字符串"""
    try:
        hist = _get_history(session_id)
        with _history_lock:
            for msg in reversed(hist):
                if msg.get("role") == "user":
                    return str(msg.get("content", ""))
    except Exception as e:
        log.debug(f"[agent.history] error at line 132: {e}")
        pass
    return ""


def _session_summaries() -> list:
    with _history_lock:
        summaries = []
        for sid, hist in _sessions.items():
            last_content = ""
            last_ts = ""
            count = len(hist)
            if hist:
                last = hist[-1]
                last_content = last.get("content", "")[:50]
                last_ts = str(last.get("ts", ""))[:19]
            summaries.append({
                "session_id": sid[:16],
                "message_count": count,
                "last_message": last_content,
                "last_ts": last_ts,
            })
        return summaries

def _save_history() -> None:
    global _history_save_seq
    with _history_lock:
        data = {sid: list(hist) for sid, hist in _sessions.items()}
        save_seq = _history_save_seq + 1
        _history_save_seq = save_seq
    path = _get_history_file()
    tmp_path = None
    try:
        with _history_file_lock, _locked_history_file(shared=False):
            # 并发 snapshot 保护: 排队期间有更新 save 则旧快照跳过
            if save_seq != _history_save_seq:
                return
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".", prefix=".history.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if save_seq != _history_save_seq:
                    return
                os.replace(tmp_path, path)
                tmp_path = None
            finally:
                if tmp_path:
                    try: os.unlink(tmp_path)
                    except OSError as _e:
                        log.debug(f"[history] 临时文件清理失败: {_e}")
    except Exception as e:
        log.error(f"[history] 保存失败: {e}")

def _append_history(hist: list, user_text: str, assistant_reply: str) -> None:
    global _history_append_count
    now = time.time()
    with _history_lock:
        hist.append({"role": "user", "content": user_text, "ts": now})
        hist.append({"role": "assistant", "content": assistant_reply, "ts": now})
        while len(hist) > MAX_HISTORY * 2:
            hist.pop(0)
            hist.pop(0)  # 成对裁剪，保持 user/assistant 配对
    # 每 N 轮对话自动落盘，防止崩溃丢失（锁外调用避免与 snapshot 竞态）
    _history_append_count += 1
    if _history_append_count >= HISTORY_SAVE_EVERY:
        _history_append_count = 0
        try:
            _save_history()
        except Exception as e:
            log.debug(f"[agent.history] error at line 202: {e}")
            pass

def _load_history() -> None:
    try:
        file_data = _read_history_file()
    except Exception as e:
        log.debug(f"[agent.history] error at line 208: {e}")
        return
    if not file_data:
        return
    with _history_lock:
        for sid, msgs in file_data.items():
            if isinstance(msgs, list):
                # 原地 clear+extend 保持 _sessions[sid] 与 _history 引用一致
                # (避免 _sessions[sid]=msgs 重新赋值断开 voice_agent._history 绑定)
                if sid in _sessions:
                    _sessions[sid].clear()
                    _sessions[sid].extend(msgs)
                else:
                    # 拷贝一层断开与 _history_file_cache 的别名：
                    # 否则后续 append/pop 会原地改写"磁盘缓存"，sig 未变时
                    # 其他读取方拿到的是被内存污染过的版本
                    _sessions[sid] = list(msgs)
        if "default" not in _sessions:
            _sessions["default"] = []

def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return len(text) // 2

def _estimate_msg_tokens(msg: dict) -> int:
    # +4: 每条消息 role/name 开销(对齐 OpenAI tokenizer 估算, 避免预算偏低致历史裁剪不足)
    return _estimate_tokens(msg.get("content", "")) + 4

def _trim_history_tokens(hist: list, max_tokens: int = 6000, session_id: str = "default") -> None:
    if not hist:
        return
    # hist 是活动会话列表：并发流的 append 持有 _history_lock，
    # 这里 pop/统计也必须全程持锁，否则配对裁剪与 token 统计漂移
    with _history_lock:
        total = sum(_estimate_msg_tokens(m) for m in hist)
        removed_texts = []
        while total > max_tokens and len(hist) > 4:  # 保最后2轮(4条)维持上下文连续性
            removed = hist.pop(0)
            total -= _estimate_msg_tokens(removed)
            removed_texts.append(removed.get("content", "")[:60])
            if hist:
                removed = hist.pop(0)
                total -= _estimate_msg_tokens(removed)
                removed_texts.append(removed.get("content", "")[:60])
        # 将被裁剪的对话写入摘要，供跨会话上下文引用
        if removed_texts:
            _ctx_summary = "；".join(removed_texts[:6])
            if len(_ctx_summary) > 200:
                _ctx_summary = _ctx_summary[:200] + "..."
            _context_summaries[session_id] = _ctx_summary

def reset_history(session_id: str = "default") -> None:
    with _history_lock:
        hist = _sessions.get(session_id)
        if hist is not None:
            hist.clear()
        else:
            _sessions[session_id] = []
            _evict_overflow_sessions_locked()
    _save_history()