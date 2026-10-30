"""用户偏好: 跨进程原子持久化"""
import os
import json
import tempfile
import threading
try:
    import fcntl
except ImportError:  # Windows 无 fcntl
    import fcntl_compat as fcntl
import logging
from contextlib import contextmanager
from typing import Callable, Any

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# FR-14：`default` 用户沿用改造前的文件名（不迁移），其他 uid 加 `_{uid}` 后缀。
PREFS_FILE = os.path.join(DATA_DIR, "preferences.json")
PREFS_LOCK_FILE = PREFS_FILE + ".lock"

DEFAULT_USER = "default"

# default 用户的内存状态保持模块级标量/字典（向后兼容既有测试与外部导入）；
# 其他 uid 各自一份，键为 uid。
_preferences = {}
_preferences_revision = 0
_preferences_save_seq = 0
_preferences_file_signature = None

_uid_prefs: dict[str, dict] = {}
_uid_signatures: dict[str, object] = {}
_uid_revisions: dict[str, int] = {}
_uid_save_seqs: dict[str, int] = {}

_prefs_lock = threading.RLock()
_preferences_file_lock = threading.Lock()


def _resolve_uid(uid: str | None = None) -> str:
    """显式 uid 优先，其次 ContextVar（`bind_user`），最后环境变量。"""
    if uid:
        return uid
    from agent.user_context import resolve_user
    return resolve_user()


def _prefs_file(uid: str | None = None) -> str:
    u = _resolve_uid(uid)
    if u == DEFAULT_USER:
        return PREFS_FILE
    return os.path.join(DATA_DIR, f"preferences_{u}.json")


def _prefs_lock_file(uid: str | None = None) -> str:
    u = _resolve_uid(uid)
    if u == DEFAULT_USER:
        return PREFS_LOCK_FILE
    return _prefs_file(u) + ".lock"


def _get_prefs_dict(u: str) -> dict:
    """取某 uid 的内存偏好字典（按对象身份，调用方可原地改）。"""
    if u == DEFAULT_USER:
        return _preferences
    d = _uid_prefs.get(u)
    if d is None:
        d = _uid_prefs[u] = {}
    return d


def _get_signature(u: str):
    if u == DEFAULT_USER:
        return _preferences_file_signature
    return _uid_signatures.get(u)


def _set_signature(u: str, sig) -> None:
    global _preferences_file_signature
    if u == DEFAULT_USER:
        _preferences_file_signature = sig
    else:
        _uid_signatures[u] = sig


def _bump_revision(u: str) -> None:
    global _preferences_revision
    if u == DEFAULT_USER:
        _preferences_revision += 1
    else:
        _uid_revisions[u] = _uid_revisions.get(u, 0) + 1


def _get_revision(u: str) -> int:
    if u == DEFAULT_USER:
        return _preferences_revision
    return _uid_revisions.get(u, 0)


def _next_save_seq(u: str) -> int:
    global _preferences_save_seq
    if u == DEFAULT_USER:
        _preferences_save_seq += 1
        return _preferences_save_seq
    seq = _uid_save_seqs.get(u, 0) + 1
    _uid_save_seqs[u] = seq
    return seq


def _get_save_seq(u: str) -> int:
    if u == DEFAULT_USER:
        return _preferences_save_seq
    return _uid_save_seqs.get(u, 0)


@contextmanager
def _locked_preferences(shared: bool = False, uid: str | None = None):
    with open(_prefs_lock_file(uid), "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

def _preferences_file_signature_now(uid: str | None = None):
    try:
        st = os.stat(_prefs_file(uid))
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

def _read_locked_preferences(uid: str | None = None) -> dict:
    try:
        with open(_prefs_file(uid), "r", encoding="utf-8") as f:
            prefs = json.load(f)
            return prefs if isinstance(prefs, dict) else {}
    except Exception:
        return {}

def _write_preferences_temp_locked(prefs: dict, uid: str | None = None) -> str:
    directory = os.path.dirname(_prefs_file(uid))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory,
        prefix=".preferences.", suffix=".tmp", delete=False,
    ) as temp_file:
        temp_path = temp_file.name
        json.dump(prefs, temp_file, ensure_ascii=False, indent=2)
        temp_file.flush()
        os.fsync(temp_file.fileno())
    return temp_path

def _write_locked_preferences(prefs: dict, uid: str | None = None) -> None:
    temp_path = ""
    try:
        temp_path = _write_preferences_temp_locked(prefs, uid)
        os.replace(temp_path, _prefs_file(uid))
        temp_path = ""
    except Exception:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

def _bump_preferences_revision(uid: str | None = None) -> None:
    """兼容旧调用：默认给 default 用户 +1；传 uid 给对应分片 +1。"""
    _bump_revision(_resolve_uid(uid))

def _load_preferences(uid: str | None = None) -> None:
    u = _resolve_uid(uid)
    with _locked_preferences(shared=True, uid=u):
        prefs = _read_locked_preferences(u)
        signature = _preferences_file_signature_now(u)
    with _prefs_lock:
        target = _get_prefs_dict(u)
        target.clear()
        target.update(prefs)
        _set_signature(u, signature)
        _bump_revision(u)

def _refresh_preferences_if_changed(uid: str | None = None) -> bool:
    u = _resolve_uid(uid)
    with _prefs_lock:
        old_signature = _get_signature(u)
    with _locked_preferences(shared=True, uid=u):
        new_signature = _preferences_file_signature_now(u)
        if new_signature == old_signature:
            return False
        prefs = _read_locked_preferences(u)
    with _prefs_lock:
        if new_signature == _get_signature(u):
            return False
        target = _get_prefs_dict(u)
        target.clear()
        target.update(prefs)
        _set_signature(u, new_signature)
        _bump_revision(u)
    return True

def _save_preferences(uid: str | None = None) -> None:
    u = _resolve_uid(uid)
    with _prefs_lock:
        snapshot = dict(_get_prefs_dict(u))
        save_seq = _next_save_seq(u)
    with _preferences_file_lock:
        with _locked_preferences(shared=False, uid=u):
            if save_seq != _get_save_seq(u):
                return
            disk_prefs = _read_locked_preferences(u)
            merged = dict(disk_prefs)
            merged.update(snapshot)
            if save_seq != _get_save_seq(u):
                return
            temp_path = ""
            try:
                temp_path = _write_preferences_temp_locked(merged, u)
                if save_seq != _get_save_seq(u):
                    return
                os.replace(temp_path, _prefs_file(u))
                temp_path = ""
            except (OSError, IOError) as e:
                log.warning(f"[prefs] 保存失败: {e}")
                return
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            _set_signature(u, _preferences_file_signature_now(u))

def _commit_preferences(mutate: Callable[[dict], Any], uid: str | None = None) -> Any:
    u = _resolve_uid(uid)
    with _preferences_file_lock:
        with _locked_preferences(shared=False, uid=u):
            disk_prefs = _read_locked_preferences(u)
            with _prefs_lock:
                target = _get_prefs_dict(u)
                merged = dict(disk_prefs)
                merged.update(target)
                result = mutate(merged)
                # 原地替换，保持外部持有的字典引用（如 voice_agent._preferences）继续有效
                target.clear()
                target.update(merged)
                _bump_revision(u)
            try:
                _write_locked_preferences(merged, u)
            except (OSError, IOError) as e:
                log.warning(f"[prefs] 保存失败: {e}")
                return result
            _set_signature(u, _preferences_file_signature_now(u))
    # 失效 system_msg 缓存，确保下次看到最新偏好
    try:
        from agent.system_msg import invalidate_system_msg_cache
        invalidate_system_msg_cache()
    except Exception as e:
        log.debug(f"[agent.preferences] error at line 163: {e}")
        pass
    return result

def set_preference(key: str, value: str, uid: str | None = None) -> str:
    _commit_preferences(lambda prefs: prefs.__setitem__(key, value), uid)
    log.info(f"[prefs] 设置偏好: {key}={value[:30]}")
    return f"已记住：{key}={value}"

def get_preference(key: str, uid: str | None = None) -> str:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        return _get_prefs_dict(u).get(key, "")

def preference_count(uid: str | None = None) -> int:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        return len(_get_prefs_dict(u))

def list_preferences(uid: str | None = None) -> dict:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        return dict(_get_prefs_dict(u))

def preferences_etag_token(uid: str | None = None) -> str:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        return f"preferences:{_get_revision(u)}:{len(_get_prefs_dict(u))}"

def preferences_snapshot(uid: str | None = None) -> tuple[dict, str]:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        return dict(_get_prefs_dict(u)), f"preferences:{_get_revision(u)}:{len(_get_prefs_dict(u))}"

def preferences_conditional(
    etag_matches: Callable[[str], bool],
    etag_for_token: Callable[[str], str],
    uid: str | None = None,
) -> tuple[dict | None, str]:
    u = _resolve_uid(uid)
    _refresh_preferences_if_changed(u)
    with _prefs_lock:
        token = f"preferences:{_get_revision(u)}:{len(_get_prefs_dict(u))}"
        if etag_matches(etag_for_token(token)):
            return None, token
        return dict(_get_prefs_dict(u)), token

def del_preference(key: str, uid: str | None = None) -> str:
    def remove_key(prefs: dict) -> bool:
        if key not in prefs:
            return False
        del prefs[key]
        return True
    found = _commit_preferences(remove_key, uid)
    if found:
        return f"已忘记：{key}"
    return f"未找到偏好：{key}"

_load_preferences()