#!/usr/bin/env python3
"""MCP Server: 红外遥控 — 通过 Tuya OpenClaw API 控制空调/电视等家电
替代旧版 ESP32 HTTP API 方案。
"""
import os, json
from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

from tuya_api import TuyaAPI

AC_DEVICE_ID = os.getenv("TUYA_AC_DEVICE_ID", "")   # 空调遥控器 remote_id
IR_DEVICE_ID = os.getenv("TUYA_IR_DEVICE_ID", "")    # 红外网关 infrared_id

mcp = FastMCP("ac-control")

# mode 映射 (2B scenes: 0制冷/1制热/2自动/3送风/4除湿, 与2C不同)
MODE_2B = {
    "cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4,
}
# 风扇映射 (2B wind: 0自动/1低/2中/3高)
FAN_MAP = {
    "auto": 0, "low": 1, "medium": 2, "high": 3,
}

def _get_api():
    log.debug("[ir] 初始化Tuya API")
    try:
        from tuya_api import TuyaCloudAPI
        return TuyaCloudAPI()
    except ValueError:
        raise ValueError("TUYA_CLIENT_ID/TUYA_ACCESS_KEY (2B) 未配置")

@mcp.tool()
def ac_control(action: str, temperature: int = 0, fan_speed: str = "") -> str:
    """[已禁用] 空调控制仅支持语音指令"""
    log.warning(f"[ir] 空调控制已被禁用(仅语音可用): {action}")
    return "空调控制已禁用，请用语音指令操作（如'打开空调制冷26度'）"
    else:
        return f"不支持的动作: {action}"
    eff_temp = max(16, min(30, temperature if temperature > 0 else 26))
    wind = None
    if fan_speed:
        fs = fan_speed.lower()
        if fs in FAN_MAP:
            wind = FAN_MAP[fs]
        else:
            return f"不支持的风速: {fan_speed}"
    try:
        api.ac_scenes_command(IR_DEVICE_ID, AC_DEVICE_ID, power=power, mode=mode, temp=eff_temp, wind=wind)
        log.info(f"[ir] 空调控制成功: {action} {eff_temp}度 风速={fan_speed or '自动'}")
        return f"✅ 空调已{action}，温度 {eff_temp}°C" + (f"，风速 {fan_speed}" if fan_speed else "")
    except Exception as e:
        log.error(f"[ir] 空调控制失败: {e}")
        return f"❌ 控制失败: {e}"

@mcp.tool()
def ac_status() -> str:
    log.info("[ir] 查询空调状态")
    """查询空调当前状态（开关、模式、温度、风速）"""
    api = _get_api()
    try:
        s = api.ac_status(IR_DEVICE_ID, AC_DEVICE_ID)
        power = "开" if str(s.get("power")) == "1" else "关"
        mode_map = {"0": "制冷", "1": "制热", "2": "自动", "3": "送风", "4": "除湿"}
        mode = mode_map.get(str(s.get("mode", "")), "未知")
        temp = s.get("temp", "?")
        fan_map = {"0": "自动", "1": "低风", "2": "中风", "3": "高风"}
        fan = fan_map.get(str(s.get("wind", "")), "?")
        log.debug(f"[ir] 空调状态: {power} {mode} {temp}度 {fan}")
        return f"空调状态: {power}，模式: {mode}，温度: {temp}°C，风速: {fan}"
    except Exception as e:
        log.error(f"[ir] 查询空调状态失败: {e}")
        return f"❌ 查询失败: {e}"

if __name__ == "__main__":
    mcp.run()
