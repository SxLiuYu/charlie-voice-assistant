"""用户偏好管理 MCP — 让 LLM 能记住和读取用户偏好"""
from mcp.server.fastmcp import FastMCP
import logging

log = logging.getLogger("magic")
mcp = FastMCP("magic-preferences")

# --- MCP 元数据（供 mcp_registry 自动发现）---
__mcp_meta__ = {
    "name": "magic-preferences",
    "tier": "core",
    "required_env": [],
    "label": "用户偏好",
}


@mcp.tool()
def set_pref(key: str, value: str) -> str:
    """记住用户偏好（如 sleep_time=22:00, sleep_ac_action=leave_alone）"""
    from agent.preferences import set_preference
    set_preference(key, value)
    log.info(f"[pref] set {key}={value}")
    return f"已记住：{key}={value}"


@mcp.tool()
def get_pref(key: str) -> str:
    """查询用户偏好"""
    from agent.preferences import get_preference
    value = get_preference(key)
    if value:
        return f"{key}={value}"
    return f"未找到偏好：{key}"


@mcp.tool()
def forget_pref(key: str) -> str:
    """删除用户偏好（用户说'以后不用...'时调用）"""
    from agent.preferences import del_preference
    if del_preference(key):
        return f"已忘记：{key}"
    return f"未找到偏好：{key}"


@mcp.tool()
def list_prefs() -> str:
    """列出所有用户偏好"""
    from agent.preferences import list_preferences
    prefs = list_preferences()
    if not prefs:
        return "暂无偏好记录"
    return "\n".join(f"{k}={v}" for k, v in prefs.items())


if __name__ == "__main__":
    mcp.run()
