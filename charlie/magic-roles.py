"""角色切换 MCP — 让 LLM 能通过语音指令切换角色（charlie / jarvis / baize）"""
from mcp.server.fastmcp import FastMCP
import logging

log = logging.getLogger("magic")
mcp = FastMCP("magic-roles")

# --- MCP 元数据（供 mcp_registry 自动发现）---
__mcp_meta__ = {
    "name": "magic-roles",
    "tier": "core",
    "required_env": [],
    "label": "角色切换",
}


@mcp.tool()
def switch_role(role_id: str) -> str:
    """切换助手角色。

    可用角色：
    - charlie：默认助手，专业、高效、友好
    - jarvis：钢铁侠风格，冷静、机智、略带幽默
    - baize：中国古代神兽，智慧、博学、略带古风

    示例：
    - "切换到贾维斯模式" → switch_role("jarvis")
    - "变成白泽" → switch_role("baize")
    - "回到默认模式" → switch_role("charlie")
    """
    from agent.roles import switch_role
    ok, msg = switch_role(role_id)
    log.info(f"[roles] LLM 切换角色: {role_id} -> {ok}")
    return msg if ok else f"切换失败：{msg}"


@mcp.tool()
def list_roles() -> str:
    """列出所有可用角色及当前角色"""
    from agent.roles import get_all_roles, get_current_role
    current = get_current_role()
    roles = get_all_roles()
    lines = [f"当前角色：{current}"]
    for rid, r in roles.items():
        marker = " ← 当前" if rid == current else ""
        lines.append(f"- {rid}: {r['name']} - {r['description']}{marker}")
    return "\n".join(lines)


@mcp.tool()
def get_role_info(role_id: str) -> str:
    """获取指定角色的详细信息"""
    from agent.roles import get_role
    role = get_role(role_id)
    if not role:
        return f"未知角色：{role_id}，可用角色：charlie, jarvis, baize"
    return f"""角色：{role['name']} ({role_id})
描述：{role['description']}
TTS 音色：{role.get('tts_voice', 'Ethan')}
语速：{role.get('tts_speed', 1.0)}
唤醒词：{', '.join(role.get('wake_words', []))}"""


if __name__ == "__main__":
    mcp.run()
