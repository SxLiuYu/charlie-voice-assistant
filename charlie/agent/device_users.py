"""设备 → 用户映射（FR-14 批次 4a / 批次 6，方案 §6）。

背景
----
FR-14 批次 2 已把数据层（`preferences` / `persona` / `history` / `memory_store` /
`state_sync`）改成按 uid 分片，批次 3A 已把用户相关内容移出共享的
`brain.system_message`。但**没有任何地方绑定用户** —— `resolve_user()` 一律回落
`os.environ["CHARLIE_USER_ID"]`（默认 `"default"`），所以那两批改造在生产里
形同虚设。本模块提供"设备身份 → 用户"的持久映射，让 ESP32 入口在收到 `hello`
握手后能确定"这台设备现在是谁在用"。

为什么按 `device_key` 而不是 `session_id`
----------------------------------------
`app/xiaozhi_ws.py` 的 `session_id` 是 `uuid4().hex[:16]`，**每次重连都会变**；
而 `device_key` 在 `hello` 载荷里（`client_id` / `device_id` / `mac`），
跨重连稳定。映射必须挂在后者上，否则设备一断线就丢身份。

三层解析（批次 6 新增第 3 层）
-----------------------------
    ① `devices[device_key]`        —— 精确绑定，最高优先级
    ② `rooms[device_key]` → `room_users[room]`  —— 房间兜底
    ③ 未命中 ⇒ None（入口层不 bind，交给 `user_context` 三层解析）

第 ② 层的用途：设备标识是**固件层面**的东西（`client_id` / MAC）。重刷固件、
换板子、OTA 改名都会让 ① 失效，而"这台在客厅"是**物理事实**不会变。老于只要
给房间绑过人，换设备后重新指认房间即可，不必重建整张表。

另外 `rooms` 本身还有独立价值：管理页可以把 `aa:bb:cc:dd:ee:ff` 显示成「客厅」，
system prompt 也能告诉 LLM "当前设备在客厅"。

设计要点
--------
- **默认空映射 ⇒ 零行为变更**：未绑定的设备一律回落 `DEFAULT_USER`，
  所有数据仍写改造前的原文件名。可以先照常跑，再逐台绑定。
- 本模块**只做解析与持久化**，不写 `os.environ`、不设进程级全局 ——
  真正的"我是谁"由调用方用 `user_context.bind_user()` 限定在本次执行流内，
  这样同一进程里多台设备并发时才不会互相覆盖。
- 环境变量 `CHARLIE_DEVICE_USERS`（JSON 对象）可覆盖/补充文件里的 `devices`；
  `CHARLIE_DEVICE_ROOMS` 可覆盖/补充 `rooms` + `room_users`（**env 优先**，
  且不写回文件）。

⚠️ `device_key` 的清洗规则是本模块的 `_sanitize_device_key()`，**写入侧与
读取侧必须共用它**。`app/xiaozhi_ws.py` 曾经自带一份 `re.sub(r"[^0-9A-Za-z_-]","")[:32]`，
与这里的 `[^0-9A-Za-z_:-][:64]` 不一致 ⇒ MAC 带冒号、长 `client_id` 的设备
**绑定静默失效**（永远回落 default）。批次 6 已统一（见该文件 hello 分支注释）。

文件格式（`<DATA_DIR>/device_users.json`）
-----------------------------------------
    {
      "version": 1,
      "devices": {
        "aa:bb:cc:dd:ee:ff": "laoyu",
        "living-room": "jiaren1"
      },
      "rooms": {
        "aa:bb:cc:dd:ee:ff": "客厅"
      },
      "room_users": {
        "客厅": "laoyu"
      }
    }

三张表都是可选的；缺表按空表处理。房间名允许中文（与 uid 同一套字符集）。
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("magic")

DATA_DIR = os.environ.get(
    "ASSISTANT_KID_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

_MAP_FILE = "device_users.json"
_ENV_VAR = "CHARLIE_DEVICE_USERS"
_ROOMS_ENV_VAR = "CHARLIE_DEVICE_ROOMS"

# uid 允许的字符：与各数据层分片文件名的拼接方式保持一致（见 preferences._prefs_file）。
# 含 `/`、`..`、空格等会把分片文件写出目录 —— 从 API 传进来时必须挡住。
_UID_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff-]{1,32}$")

# 房间名：与 uid 同一套字符集 + 空格（"living room" 这类写法）。
# 房间名**不参与文件名拼接**，但会进 system prompt 与 JSON，所以同样要挡控制字符。
_ROOM_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff -]{1,24}$")

# `app/xiaozhi_ws.py` 调 brain 时用的 session_id 前缀（`"xiaozhi-" + device_key`）。
SESSION_PREFIX = "xiaozhi-"
# `app/mqtt_server.py::_process_utterance` 调 brain 时用的前缀
# （`"mqtt-" + device_id`，见该文件 747 行附近）。两条语音入口都进
# brain_stream_sentences，`room_for_session` 必须都能反解出设备身份 ——
# 只认 xiaozhi- 会让 MQTT 路径的房间上下文静默丢失。
MQTT_SESSION_PREFIX = "mqtt-"
_SESSION_PREFIXES = (SESSION_PREFIX, MQTT_SESSION_PREFIX)

_lock = threading.RLock()
_cache: dict | None = None       # 文件内容（三张表），不含 env
_cache_sig: tuple | None = None


def _map_path() -> str:
    return os.path.join(DATA_DIR, _MAP_FILE)


def _empty_state() -> dict:
    return {"devices": {}, "rooms": {}, "room_users": {}}


def sanitize_uid(uid: str | None) -> str | None:
    """校验 uid 是否可安全用作分片文件名后缀。不合法返回 None。"""
    if not uid:
        return None
    uid = str(uid).strip()
    if uid in ("default", "."):
        return None
    return uid if _UID_RE.match(uid) else None


def sanitize_room(room: str | None) -> str | None:
    """校验房间名。不合法返回 None（含 `/`、`..`、控制字符等）。

    ⚠️ 房间名**允许中文** —— 不能复用 `_sanitize_device_key`（它按
    `[^0-9A-Za-z_:-]` 清洗，会把「客厅」整串吃掉，静默变成空字符串）。
    """
    if room is None:
        return None
    room = str(room).strip()
    if not room:
        return None
    return room if _ROOM_RE.match(room) else None


def _sanitize_device_key(device_key: str | None) -> str:
    """归一化设备标识（唯一事实来源）。

    ⚠️ 这是**写入侧与读取侧共用的**清洗函数。`app/xiaozhi_ws.py` 的 hello
    解析必须调它，不能再自带一份正则 —— 两侧规则漂移会让绑定静默失效
    （历史 bug：那边 `[^0-9A-Za-z_-][:32]` vs 这边 `[^0-9A-Za-z_:-][:64]`）。
    """
    if not device_key:
        return ""
    return re.sub(r"[^0-9A-Za-z_:-]", "", str(device_key))[:64]


def device_key_from_session(session_id: str | None) -> str:
    """从 brain 用的 session_id 反推 device_key（批次 6）。

    `app/xiaozhi_ws.py` 调 `brain_stream_sentences(..., session_id="xiaozhi-"+device_key)`，
    `app/mqtt_server.py` 调 `session_id="mqtt-"+device_id` —— 两种前缀都剥。
    都不是这种形状 ⇒ 返回 `""`（不做猜测）。
    """
    if not session_id:
        return ""
    s = str(session_id)
    for prefix in _SESSION_PREFIXES:
        if s.startswith(prefix):
            return _sanitize_device_key(s[len(prefix):])
    return ""


def _file_sig() -> tuple | None:
    """文件指纹，用于检测外部改动（人工编辑 JSON 后无需重启服务）。"""
    try:
        st = os.stat(_map_path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _normalize_state(raw: dict | None) -> dict:
    """把任意 dict 归一化成 `{"devices":…, "rooms":…, "room_users":…}`。

    非法条目**逐条丢弃**而不是整表丢弃 —— 手工编辑 JSON 时错一个 key
    不应该让其余绑定全部失效。
    """
    st = _empty_state()
    if not isinstance(raw, dict):
        return st
    devices = raw.get("devices")
    if isinstance(devices, dict):
        for k, v in devices.items():
            ck, cu = _sanitize_device_key(k), sanitize_uid(v)
            if ck and cu:
                st["devices"][ck] = cu
    rooms = raw.get("rooms")
    if isinstance(rooms, dict):
        for k, v in rooms.items():
            ck, cr = _sanitize_device_key(k), sanitize_room(v)
            if ck and cr:
                st["rooms"][ck] = cr
    room_users = raw.get("room_users")
    if isinstance(room_users, dict):
        for k, v in room_users.items():
            cr, cu = sanitize_room(k), sanitize_uid(v)
            if cr and cu:
                st["room_users"][cr] = cu
    return st


def _read_file_state() -> dict:
    """读文件里的三张表。文件不存在/损坏 → 空表（不抛异常）。"""
    try:
        with open(_map_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except Exception as e:
        log.warning("[device_users] 映射文件读取失败（按空映射处理）: %s", e)
        return _empty_state()
    return _normalize_state(raw)


def _read_env_state() -> dict:
    """读 env 覆盖。env 优先于文件。"""
    st = _empty_state()
    raw = os.environ.get(_ENV_VAR, "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            log.warning("[device_users] %s 不是合法 JSON，已忽略: %s", _ENV_VAR, e)
            data = None
        if isinstance(data, dict):
            # 允许两种写法：{"devices": {...}} 或直接 {"dev": "uid"}
            devices = data.get("devices") if isinstance(data.get("devices"), dict) else data
            for k, v in devices.items():
                ck, cu = _sanitize_device_key(k), sanitize_uid(v)
                if ck and cu:
                    st["devices"][ck] = cu

    raw = os.environ.get(_ROOMS_ENV_VAR, "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            log.warning("[device_users] %s 不是合法 JSON，已忽略: %s", _ROOMS_ENV_VAR, e)
            data = None
        if isinstance(data, dict):
            # 两种写法：{"dev": "客厅"}（只给 rooms）
            # 或 {"rooms": {...}, "room_users": {...}}（两张表都给）
            if isinstance(data.get("rooms"), dict) or isinstance(data.get("room_users"), dict):
                rooms = data.get("rooms")
                if isinstance(rooms, dict):
                    for k, v in rooms.items():
                        ck, cr = _sanitize_device_key(k), sanitize_room(v)
                        if ck and cr:
                            st["rooms"][ck] = cr
                room_users = data.get("room_users")
                if isinstance(room_users, dict):
                    for k, v in room_users.items():
                        cr, cu = sanitize_room(k), sanitize_uid(v)
                        if cr and cu:
                            st["room_users"][cr] = cu
            else:
                for k, v in data.items():
                    ck, cr = _sanitize_device_key(k), sanitize_room(v)
                    if ck and cr:
                        st["rooms"][ck] = cr
    return st


def _file_state() -> dict:
    """带指纹缓存的文件内容（三张表）。"""
    global _cache, _cache_sig
    sig = _file_sig()
    with _lock:
        if _cache is None or sig != _cache_sig:
            _cache = _read_file_state()
            _cache_sig = sig
        # 返回深拷贝，避免调用方原地修改缓存
        return {
            "devices": dict(_cache["devices"]),
            "rooms": dict(_cache["rooms"]),
            "room_users": dict(_cache["room_users"]),
        }


def all_state() -> dict:
    """当前生效的完整状态（文件 ∪ env，env 优先）。返回副本。"""
    st = _file_state()
    env = _read_env_state()
    # env 每次都读：改 .env 后重启即生效，但也允许测试里 monkeypatch 立刻生效
    st["devices"].update(env["devices"])
    st["rooms"].update(env["rooms"])
    st["room_users"].update(env["room_users"])
    return st


def all_mappings() -> dict:
    """设备 → 用户映射（文件 ∪ env，env 优先）。返回副本。"""
    return all_state()["devices"]


def all_rooms() -> dict:
    """设备 → 房间名映射。返回副本。"""
    return all_state()["rooms"]


def all_room_users() -> dict:
    """房间名 → 用户映射（兜底绑定）。返回副本。"""
    return all_state()["room_users"]


def _resolve_from(st: dict, device_key: str | None) -> str | None:
    """纯函数版解析（不读文件），供 `devices_for_user` 复用同一套优先级。

    优先级：① `devices[device_key]` ② `room_users[rooms[device_key]]` ③ None
    """
    ck = _sanitize_device_key(device_key)
    if not ck:
        return None
    uid = st["devices"].get(ck)
    if uid:
        return uid
    room = st["rooms"].get(ck)
    if room:
        return st["room_users"].get(room)
    return None


def get_device_user(device_key: str | None) -> str | None:
    """查询设备绑定的 uid（含房间兜底）；未绑定返回 None。"""
    return _resolve_from(all_state(), device_key)


def get_device_room(device_key: str | None) -> str | None:
    """查询设备所在房间名；未设置返回 None。"""
    ck = _sanitize_device_key(device_key)
    if not ck:
        return None
    return all_rooms().get(ck)


def room_for_session(session_id: str | None) -> str | None:
    """由 brain 的 session_id 直接得到房间名（批次 6：注入 system prompt 用）。

    支持 `xiaozhi-*`（WS 入口）与 `mqtt-*`（MQTT 入口）两种前缀。
    非这两种形状的 session（HTTP /ws、飞书）返回 None —— 它们没有
    设备归属，硬猜会把某个房间写进所有入口。
    """
    dk = device_key_from_session(session_id)
    if not dk:
        return None
    return get_device_room(dk)


def devices_for_user(user_id: str | None) -> set[str]:
    """反查：某个用户绑定了哪些设备（FR-14 投递路由用）。

    用于回答"这条提醒该推到哪台设备"。返回的是**设备标识集合**，
    调用方再与在线设备表求交集。

    ⚠️ 与 `get_device_user()` **严格互为逆运算**：候选集是 `devices` 与
    `rooms` 两张表的 key 的并集，再逐个用 `_resolve_from()` 判定归属。
    这样"设备直接绑 A、其房间绑 B"的冲突情形下，两边结论必然一致
    （`devices` 优先）—— 若这里只按 `rooms` 反查，A 就会同时出现在两个人的
    设备集合里，提醒被推给错的人。

    ⚠️ `"default"` 不是合法 uid（`sanitize_uid` 拒收）⇒ 返回空集。
    这正是投递层需要的语义："owner=default ⇒ 没有定向设备 ⇒ 广播"。
    """
    uid = sanitize_uid(user_id)
    if not uid:
        return set()
    st = all_state()
    candidates = set(st["devices"]) | set(st["rooms"])
    return {k for k in candidates if _resolve_from(st, k) == uid}


def resolve_device_user(device_key: str | None) -> str:
    """解析设备对应的 uid；未绑定/非法一律回落 `DEFAULT_USER`。

    适合"展示/诊断"场景（比如管理页显示某台设备当前是谁）。
    ⚠️ **入口层不要用这个去 bind** —— 见 `bound_device_user()`。
    """
    from agent.user_context import DEFAULT_USER
    return get_device_user(device_key) or DEFAULT_USER


def bound_device_user(device_key: str | None) -> str | None:
    """返回设备**真实绑定**的 uid；未绑定返回 `None`（不回落 default）。

    入口层的正确用法：

        uid = bound_device_user(device_key)
        if uid is None:
            await handle()                 # 不 bind，交给三层解析
        else:
            with bind_user(uid):
                await handle()

    **为什么未绑定时必须返回 None 而不是 DEFAULT_USER**：
    `bind_user("default")` 会把 `os.environ["CHARLIE_USER_ID"]` 的兜底**盖掉**。
    如果用户在 `.env` 里设了 `CHARLIE_USER_ID=laoyu`（单用户老配置），
    未绑定设备一旦 bind("default")，这条路会从 laoyu 突变成 default，
    表现就是"记忆、偏好、历史全部凭空消失"。
    不 bind 时三层解析（ContextVar → session 注册表 → env）自然给出正确答案。
    """
    return get_device_user(device_key)


def _write_state(state: dict) -> None:
    """原子写回三张表（调用方必须已持有 `_lock`）。"""
    global _cache, _cache_sig
    path = _map_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, **state}, f,
                  ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _cache = state
    _cache_sig = _file_sig()


def set_device_user(device_key: str, user_id: str | None) -> bool:
    """绑定/解绑设备 → 用户（写文件，返回是否发生变更）。

    `user_id` 为空或 `"default"` 表示解绑。uid 非法时抛 `ValueError`
    （调用方是 API，必须让用户看到明确错误，而不是静默丢弃）。
    """
    ck = _sanitize_device_key(device_key)
    if not ck:
        raise ValueError("device_key 不能为空")
    cu = sanitize_uid(user_id)
    if user_id not in (None, "", "default") and cu is None:
        raise ValueError(
            f"user_id 不合法: {user_id!r}（只允许中英文/数字/下划线/连字符，且不能是 default）")

    with _lock:
        # 以**文件内容**为基准改，避免把 env 里的条目固化进文件
        state = _read_file_state()
        devices = state["devices"]
        if cu is None:
            changed = devices.pop(ck, None) is not None
        else:
            changed = devices.get(ck) != cu
            devices[ck] = cu
        if not changed:
            return False
        _write_state(state)
    log.info("[device_users] %s → %s", ck, cu or "(解绑)")
    return True


def set_device_room(device_key: str, room: str | None) -> bool:
    """设置/清除设备所在房间名（写文件，返回是否发生变更）。

    `room` 为空 ⇒ 清除该设备的房间标记（**不动** `room_users` 里的房间绑定，
    那个是房间维度的，删它会让同房间其他设备一起失去兜底）。
    """
    ck = _sanitize_device_key(device_key)
    if not ck:
        raise ValueError("device_key 不能为空")
    cr = None if room in (None, "") else sanitize_room(room)
    if room not in (None, "") and cr is None:
        raise ValueError(
            f"房间名不合法: {room!r}（只允许中英文/数字/下划线/连字符/空格，≤24 字）")

    with _lock:
        state = _read_file_state()
        rooms = state["rooms"]
        if cr is None:
            changed = rooms.pop(ck, None) is not None
        else:
            changed = rooms.get(ck) != cr
            rooms[ck] = cr
        if not changed:
            return False
        _write_state(state)
    log.info("[device_users] 设备 %s 房间 → %s", ck, cr or "(清除)")
    return True


def set_room_user(room: str, user_id: str | None) -> bool:
    """绑定/解绑 房间 → 用户（兜底身份，写文件，返回是否发生变更）。"""
    cr = sanitize_room(room)
    if cr is None:
        raise ValueError(
            f"房间名不合法: {room!r}（只允许中英文/数字/下划线/连字符/空格，≤24 字）")
    cu = sanitize_uid(user_id)
    if user_id not in (None, "", "default") and cu is None:
        raise ValueError(
            f"user_id 不合法: {user_id!r}（只允许中英文/数字/下划线/连字符，且不能是 default）")

    with _lock:
        state = _read_file_state()
        room_users = state["room_users"]
        if cu is None:
            changed = room_users.pop(cr, None) is not None
        else:
            changed = room_users.get(cr) != cu
            room_users[cr] = cu
        if not changed:
            return False
        _write_state(state)
    log.info("[device_users] 房间 %s → %s", cr, cu or "(解绑)")
    return True


def remove_device_user(device_key: str) -> bool:
    """解绑设备（连带清除它的房间标记）。返回是否发生变更。

    "忘掉这台设备"是一个整体动作 —— 只清 `devices` 而留着 `rooms`，
    会让它继续通过房间兜底拿到身份，用户看到的现象是"删了还在"。
    """
    ck = _sanitize_device_key(device_key)
    if not ck:
        return False
    with _lock:
        state = _read_file_state()
        changed = state["devices"].pop(ck, None) is not None
        if state["rooms"].pop(ck, None) is not None:
            changed = True
        if not changed:
            return False
        _write_state(state)
    log.info("[device_users] %s 已解绑（含房间标记）", ck)
    return True


def reset_cache() -> None:
    """丢弃内存缓存（测试与人工改文件后强制重读用）。"""
    global _cache, _cache_sig
    with _lock:
        _cache = None
        _cache_sig = None


def stats() -> dict:
    """诊断信息（供 `/api/device-users` 与管理页展示）。"""
    with _lock:
        file_state = _read_file_state()
    env_state = _read_env_state()
    effective = all_state()
    return {
        "map_file": _map_path(),
        "map_file_exists": os.path.exists(_map_path()),
        "from_file": len(file_state["devices"]),
        "from_env": len(env_state["devices"]),
        "effective": len(effective["devices"]),
        "rooms": len(effective["rooms"]),
        "room_users": len(effective["room_users"]),
        "env_var": _ENV_VAR,
        "rooms_env_var": _ROOMS_ENV_VAR,
        "loaded_at": time.time(),
    }
