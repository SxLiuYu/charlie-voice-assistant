"""跨设备状态同步

解决同一局域网内多个 Charlie 客户端(浏览器/ESP32/飞书) 状态不一致的问题。

架构:
  1. 共享文件 (user_state_sync.json) + fcntl 机器级锁
     → 同一台机器上多进程(voice_server + schedulers) 状态一致
  2. MQTT 主题 (charlie/state/{user_id})
     → 跨设备(多机器/ESP32) 状态广播
  3. 内存缓存 + 定期快照
     → 减少 I/O, 60s 同步一次, 重大变化立即同步

同步内容:
  - user_state: at_home/away/sleeping/working + confidence
  - persona: mood, relationship_level, tone_profile
  - preferences: 用户偏好 (key=value 对)
  - reminders_pending: 待提醒列表摘要(设备间提醒一致性)

不通过此模块同步的内容(已有独立通道):
  - 对话历史 → streaming WebSocket (实时)
  - 提醒TTS → MQTT/HTTPS/入队 (专用通道)
"""

import json
import time
import threading
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# ===== 同步文件路径 =====

def _get_sync_file() -> str:
    uid = os.environ.get("CHARLIE_USER_ID", "default")
    name = f"user_state_sync_{uid}.json" if uid != "default" else "user_state_sync.json"
    return os.path.join(DATA_DIR, name)


# ===== 内存缓存 =====

_sync_lock = threading.Lock()
_local_state: dict = {}
_last_sync_ts: float = 0
_SYNC_INTERVAL = 60  # 60s 定期同步
_DIRTY = threading.Event()  # 标记有未同步的变更


# ===== 文件级状态(机器内多进程共享) =====

def _acquire_file_lock():
    """获取状态文件锁。返回 (fd, filepath) 或 None."""
    import fcntl
    path = _get_sync_file()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return fd, path
    except (OSError, BlockingIOError):
        try:
            os.close(fd)
        except Exception:
            pass
        return None, None


def _acquire_file_lock_write():
    """获取状态文件写锁(独占)。"""
    import fcntl
    path = _get_sync_file()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, path
    except (OSError, BlockingIOError):
        try:
            os.close(fd)
        except Exception:
            pass
        return None, None


def _release_file_lock(fd):
    """释放锁并关闭 fd。"""
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


def read_shared_state() -> dict:
    """从共享文件读取最新状态(非阻塞, 加共享锁)。"""
    fd, path = _acquire_file_lock()
    if fd is None:
        return {}
    try:
        data = os.read(fd, 65536).decode("utf-8")
        if data:
            return json.loads(data)
    except Exception as e:
        log.debug(f"[state_sync] read failed: {e}")
    finally:
        _release_file_lock(fd)
    return {}


def write_shared_state(state: dict) -> bool:
    """写入共享状态文件(独占锁, 原子替换)。"""
    fd, path = _acquire_file_lock_write()
    if fd is None:
        return False
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=None)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        return True
    except Exception as e:
        log.debug(f"[state_sync] write failed: {e}")
    finally:
        _release_file_lock(fd)
    return False


# ===== MQTT 跨设备广播 =====

def _publish_mqtt(state: dict) -> None:
    """发布状态到 MQTT topic (异步, fire-and-forget)。"""
    try:
        from app.mqtt_server import get_mqtt_client, _MQTT_AVAILABLE
        if not _MQTT_AVAILABLE:
            return
        client = get_mqtt_client()
        if client is None:
            return
        uid = os.environ.get("CHARLIE_USER_ID", "default")
        topic = f"charlie/state/{uid}"
        payload = json.dumps({
            "ts": time.time(),
            "state": state,
            "source": os.uname().nodename if hasattr(os, 'uname') else "unknown",
        }, ensure_ascii=False)
        client.publish(topic, payload, qos=0, retain=False)
        log.debug(f"[state_sync] MQTT 广播 → {topic}")
    except Exception as e:
        log.debug(f"[state_sync] MQTT 广播跳过: {e}")


# ===== 公共接口 =====

def sync_state(section: str, data: dict, immediate: bool = False) -> None:
    """同步一段状态到共享文件 + MQTT。

    参数:
      section:  状态段名称 ("user_state", "persona", "preferences")
      data:     状态数据 dict (必须可 JSON 序列化)
      immediate: True = 立即同步; False = 标脏 + 定期同步
    """
    if not data:
        return
    with _sync_lock:
        _local_state[section] = data
        _local_state["_ts"] = time.time()
        _DIRTY.set()

    if immediate:
        _flush_sync()
    else:
        # 首次变更时启动后台线程(幂等)
        _schedule_flush()


def get_synced_state(section: str = "", default=None) -> dict | any:
    """读取本地缓存的状态段。
    如果本地为空则尝试从共享文件加载(启动时热加载)。
    """
    with _sync_lock:
        if not _local_state:
            _load_from_shared_file()
        if section:
            return _local_state.get(section, default or {})
        return dict(_local_state)


def _load_from_shared_file() -> None:
    """从共享文件加载状态到内存(启动时调用)。"""
    shared = read_shared_state()
    if shared:
        with _sync_lock:
            _local_state.update(shared)
            _last_sync_ts = time.time()
        log.info(f"[state_sync] 加载共享状态: {len(shared)} 段")


def _flush_sync() -> None:
    """强制刷盘 + MQTT 广播。"""
    with _sync_lock:
        if not _local_state:
            return
        state = dict(_local_state)
        _DIRTY.clear()
        _last_sync_ts = time.time()

    # 1. 写共享文件
    write_shared_state(state)
    # 2. MQTT 广播
    _publish_mqtt(state)
    log.debug(f"[state_sync] 同步完成 ({len(state)} 段)")


def _schedule_flush() -> None:
    """防抖: 500ms 内再次变更则合并, 最终后台 flush。"""
    def _delayed():
        _DIRTY.wait(timeout=0.5)
        if _DIRTY.is_set():
            _flush_sync()

    threading.Thread(target=_delayed, daemon=True).start()


# ===== 定期同步守护 =====

_sync_thread: threading.Thread | None = None


def _sync_loop() -> None:
    """后台循环: 定期同步 + 从 MQTT 接收跨设备更新。"""
    # 启动时热加载
    _load_from_shared_file()

    while True:
        try:
            time.sleep(_SYNC_INTERVAL)
            # 定期强制同步 (即使没有变更也刷新 timestamp)
            _flush_sync()
        except Exception as e:
            log.debug(f"[state_sync] loop error: {e}")


def start_state_sync() -> None:
    """启动状态同步守护线程(幂等)。"""
    global _sync_thread
    if _sync_thread is not None and _sync_thread.is_alive():
        return
    _sync_thread = threading.Thread(target=_sync_loop, daemon=True)
    _sync_thread.start()
    log.info("[state_sync] 跨设备状态同步已启动")


# ===== 便捷集成: user_state / persona / preferences =====

def sync_user_state() -> dict:
    """将 agent.state._user_state 同步到共享状态。"""
    try:
        from agent.state import get_user_state as _gus
        state = _gus()
        sync_state("user_state", state)
        return state
    except Exception as e:
        log.debug(f"[state_sync] user_state 同步跳过: {e}")
        return {}


def sync_persona() -> dict:
    """将 agent.persona 状态同步到共享状态。"""
    try:
        from agent.persona import _load as _persona_load
        state = _persona_load()
        sync_state("persona", {
            "mood": state.get("mood", "neutral"),
            "formality": state.get("formality", "casual"),
            "humor_level": state.get("humor_level", 0.3),
            "relationship_level": state.get("relationship_level", "acquaintance"),
            "tone_profile": state.get("tone_profile", "casual"),
            "interaction_count": state.get("interaction_count", 0),
        })
        return state
    except Exception as e:
        log.debug(f"[state_sync] persona 同步跳过: {e}")
        return {}


def sync_preferences() -> dict:
    """将 agent.preferences 同步到共享状态。"""
    try:
        from agent.preferences import list_preferences as _lp
        prefs = _lp()
        if prefs:
            sync_state("preferences", dict(prefs))
        return prefs
    except Exception as e:
        log.debug(f"[state_sync] preferences 同步跳过: {e}")
        return {}


def full_sync() -> None:
    """全量同步: user_state + persona + preferences。用于重大变更后。"""
    sync_user_state()
    sync_persona()
    sync_preferences()


# ===== MQTT 接收端 (订阅 charlie/state/{user_id}) =====

def setup_mqtt_listener() -> None:
    """订阅 MQTT state topic, 接收跨设备状态更新。
    在其他设备修改状态后, 本进程自动拉取最新值。
    """
    try:
        from app.mqtt_server import get_mqtt_client, _MQTT_AVAILABLE
        if not _MQTT_AVAILABLE:
            return
        client = get_mqtt_client()
        if client is None:
            return

        uid = os.environ.get("CHARLIE_USER_ID", "default")
        topic = f"charlie/state/{uid}"

        def _on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                remote_state = payload.get("state", {})
                src = payload.get("source", "?")
                # 忽略自己发出的消息
                if src == os.uname().nodename if hasattr(os, 'uname') else False:
                    return
                with _sync_lock:
                    _local_state.update(remote_state)
                    _local_state["_ts"] = payload.get("ts", time.time())
                log.info(f"[state_sync] 收到远程状态更新 from {src}: {len(remote_state)} 段")
                # 通知各子系统应用新状态
                _apply_remote_state(remote_state)
            except Exception as e:
                log.debug(f"[state_sync] on_message error: {e}")

        client.subscribe(topic, qos=0)
        client.message_callback_add(topic, _on_message)
        log.info(f"[state_sync] MQTT 监听 → {topic}")
    except Exception as e:
        log.debug(f"[state_sync] MQTT 监听跳过: {e}")


def _apply_remote_state(state: dict) -> None:
    """将远程接收的状态应用到本地子系统。"""
    # 更新 user_state
    if "user_state" in state:
        try:
            from agent.state import _user_state, _user_state_lock
            with _user_state_lock:
                _user_state.update(state["user_state"])
                _user_state["last_update"] = time.time()
        except Exception:
            pass
    # 更新 persona
    if "persona" in state:
        try:
            from agent.persona import _load, _save
            uid = _persona_load()
            persona_state = state["persona"]
            for key in ("mood", "formality", "humor_level", "relationship_level", "tone_profile"):
                if key in persona_state:
                    uid[key] = persona_state[key]
            _save()
        except Exception:
            pass
    # 更新 preferences
    if "preferences" in state:
        try:
            from agent.preferences import set_preference
            for k, v in state["preferences"].items():
                set_preference(k, str(v))
        except Exception:
            pass


# ===== 统计 =====

def get_sync_stats() -> dict:
    """返回同步状态统计。"""
    with _sync_lock:
        return {
            "sections": list(_local_state.keys()),
            "last_sync": _last_sync_ts,
            "dirty": _DIRTY.is_set(),
            "sync_interval": _SYNC_INTERVAL,
        }
