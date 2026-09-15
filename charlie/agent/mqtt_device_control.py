"""MQTT 统一设备控制

将现有 device_control (Tuya AC) 与 MQTT 设备发现/控制统一在一个接口下:
  - ESP32 (xiaozhi): 已有 mqtt_server 处理语音 + TTS
  - Tasmota/Sonoff/Shelly (MQTT): 通用 on/off/brightness/color
  - Tuya IR AC: 现有 device_control 直接调用

设计原则:
  - 不替换现有 device_control.py, 在其上层做适配
  - MQTT 设备通过 topic 前缀区分类型
  - 设备发现: 扫描 MQTT topic 树, 分类设备
  - 状态同步: 设备状态变更通过 MQTT 广播, 供跨设备同步

MCP 工具暴露:
  - list_devices() — 发现所有设备
  - control_device(device, action, params) — 控制设备
  - device_status(device) — 查询设备状态
"""

import json
import time
import threading
import logging
import os
from typing import Optional

log = logging.getLogger("magic")

# ===== MQTT 设备类型与 topic 映射 =====

_DEVICE_TOPIC_PREFIXES = {
    "tasmota": "tasmota/",
    "sonoff": "sonoff/",
    "shelly": "shellies/",
    "homeassistant": "homeassistant/",
}

_DEVICE_DISCOVERY_PREFIXES = [
    "tasmota/discovery/",
    "homeassistant/",
    "shellies/",
]

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mqtt-devices")


def _get_mqtt_client():
    """获取 MQTT client (惰性导入, 避免循环依赖)。"""
    try:
        from app.mqtt_server import get_server
        server = get_server()
        if server and server._client:
            return server._client
    except Exception as e:
        log.debug(f"[mqtt_devices] 获取 MQTT client 失败: {e}")
    return None


# ===== 设备注册表 =====

_devices_lock = threading.Lock()
_devices: dict[str, dict] = {}
_last_scan_ts: float = 0
_SCAN_INTERVAL = 300  # 5 分钟扫描一次


def _register_device(device_id: str, device_type: str, name: str, capabilities: list,
                     topic_prefix: str = "") -> None:
    """注册/更新设备到本地注册表。"""
    with _devices_lock:
        _devices[device_id] = {
            "id": device_id,
            "type": device_type,
            "name": name,
            "capabilities": capabilities,
            "topic_prefix": topic_prefix,
            "last_seen": time.time(),
            "online": True,
        }


def _mark_offline(device_id: str) -> None:
    """标记设备离线 (超过 2 分钟无消息视为离线)。"""
    with _devices_lock:
        if device_id in _devices:
            _devices[device_id]["online"] = False
            _devices[device_id]["last_seen"] = time.time()


def scan_devices() -> list[dict]:
    """扫描 MQTT topic 树, 发现并分类所有智能设备。"""
    global _last_scan_ts
    results = []
    client = _get_mqtt_client()
    now = time.time()
    scan_needed = (now - _last_scan_ts) > _SCAN_INTERVAL

    # 1. MQTT 设备发现
    if client and scan_needed:
        _scan_mqtt_topics(client)
        _last_scan_ts = now

    # 2. ESP32 设备 (来自 mqtt_server)
    try:
        from app.mqtt_server import get_server
        server = get_server()
        if server:
            with server._sessions_lock:
                esp32_ids = list(server._sessions.keys())
            for did in esp32_ids:
                _register_device(did, "esp32_xiaozhi", f"ESP32-{did[:8]}",
                                 ["voice", "tts", "notify"], "charlie/esp32/")
    except Exception:
        pass

    # 3. 已有 Tuya AC
    try:
        from agent.device_control import get_ac_status
        _register_device("tuya_ac", "tuya_ac", "空调(Tuya IR)",
                         ["power", "mode", "temp", "fan"], "tuya/")
    except Exception:
        pass

    # 4. 清理离线设备 + 返回
    with _devices_lock:
        stale_threshold = now - 120  # 2分钟无消息算离线
        offline_ids = [did for did, dev in _devices.items()
                       if dev["last_seen"] < stale_threshold]
        for did in offline_ids:
            _devices[did]["online"] = False

        results = [
            {
                "id": dev["id"],
                "type": dev["type"],
                "name": dev["name"],
                "online": dev["online"],
                "capabilities": dev["capabilities"],
            }
            for dev in _devices.values()
        ]
    return sorted(results, key=lambda d: d["id"])


def _scan_mqtt_topics(client) -> int:
    """通过订阅/重发扫描 MQTT topic 发现设备。返回发现设备数。"""
    if not client:
        return 0

    # 订阅 HA 和 Tasmota 发现 topic
    for prefix in _DEVICE_DISCOVERY_PREFIXES:
        try:
            client.subscribe(f"{prefix}#", qos=0)
        except Exception as e:
            log.debug(f"[mqtt_devices] 订阅 {prefix}# 失败: {e}")

    # 实时扫描: 通过已订阅的 topic 推断设备
    for prefix in ("stat/", "tele/", "shellies/"):
        try:
            client.subscribe(f"{prefix}#", qos=0)
        except Exception:
            pass

    return 0


def control_device(device_id: str, action: str, params: dict = None) -> dict:
    """控制指定设备(公开 API, 供内部 + 外部调用)。"""
    params = params or {}
    device_type = _get_device_type(device_id)

    if device_type == "esp32_xiaozhi":
        return _control_esp32(device_id, action, params)
    elif device_type == "tuya_ac":
        return _control_tuya_ac(action, params)
    elif device_type and device_type.startswith("mqtt_"):
        return _control_mqtt_device(device_id, device_type, action, params)
    else:
        # 回退: 尝试通过 MQTT 推送
        return _control_mqtt_device(device_id, "unknown", action, params)


def _get_device_type(device_id: str) -> Optional[str]:
    """获取设备类型。"""
    with _devices_lock:
        dev = _devices.get(device_id)
        if dev:
            return dev["type"]
    # 推断
    if device_id.startswith("esp32") or device_id.startswith("mqtt-"):
        return "esp32_xiaozhi"
    if device_id == "tuya_ac":
        return "tuya_ac"
    return None


def _control_esp32(device_id: str, action: str, params: dict) -> dict:
    """控制 ESP32 设备。"""
    try:
        from app.mqtt_server import get_server
        server = get_server()
        if not server or not server.is_connected():
            return {"ok": False, "message": "ESP32 未连接"}

        topic = server._get_publish_topic_for_device(device_id)
        if not topic:
            topic = f"charlie/esp32/{device_id}/down"

        if action == "notify":
            text = params.get("text", "")
            server._publish_to_device(device_id, json.dumps({
                "type": "notification", "text": text
            }))
            return {"ok": True, "message": f"已推送到 ESP32: {text[:30]}"}

        elif action == "tts":
            text = params.get("text", "")
            server._publish_to_device(device_id, json.dumps({
                "type": "tts", "state": "start", "text": text, "voice": "zh-CN"
            }))
            return {"ok": True, "message": f"TTS 已推送到 ESP32: {text[:30]}"}

        else:
            return {"ok": False, "message": f"ESP32 不支持动作: {action}"}
    except Exception as e:
        return {"ok": False, "message": f"控制失败: {e}"}


def _control_tuya_ac(action: str, params: dict) -> dict:
    """控制 Tuya 红外空调 (复用现有 device_control)。"""
    try:
        from agent.device_control import direct_ac_control
        text = _build_ac_command_text(action, params)
        if not text:
            return {"ok": False, "message": "无法解析空调指令"}
        result = direct_ac_control(text)
        return {"ok": bool(result), "message": result or "空调指令已发送"}
    except Exception as e:
        return {"ok": False, "message": f"空调控制失败: {e}"}


def _build_ac_command_text(action: str, params: dict) -> str:
    """将结构化 action/params 转换为空调控制自然语言。"""
    if action in ("turn_on", "turn_off"):
        return "空调" + ("开" if action == "turn_on" else "关")
    parts = []
    if "mode" in params:
        mode_map = {"cool": "制冷", "heat": "制热", "dry": "除湿", "fan": "送风", "auto": "自动"}
        parts.append(mode_map.get(params["mode"], params["mode"]))
    if "temp" in params:
        parts.append(f"{params['temp']}度")
    if "fan" in params:
        parts.append({"low": "低风", "medium": "中风", "high": "高风"}.get(params["fan"], params["fan"]))
    return "空调" + "".join(parts)


def _control_mqtt_device(device_id: str, device_type: str, action: str, params: dict) -> dict:
    """通过 MQTT 控制通用智能设备 (Tasmota/Sonoff/Shelly)。"""
    client = _get_mqtt_client()
    if not client:
        return {"ok": False, "message": "MQTT 未连接"}

    # 确定 control topic
    base_type = device_type.replace("mqtt_", "") if device_type.startswith("mqtt_") else device_type
    prefix = _DEVICE_TOPIC_PREFIXES.get(base_type, f"charlie/device/{device_id}/")

    # 构建 payload
    payload = "ON" if action in ("turn_on", "on") else \
              "OFF" if action in ("turn_off", "off") else \
              json.dumps({action: params}, ensure_ascii=False)

    cmnd_topic = f"{prefix}cmnd/{action}"
    try:
        client.publish(cmnd_topic, payload, qos=1)
        return {"ok": True, "message": f"MQTT 指令已发送: {cmnd_topic} = {payload[:50]}", "topic": cmnd_topic}
    except Exception as e:
        return {"ok": False, "message": f"MQTT 发布失败: {e}"}


def get_device_status(device_id: str) -> dict:
    """查询指定设备的状态。"""
    dev_type = _get_device_type(device_id)
    if dev_type == "tuya_ac":
        try:
            from agent.device_control import get_ac_status
            return get_ac_status()
        except Exception as e:
            return {"error": str(e)}
    elif dev_type == "esp32_xiaozhi":
        return {
            "id": device_id,
            "type": "esp32_xiaozhi",
            "online": True,
            "status": "voice assistant active",
        }
    else:
        # 尝试 MQTT 状态 topic
        base_type = dev_type.replace("mqtt_", "") if dev_type and dev_type.startswith("mqtt_") else (dev_type or "unknown")
        prefix = _DEVICE_TOPIC_PREFIXES.get(base_type, f"charlie/device/{device_id}/")
        stat_topic = f"{prefix}stat/RESULT"
        return {
            "id": device_id,
            "type": base_type,
            "status_topic": stat_topic,
            "note": "需要从 MQTT stat topic 读取",
        }


# ===== MCP 工具 =====

def create_mcp_tools():
    """创建设备控制 MCP 工具集合 (返回 list of (name, func, doc))。"""
    tools = []

    @mcp.tool()
    def list_devices() -> str:
        """列出所有已发现的智能设备及其状态。

        例: list_devices() → 返回设备列表
        """
        devices = scan_devices()
        if not devices:
            return "未发现任何智能设备。请确保 MQTT broker 已连接且设备已注册。"
        lines = [f"发现 {len(devices)} 个设备："]
        for d in devices:
            status = "在线" if d["online"] else "离线"
            caps = "/".join(d["capabilities"])
            lines.append(f"  [{d['id']}] {d['name']} ({d['type']}) [{status}] 能力: {caps}")
        return "\n".join(lines)

    @mcp.tool()
    def mqtt_device_control(device_id: str, action: str, params: str = "") -> str:
        """控制智能设备。

        参数:
          device_id: 设备 ID (用 list_devices 查看)
          action: 动作 (turn_on/turn_off/set_temp/set_mode/set_brightness/notify/tts)
          params: JSON 字符串 (可选), 如 '{"temp": 26, "mode": "cool"}'

        例: mqtt_device_control("tuya_ac", "turn_on")
            mqtt_device_control("esp32-abc", "notify", "{\"text\": \"该开会了\"}")
            mqtt_device_control("tasmota_plug_1", "turn_on")
        """
        params_dict = {}
        if params:
            try:
                params_dict = json.loads(params)
            except json.JSONDecodeError:
                return f"参数解析失败: {params}"
        result = control_device(device_id, action, params_dict)
        status = "成功" if result.get("ok") else "失败"
        msg = result.get("message", "")
        log.info(f"[mqtt_devices] {device_id}.{action} → {status}: {msg[:50]}")
        return f"[{status}] {msg}"

    @mcp.tool()
    def device_status(device_id: str) -> str:
        """查询设备状态。

        例: device_status("tuya_ac")
        """
        status = get_device_status(device_id)
        return json.dumps(status, ensure_ascii=False, indent=2)

    return tools
