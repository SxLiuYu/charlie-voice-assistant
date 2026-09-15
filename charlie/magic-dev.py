"""代码开发 MCP 模块 — 允许 LLM 通过飞书对话读写项目文件。

安全控制：
1. DEV_MODE_ENABLED=1 环境变量门控（默认关闭）
2. 路径白名单：只允许项目根目录内，排除 .env/.git/__pycache__ 等
3. 写操作必须经过二次确认（pending_confirmation）
4. 用户白名单：FEISHU_DEV_OPEN_IDS 环境变量（逗号分隔）
5. audit_log 全量审计
"""
import os, re, json, logging, difflib, time
from pathlib import Path
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("magic")

__mcp_meta__ = {
    "name": "magic-dev",
    "tier": "optional",
    "required_env": ["DEV_MODE_ENABLED"],
    "label": "代码开发",
}

mcp = FastMCP("magic-dev")

# 项目根目录：magic-dev.py 所在目录（charlie/）
PROJECT_ROOT = Path(__file__).resolve().parent

# 禁止访问的路径模式
_BLOCKED_PATTERNS = [
    re.compile(r'\.env($|\.)'),
    re.compile(r'\.git($|/|\\)'),
    re.compile(r'__pycache__'),
    re.compile(r'\.pyc$'),
    re.compile(r'\.key$'),
    re.compile(r'\.pem$'),
    re.compile(r'credentials'),
    re.compile(r'secret'),
]

# 开发用户白名单（逗号分隔的 open_id）
_DEV_OPEN_IDS = set(
    filter(None, os.getenv("FEISHU_DEV_OPEN_IDS", "").split(","))
)


def _check_dev_access(sender_id: str = "") -> tuple[bool, str]:
    """检查是否有开发操作权限。"""
    if os.getenv("DEV_MODE_ENABLED") != "1":
        return False, "开发模式未启用（DEV_MODE_ENABLED 未设置）"
    if _DEV_OPEN_IDS:
        if sender_id not in _DEV_OPEN_IDS:
            return False, "sender_id 不在开发用户白名单"
        return True, ""
    log.warning("[magic-dev] DEV_MODE_ENABLED=1 但 FEISHU_DEV_OPEN_IDS 未配置，允许所有用户（建议配置白名单）")
    return True, ""


def _is_safe_path(path: str) -> tuple[bool, str]:
    """检查路径是否在允许范围内。"""
    try:
        full_path = (PROJECT_ROOT / path).resolve()
        full_path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False, f"路径越界：{path} 不在项目目录内"
    
    path_str = str(full_path)
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(path_str):
            return False, f"路径被禁止访问：{path}"
    
    return True, ""


def _try_import_decisions():
    """尝试导入 magic-decisions 模块（文件名带横线）。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_decisions_dev",
            str(PROJECT_ROOT / "magic-decisions.py")
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception as e:
        log.debug(f"[magic-dev] 导入 magic-decisions 失败: {e}")
    return None


@mcp.tool()
def list_project_files(subdir: str = "") -> str:
    """列出项目文件树。
    
    Args:
        subdir: 相对项目根目录的子目录（如 "agent/"、"app/routes/"），留空表示根目录
    
    Returns:
        缩进的文件/目录列表
    """
    try:
        target = (PROJECT_ROOT / subdir).resolve()
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        return f"错误：路径越界 {subdir}"
    
    if not target.exists():
        return f"错误：目录不存在 {subdir}"
    
    lines = []
    try:
        for item in sorted(target.iterdir()):
            rel = item.relative_to(PROJECT_ROOT)
            indent = "  " * len(rel.parts[:-1])
            prefix = "[DIR] " if item.is_dir() else "      "
            lines.append(f"{indent}{prefix}{item.name}/" if item.is_dir() else f"{indent}{item.name}")
    except PermissionError:
        return f"错误：无权限读取 {subdir}"
    
    return "\n".join(lines) if lines else f"{subdir or '/'} 目录为空"


@mcp.tool()
def read_file(path: str) -> str:
    """读取项目文件内容。
    
    Args:
        path: 相对项目根目录的文件路径（如 "agent/llm.py"、"app/routes/feishu.py"）
    
    Returns:
        文件内容（最多 500 行）
    """
    ok, msg = _is_safe_path(path)
    if not ok:
        return f"错误：{msg}"
    
    full_path = (PROJECT_ROOT / path).resolve()
    if not full_path.is_file():
        return f"错误：文件不存在 {path}"
    
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        if len(lines) > 500:
            content = "\n".join(lines[:500]) + f"\n\n... (共 {len(lines)} 行，已截断)"
        return content
    except Exception as e:
        return f"错误：读取文件失败 {e}"


@mcp.tool()
def edit_file(path: str, old_text: str, new_text: str, sender_id: str = "") -> str:
    """精确替换文件内容（非全量覆盖）。
    
    先记录修改请求，需要用户在飞书回复"确认"后才真正写盘。
    
    Args:
        path: 相对项目根目录的文件路径
        old_text: 要被替换的原文片段（精确匹配）
        new_text: 替换后的新内容
        sender_id: 飞书用户 open_id（用于二次确认）
    
    Returns:
        操作结果说明
    """
    # 1. 检查开发权限
    ok, msg = _check_dev_access(sender_id)
    if not ok:
        return f"错误：{msg}"
    
    # 2. 检查路径安全
    path_ok, path_msg = _is_safe_path(path)
    if not path_ok:
        return f"错误：{path_msg}"
    
    full_path = (PROJECT_ROOT / path).resolve()
    if not full_path.is_file():
        return f"错误：文件不存在 {path}"
    
    # 3. 读取当前内容
    try:
        current_content = full_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"错误：读取文件失败 {e}"
    
    # 4. 检查 old_text 是否精确匹配
    if old_text not in current_content:
        # 尝试模糊匹配提示
        close = difflib.get_close_matches(old_text, current_content.splitlines(), n=3, cutoff=0.6)
        hint = f"\n  相近的行：{', '.join(close)}" if close else ""
        return f"错误：原文片段未找到（请确保精确匹配，包括缩进和换行）{hint}"
    
    # 5. 生成 diff 预览
    new_content = current_content.replace(old_text, new_text, 1)
    diff = list(difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    diff_text = "".join(diff)
    
    # 截断过长的 diff
    if len(diff_text) > 3000:
        diff_text = diff_text[:3000] + "\n\n... (diff 过长，已截断)"
    
    # 6. 写入 pending_confirmation 等待用户确认
    try:
        decisions = _try_import_decisions()
        if decisions:
            rule_id = f"code_edit_{int(time.time())}"
            confirm_text = f"代码修改确认\n路径：{path}\n\n{diff_text}\n\n回复「确认」执行，回复「取消」放弃。"
            decisions.set_pending_confirmation(
                rule_id=rule_id,
                text=confirm_text,
                user_id=sender_id,
                extra_data={"path": path, "old_text": old_text, "new_text": new_text},
            )
            return f"修改已记录，请在飞书回复「确认」执行或「取消」放弃。\n\n{diff_text[:500]}"
    except Exception as e:
        log.warning(f"[magic-dev] 写入 pending_confirmation 失败: {e}")
    
    return "错误：无法创建确认请求，请稍后重试"


def _write_edit(path: str, old_text: str, new_text: str) -> str:
    """内部函数：执行实际的文件写入（用于用户确认后的执行）。
    
    Args:
        path: 相对项目根目录的文件路径
        old_text: 要被替换的原文片段
        new_text: 替换后的新内容
    
    Returns:
        操作结果说明
    """
    # 路径检查（复用安全逻辑）
    path_ok, path_msg = _is_safe_path(path)
    if not path_ok:
        return f"错误：{path_msg}"
    
    full_path = (PROJECT_ROOT / path).resolve()
    if not full_path.is_file():
        return f"错误：文件不存在 {path}"
    
    # 读取当前内容
    try:
        current_content = full_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"错误：读取文件失败 {e}"
    
    # 检查 old_text 是否精确匹配
    if old_text not in current_content:
        return "错误：原文片段未找到（文件可能已被修改）"
    
    # 执行替换
    new_content = current_content.replace(old_text, new_text, 1)
    
    # 写入文件
    try:
        full_path.write_text(new_content, encoding="utf-8")
        # audit_log
        try:
            from app.audit_log import audit_log
            audit_log(
                feature="mcp:edit_file",
                input_data={"path": path, "old_text_len": len(old_text), "new_text_len": len(new_text)},
                output_data={"status": "success"},
                success=True,
            )
        except Exception:
            pass
        return f"✅ 已修改 {path}（替换 {len(old_text)} 字节 → {len(new_text)} 字节）"
    except Exception as e:
        return f"错误：写入文件失败 {e}"

