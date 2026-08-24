#!/usr/bin/env python3
"""MCP Server: 红外遥控 — 通过 Tuya OpenClaw API 控制空调/电视等家电
替代旧版 ESP32 HTTP API 方案。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "ac-control",
    "tier": "optional",
    "required_env": ['TUYA_CLIENT_ID', 'TUYA_ACCESS_KEY'],
    "label": "空调控制",
    "disabled": True
}

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
    return "空调控制已禁用，请用语音指令操作（如'打开空调制冷26度')"

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
