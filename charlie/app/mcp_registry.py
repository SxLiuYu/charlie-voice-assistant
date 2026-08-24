"""MCP server 注册表 — 动态发现（基于 __mcp_meta__ ast.parse）+ frozen 探测 + profile 合并（深 module）

从 voice_agent._build_brain 抽出。负责：
- all_mcp dict（MCP 的 command/args/cwd 配置）
- frozen/cwd 探测
- 按 mcp_set + MCP_PROFILE 解析启用的 MCP 列表
- Demo 模式（无 LLM）不启 MCP
"""
import os
import sys
import ast
import glob as _glob
import logging

log = logging.getLogger("magic")


def _read_mcp_meta(fpath: str) -> dict | None:
    """用 ast.parse 读取文件头部的 __mcp_meta__（不执行文件）"""
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == '__mcp_meta__':
                    return ast.literal_eval(node.value)
    except Exception:
        pass
    return None


def _discover_mcp_metas() -> list[dict]:
    """glob 发现所有 MCP 文件，返回 __mcp_meta__ 列表（过滤 disabled）"""
    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = []
    # 1. magic-*.py
    for fpath in sorted(_glob.glob(os.path.join(PROJECT_DIR, "magic-*.py"))):
        meta = _read_mcp_meta(fpath)
        if not meta or meta.get("disabled"):
            continue
        if not meta.get("name"):
            continue
        result.append(meta)
    # 2. 非前缀文件（白名单）
    for fname in ["baize_skills_mcp.py", "mcp_ir_control.py"]:
        fpath = os.path.join(PROJECT_DIR, fname)
        if not os.path.exists(fpath):
            continue
        meta = _read_mcp_meta(fpath)
        if not meta or meta.get("disabled"):
            continue
        if not meta.get("name"):
            continue
        result.append(meta)
    # 3. 别名（无独立文件）
    result.append({"name": "filesystem", "tier": "core", "required_env": [], "label": "文件系统"})
    result.append({"name": "amap-maps", "tier": "core", "required_env": [], "label": "高德地图/天气"})
    return result


def _build_all_mcp() -> dict:
    """glob 发现所有 MCP 文件，从 __mcp_meta__ 构建注册表"""
    _is_frozen = getattr(sys, 'frozen', False)
    _py = sys.executable
    _mcp_cwd = os.path.dirname(_py) if _is_frozen else os.getcwd()

    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = {}

    def _add_mcp(name, fname):
        if _is_frozen:
            result[name] = {"command": _py, "args": ["--mcp", name], "cwd": _mcp_cwd}
        else:
            result[name] = {"command": _py, "args": [fname], "cwd": _mcp_cwd}

    # 1. magic-*.py
    for fpath in sorted(_glob.glob(os.path.join(PROJECT_DIR, "magic-*.py"))):
        meta = _read_mcp_meta(fpath)
        if not meta or meta.get("disabled"):
            continue
        name = meta.get("name")
        if not name:
            log.warning(f"[mcp] {os.path.basename(fpath)} 缺少 __mcp_meta__.name，跳过")
            continue
        _add_mcp(name, os.path.basename(fpath))

    # 2. 非前缀文件（白名单）
    for fname in ["baize_skills_mcp.py", "mcp_ir_control.py"]:
        fpath = os.path.join(PROJECT_DIR, fname)
        if not os.path.exists(fpath):
            continue
        meta = _read_mcp_meta(fpath)
        if not meta or meta.get("disabled"):
            continue
        name = meta.get("name")
        if not name:
            continue
        _add_mcp(name, fname)

    # 3. 别名（无独立文件，复用其他 MCP 的配置）
    if "magic-notes" in result:
        result["filesystem"] = result["magic-notes"].copy()
    if "magic-info" in result:
        result["amap-maps"] = result["magic-info"].copy()

    return result


# 模块加载时构建一次（frozen 状态不变）
ALL_MCP = _build_all_mcp()


def resolve(mcp_set: str = "all") -> dict:
    """解析启用的 MCP server 配置

    Args:
        mcp_set: "none" / "all" / 单个 MCP 名

    Returns:
        {name: {command, args, cwd}} 字典（可能为空）
    """
    from app.mcp_gate import resolve_mcp_profile
    from app.llm_config import demo_mode_active

    # Demo 规则模式（无 LLM）不启 MCP
    if demo_mode_active():
        log.info("[mcp_registry] Demo 规则模式: 不启 MCP（无 LLM）")
        return {}
    if mcp_set == "none":
        return {}
    elif mcp_set == "all":
        enabled = resolve_mcp_profile()
        return {k: v for k, v in ALL_MCP.items() if k in enabled}
    else:
        return {mcp_set: ALL_MCP[mcp_set]} if mcp_set in ALL_MCP else {}
