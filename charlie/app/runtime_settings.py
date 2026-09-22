"""运行时默认参数 —— 不用改 .env、不用重启服务就能调整。

这些参数原先都是硬编码，各在各的地方
------------------------------------
* 设备开机是否免唤醒自动开麦 —— 写死在固件编译期（CONFIG_AUTO_LISTEN_ON_BOOT），
  改一次要重编译固件 + OTA 重刷
* 语音语速 —— 写死在 ``agent/roles.py`` 的 ``_ROLES`` 字典里（charlie 1.0 / 0.92 / 0.9 ...），
  改一处等于改所有用户
* TTS 优先级 —— 只认 ``.env`` 的 ``TTS_PRIORITY``，改完必须重启服务

本模块把这三类参数收成一个地方：内存覆盖 + JSON 落盘，读侧一律走 getter。

生效时机（站点页面上会如实标注，别骗用户）
----------------------------------------
=================  ====================================================
device.auto_listen 设备下次开机取 OTA 配置时生效（需设备重启一次）
device.ota_url     设备下次开机取 OTA 配置时生效（需设备重启一次）
tts.voice          立即生效（清空 TTS 缓存，无需重启服务）
tts.speed          立即生效（同上）
tts.priority       立即生效（下一条合成即按新顺序选 provider）
=================  ====================================================

覆盖值落在 ``data/runtime_settings.json``，重启服务后仍在。取参数时若没有覆盖，
则回落到调用方传进来的 env 默认值，所以删掉文件即完全回到 env 行为。
"""
import json
import logging
import os
import threading

log = logging.getLogger("app.runtime_settings")

_LOCK = threading.RLock()

# 与 agent/preferences.py 同一套路径推导（那里在 agent/ 下，两层上溯同为项目根）
DATA_DIR = os.environ.get(
    "ASSISTANT_KID_DATA_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
_SETTINGS_FILE = os.path.join(DATA_DIR, "runtime_settings.json")

_overrides: dict = {}

# 参数表。type 用于校验与反序列化；effect 是给站点页面展示的"何时生效"。
# choices 仅用于下拉/白名单校验，None 表示自由输入（仍需过 type 校验）。
_KEYS: dict[str, dict] = {
    "device.auto_listen": {
        "type": "bool",
        "label": "免唤醒自动开麦",
        "help": "激活完成后直接打开音频通道进入监听，不用喊唤醒词。"
                "关掉则回到唤醒词门控，更省电。",
        "effect": "设备下次开机取配置时生效（需设备重启一次）",
        "choices": None,
        "default": True,
    },
    "device.ota_url": {
        "type": "str",
        "label": "OTA 服务器地址",
        "help": "设备激活时轮询的 OTA 配置地址（默认 http://192.168.1.12:8000/xiaozhi/ota）。"
                "留空表示沿用设备已保存的地址。注意：服务器搬家必须在设备还能连上旧服务器时"
                "提前下发，服务器已经搬走的话设备拿不到新地址，需要重新烧录。",
        "effect": "设备下次激活时生效（重启设备即可）",
        "choices": None,
        "default": "",
    },
    "tts.voice": {
        "type": "str",
        "label": "音色",
        "help": "语音合成的音色。角色上单独设过音色的，以角色设置为准。",
        "effect": "立即生效（无需重启服务）",
        "choices": None,  # 由调用方从 agent.asr_tts._VOICE_MAP 注入
        # 默认值惰性读 env：改 .env 的 TTS_VOICE 不必同步改这里，避免两处漂移
        "default": lambda: os.getenv("TTS_VOICE", "Ethan"),
    },
    "tts.speed": {
        "type": "float",
        "label": "语速",
        "help": "0.5 慢 / 1.0 正常 / 2.0 快。角色上单独设过语速的，以角色设置为准。",
        "effect": "立即生效（无需重启服务）",
        "choices": None,
        "default": lambda: float(os.getenv("TTS_SPEED", "1.0")),
    },
    "tts.priority": {
        "type": "list",
        "label": "TTS 优先级",
        "help": "逗号分隔的 provider 顺序，前面的失败才轮到后面。",
        "effect": "立即生效（无需重启服务）",
        "choices": None,  # 由调用方从已注册 provider 注入
        "default": lambda: [p.strip() for p in
                            os.getenv("TTS_PRIORITY", "stepfun").split(",") if p.strip()],
    },
}


def _coerce(key: str, value):
    """把任意来源的值转成该参数声明的类型；不合法返回 (False, 错误信息)。"""
    spec = _KEYS.get(key)
    if spec is None:
        return False, f"未知参数: {key}"
    kind = spec["type"]
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return True, value
            if isinstance(value, (int, float)):
                return True, bool(value)
            if isinstance(value, str):
                s = value.strip().lower()
                if s in ("1", "true", "yes", "on"):
                    return True, True
                if s in ("0", "false", "no", "off"):
                    return True, False
            return False, f"{key} 需要布尔值"
        if kind == "float":
            v = float(value)
            if not (0.5 <= v <= 2.0):
                return False, f"{key} 取值范围 0.5 ~ 2.0"
            return True, round(v, 2)
        if kind == "list":
            if isinstance(value, str):
                items = [x.strip() for x in value.split(",")]
            elif isinstance(value, (list, tuple)):
                items = [str(x).strip() for x in value]
            else:
                return False, f"{key} 需要字符串或数组"
            return True, [x for x in items if x]
        # str
        return True, str(value).strip()
    except (TypeError, ValueError):
        return False, f"{key} 类型不合法（期望 {kind}）"


def _load() -> None:
    """启动时读一次落盘覆盖。读不到就当没有覆盖，不抛异常。"""
    global _overrides
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _overrides = {k: v for k, v in data.items() if k in _KEYS}
    except FileNotFoundError:
        _overrides = {}
    except (OSError, json.JSONDecodeError) as e:
        # 坏文件不能让服务起不来：记日志并退回到"无覆盖"
        log.warning(f"[settings] 读取 {_SETTINGS_FILE} 失败，按无覆盖处理: {e}")
        _overrides = {}


def _save() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_overrides, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SETTINGS_FILE)


def default_for(key: str):
    """参数表里声明的默认值（"没覆盖、env 也没给"时的兜底）。

    声明为可调用的表示惰性求值（读 env），改 .env 不必同步改这里。
    """
    spec = _KEYS.get(key)
    if spec is None:
        return None
    d = spec["default"]
    return d() if callable(d) else d


def overrides() -> dict:
    """当前所有覆盖（浅拷贝），用于页面显示"哪些参数被改过"。"""
    with _LOCK:
        return dict(_overrides)


def get(key: str, default=None):
    """取生效值：有覆盖返回覆盖，否则返回 default（调用方传 env 基线）。"""
    with _LOCK:
        raw = _overrides.get(key)
    if raw is None:
        return default
    ok, coerced = _coerce(key, raw)
    return coerced if ok else default


def set_value(key: str, value):
    """写入覆盖并落盘。返回 (ok, message)。"""
    global _overrides
    if key not in _KEYS:
        return False, f"未知参数: {key}"
    ok, coerced = _coerce(key, value)
    if not ok:
        return False, coerced  # 此时 coerced 是错误信息
    with _LOCK:
        _overrides[key] = coerced
        try:
            _save()
        except OSError as e:
            _overrides.pop(key, None)
            return False, f"写入 {_SETTINGS_FILE} 失败: {e}"
    return True, f"{_KEYS[key]['label']} 已设为 {coerced}"


def clear(key: str | None = None) -> None:
    """清除某个（或全部）覆盖，回到 env/代码默认值。"""
    global _overrides
    with _LOCK:
        if key is None:
            _overrides = {}
        else:
            _overrides.pop(key, None)
        try:
            _save()
        except OSError as e:
            log.warning(f"[settings] 清除覆盖落盘失败: {e}")


def schema() -> dict:
    """参数元数据（给站点页面渲染用）。不含值。"""
    return {k: dict(spec) for k, spec in _KEYS.items()}


_load()
