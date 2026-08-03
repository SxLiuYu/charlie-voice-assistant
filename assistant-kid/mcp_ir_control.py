#!/usr/bin/env python3
"""MCP Server: 红外遥控 — 通过 ESP32 HTTP API 控制空调/电视等家电
ESP32 API: POST /api/ir/send  body: {"device":"ac","action":"power_on"}
"""
import os, json, urllib.request, urllib.error
from mcp.server.fastmcp import FastMCP

os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

ESP32_HOST = os.getenv("ESP32_HOST", "192.168.1.7")

mcp = FastMCP("ac-control")

def _send_ir(device: str, action: str, temperature: int = 0, fan_speed: str = "") -> str:
    """发送红外指令到 ESP32, 支持温度/风速参数"""
    url = f"http://{ESP32_HOST}/api/ir/send"
    body = {"device": device, "action": action}
    if temperature:
        body["temperature"] = temperature
    if fan_speed:
        body["fan_speed"] = fan_speed
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            if result.get("success"):
                return f"已发送{device}的{action}指令（{result.get('method','')}）"
            else:
                return f"ESP32返回失败: {result.get('error','未知')}"
    except urllib.error.HTTPError as e:
        return f"ESP32 HTTP错误 {e.code}: {e.reason}"
    except Exception as e:
        return f"ESP32连接失败: {e}"

@mcp.tool()
def ac_control(action: str, temperature: int = 0, fan_speed: str = "") -> str:
    """通过红外控制空调（开关、模式、温度、风速）。
    参数:
    - action: on=开机 off=关机 cool=制冷 heat=制热 fan=送风 dry=除湿 auto=自动
    - temperature: 目标温度 (16-30), 留空=不调温
    - fan_speed: 风速 auto/low/medium/high, 留空=不调风速
    """
    # 映射到 ESP32 预编码命令
    action_map = {
        "on": "power_on", "off": "power_off",
        "cool": "cool", "heat": "heat",
        "fan": "fan", "dry": "dry", "auto": "auto",
    }
    esp32_action = action_map.get(action.lower(), action.lower())
    return _send_ir("ac", esp32_action, temperature=temperature, fan_speed=fan_speed)

@mcp.tool()
def tv_control(action: str) -> str:
    """通过红外控制小米电视。
    参数:
    - action: power=开关机 volume_up=音量+ volume_down=音量- channel_up=频道+ channel_down=频道- home=主页 input_source=信号源
    """
    return _send_ir("tv", action.lower())

if __name__ == "__main__":
    mcp.run()
