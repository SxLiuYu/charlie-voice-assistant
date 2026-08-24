"""MCP 分层 + key 缺失自动跳过

核心 MCP（12个）：不依赖外部 key，基础能力（时间/提醒/备忘录/系统/摘要/进化/场景/文件/偏好/记忆/角色/地图）
可选 MCP（12个）：依赖各自 key 或 binary，缺失自动跳过 + warning

MCP_PROFILE 环境变量控制：
- core（默认）：仅 12 个核心
- all：核心 + 可选（key 缺失过滤后）
- custom：读 MCP_SERVERS 自定义列表
"""
import os
import logging

log = logging.getLogger("magic")


def _load_mcp_meta():
    """从 mcp_registry 的动态发现结果派生分层信息"""
    from app.mcp_registry import _discover_mcp_metas
    metas = _discover_mcp_metas()
    core = [m["name"] for m in metas if m.get("tier") == "core"]
    optional = [m["name"] for m in metas if m.get("tier") == "optional"]
    required_env = {m["name"]: m.get("required_env", []) for m in metas}
    labels = {m["name"]: m.get("label", m["name"]) for m in metas}
    return core, optional, required_env, labels


CORE_MCP_KEYS, OPTIONAL_MCP_KEYS, REQUIRED_ENV, MCP_LABELS = _load_mcp_meta()


def _is_configured(key: str) -> bool:
    """环境变量是否已配置 — 委托到 env_catalog（#5 修复：单一来源）"""
    from app.env_catalog import is_configured
    return is_configured(key)


def filter_optional_mcp(keys: list[str]) -> list[str]:
    """过滤掉 key 缺失的可选 MCP，缺失项打 warning"""
    result = []
    for k in keys:
        required = REQUIRED_ENV.get(k, [])
        missing = [r for r in required if not _is_configured(r)]
        if missing:
            log.warning(f"[mcp_gate] 跳过 {k}（缺少 {', '.join(missing)}）")
        else:
            result.append(k)
    return result


def resolve_mcp_profile() -> list[str]:
    """根据 MCP_PROFILE 环境变量解析启用的 MCP 列表

    返回顺序：核心在前，可选在后。
    Demo 规则模式的"不启 MCP"判断不在此处——由 _build_brain 调用方决定。
    """
    profile = os.getenv("MCP_PROFILE", "core").strip().lower()
    if profile == "all":
        optional = filter_optional_mcp(OPTIONAL_MCP_KEYS)
        return list(CORE_MCP_KEYS) + optional
    elif profile == "custom":
        servers = os.getenv("MCP_SERVERS", "").strip()
        if not servers:
            log.warning("[mcp_gate] MCP_PROFILE=custom 但 MCP_SERVERS 为空，回退 core")
            return list(CORE_MCP_KEYS)
        custom = [s.strip() for s in servers.split(",") if s.strip()]
        # 自定义列表里属于可选的走 key 过滤，核心直接保留
        filtered_custom = filter_optional_mcp(
            [c for c in custom if c in OPTIONAL_MCP_KEYS]
        )
        core_in_custom = [c for c in custom if c in CORE_MCP_KEYS]
        return core_in_custom + filtered_custom
    else:  # core（默认）
        return list(CORE_MCP_KEYS)
