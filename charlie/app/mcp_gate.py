"""MCP 分层 + key 缺失自动跳过

核心 MCP（8个）：不依赖外部 key，基础能力（时间/提醒/备忘录/系统/摘要/进化/场景/文件）
可选 MCP（12个）：依赖各自 key 或 binary，缺失自动跳过 + warning

MCP_PROFILE 环境变量控制：
- core（默认）：仅 8 个核心
- all：核心 + 可选（key 缺失过滤后）
- custom：读 MCP_SERVERS 自定义列表
"""
import os
import logging

log = logging.getLogger("magic")

# 核心 MCP（8个）：不依赖外部 key
CORE_MCP_KEYS = [
    "magic-info",       # 时间/天气/新闻/翻译/计算（天气需AMAP_KEY但其他不需，混合保留）
    "magic-reminder",   # 提醒/定时任务
    "magic-notes",      # 备忘录/购物清单
    "magic-system",     # 系统控制/音量/语速
    "magic-summary",    # 对话摘要
    "magic-evolution",  # 自进化
    "magic-scenes",     # 场景Protocol（内置4个不需key）
    "filesystem",       # 文件读写（=magic-notes）
]

# 可选 MCP（13个）：依赖各自 key 或外部 binary
OPTIONAL_MCP_KEYS = [
    "amap-maps",         # = magic-info（重复，可选）
    "magic-music",       # ncm binary
    "magic-life",        # ESP32_IP
    "magic-apps",        # ego-browser
    "magic-feishu",      # FEISHU_APP_ID/SECRET
    "magic-douyin",      # ego-browser
    "magic-taobao",      # ego-browser
    "magic-wardrobe",    # AMAP_KEY
    "magic-recipe",      # 本地菜谱（不需key但归可选）
    "magic-browser",     # ego-browser
    "magic-jarvis",      # 贾维斯能力（金融/环境/体育，免费无Key）
    "magic-habits",      # 习惯追踪（无key依赖）
    "baize-skills",      # TAVILY/ALIYUN
    # "ac-control",      # 已禁用：空调仅通过语音快路径控制，LLM不可自动调用
]

# 每个可选 MCP 需要的 env key（任一缺失则跳过该 MCP）
REQUIRED_ENV = {
    "magic-music": [],
    "magic-life": ["ESP32_IP"],
    "magic-apps": [],
    "magic-feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    "magic-douyin": [],
    "magic-taobao": [],
    "magic-wardrobe": ["AMAP_KEY"],
    "magic-recipe": [],
    "magic-browser": [],
    "magic-jarvis": [],
    "magic-habits": [],
    "baize-skills": [],
    "ac-control": ["TUYA_CLIENT_ID", "TUYA_ACCESS_KEY"],
    "amap-maps": [],
}


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
