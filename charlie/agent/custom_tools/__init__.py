"""自定义 MCP 工具加载器

用户只需在 custom_tools/ 目录下创建 Python 模块, 即可添加自定义工具。
模块通过 __mcp_meta__ 声明元数据, @mcp.tool() 装饰器注册工具。

用法:
  1. 在 custom_tools/ 目录下创建 my_tool.py:
       __mcp_meta__ = {"name": "my-tool", "tier": "custom", "label": "我的工具"}

       from mcp.server.fastmcp import FastMCP
       mcp = FastMCP("my-tool")

       @mcp.tool()
       def hello(name: str) -> str:
           '''Say hello'''
           return f"Hello, {name}!"

  2. 系统启动时自动发现并注册 (custom_tools.loader 自动执行)
  3. 在对话中可通过 MCP 调用这些工具

设计:
  - 错误隔离: 单个工具模块加载失败不影响其他模块
  - 元数据驱动: 通过 __mcp_meta__ 控制启用/禁用
  - stdio + in-process 双模式兼容
"""
import os
import sys
import ast
import glob
import logging
import importlib
import importlib.util
import threading
from typing import Optional

log = logging.getLogger("magic")

# custom_tools 目录路径 (本文件就在 custom_tools/ 目录内)
_CUSTOM_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

# 已加载的自定义模块缓存
_loaded_modules: dict[str, object] = {}
_load_lock = threading.Lock()


def _load_mcp_meta(fpath: str) -> Optional[dict]:
    """用 ast.parse 读取 __mcp_meta__ (不执行文件)。"""
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == '__mcp_meta__':
                    return ast.literal_eval(node.value)
    except Exception as e:
        log.debug(f"[custom_tools] 读取元数据失败 {fpath}: {e}")
    return None


def discover_custom_tools() -> list[dict]:
    """扫描 custom_tools/ 目录, 返回所有可用的自定义工具元数据。"""
    results = []
    if not os.path.isdir(_CUSTOM_TOOLS_DIR):
        return results
    for fpath in sorted(glob.glob(os.path.join(_CUSTOM_TOOLS_DIR, "*.py"))):
        fname = os.path.basename(fpath)
        if fname.startswith("_") or fname.startswith("."):
            continue
        meta = _load_mcp_meta(fpath)
        if not meta:
            log.debug(f"[custom_tools] {fname} 缺少 __mcp_meta__, 跳过")
            continue
        if meta.get("disabled"):
            log.debug(f"[custom_tools] {fname} 已禁用, 跳过")
            continue
        name = meta.get("name")
        if not name:
            log.warning(f"[custom_tools] {fname} 缺少 name, 跳过")
            continue
        meta["_filepath"] = fpath
        meta["_fname"] = fname
        results.append(meta)
    return results


def load_custom_tool(fpath: str) -> Optional[object]:
    """加载单个自定义工具模块, 返回模块对象。"""
    fname = os.path.basename(fpath)
    module_name = f"custom_tools.{fname[:-3]}"

    with _load_lock:
        if module_name in _loaded_modules:
            return _loaded_modules[module_name]

    try:
        # 确保 custom_tools 包在 sys.modules 中
        if "custom_tools" not in sys.modules:
            pkg = importlib.util.module_from_spec(
                importlib.util.spec_from_file_location("custom_tools",
                    os.path.join(_CUSTOM_TOOLS_DIR, "__init__.py"))
            )
            sys.modules["custom_tools"] = pkg
            pkg.__path__ = [_CUSTOM_TOOLS_DIR]
            pkg.__package__ = "custom_tools"

        spec = importlib.util.spec_from_file_location(module_name, fpath)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        with _load_lock:
            _loaded_modules[module_name] = mod
        log.info(f"[custom_tools] 已加载: {fname}")
        return mod
    except Exception as e:
        log.warning(f"[custom_tools] 加载失败 {fname}: {e}")
        return None


def load_all_custom_tools() -> dict[str, object]:
    """加载所有启用的自定义工具, 返回 {name: module} 映射。"""
    discovered = discover_custom_tools()
    loaded = {}
    for meta in discovered:
        fpath = meta["_filepath"]
        name = meta["name"]
        mod = load_custom_tool(fpath)
        if mod is not None:
            loaded[name] = mod
    return loaded


def get_custom_tool_names() -> list[str]:
    """获取所有可用的自定义工具名称列表。"""
    return [m["name"] for m in discover_custom_tools()]


def register_custom_tools_to_mcp_registry() -> dict:
    """将自定义工具注册到 MCP 注册表, 返回 {name: {command, args}} 字典。

    这种方式让自定义工具可以通过 stdio 子进程模式运行 (与 magic-*.py 一致)。
    """
    discovered = discover_custom_tools()
    result = {}
    _py = sys.executable
    _mcp_cwd = os.getcwd()
    for meta in discovered:
        fpath = meta["_filepath"]
        name = meta["name"]
        result[name] = {
            "command": _py,
            "args": [os.path.basename(fpath)],
            "cwd": _CUSTOM_TOOLS_DIR,
            "env": dict(os.environ),
        }
    if result:
        log.info(f"[custom_tools] 注册 {len(result)} 个工具到 MCP 注册表")
    return result


# ===== 启动时自动加载 =====

def auto_register() -> dict:
    """启动时自动发现 + 加载所有自定义工具。
    返回加载的模块映射。

    在 app/lifespan.py 中调用:
      from agent.custom_tools import auto_register
      custom_mcp = auto_register()
    """
    discovered = discover_custom_tools()
    if not discovered:
        log.debug("[custom_tools] 目录为空或不存在，跳过")
        return {}

    loaded = load_all_custom_tools()
    # 同时注册到 MCP 注册表 (用于 stdio 模式)
    _mcp_reg = register_custom_tools_to_mcp_registry()

    log.info(f"[custom_tools] 启动注册完成: {len(loaded)} 个工具已加载")
    return loaded


# ===== 便捷入口: 用户创建 custom_tools/example.py 即可使用 =====

# 在 custom_tools/ 目录下创建一个示例工具 (如果不存在)
_EXAMPLE_TOOL = '''"""示例自定义工具

用法: 在对话中说 "hello 名字" 或直接通过 MCP 调用。
删除此文件即可移除示例工具。
"""
__mcp_meta__ = {
    "name": "hello-tool",
    "tier": "custom",
    "required_env": [],
    "label": "问候工具 (示例)",
}

from mcp.server.fastmcp import FastMCP
import os
import logging

log = logging.getLogger("magic")
mcp = FastMCP("hello-tool")


@mcp.tool()
def hello(name: str = "世界") -> str:
    """向指定名字打招呼。

    参数:
      name: 要问候的名字 (默认: 世界)

    例: hello("小明") → "你好，小明！"
    """
    return f"你好，{name}！我是 Charlie 的自定义工具。"


@mcp.tool()
def echo(text: str) -> str:
    """回声工具: 原样返回输入文本。

    参数:
      text: 要回声的文本

    例: echo("测试") → "测试"
    """
    return text
'''

# Example tool file path
_EXAMPLE_PATH = os.path.join(_CUSTOM_TOOLS_DIR, "example.py")


def _ensure_example():
    """如果 custom_tools 目录为空, 创建示例工具。"""
    if not os.path.exists(_EXAMPLE_PATH):
        try:
            with open(_EXAMPLE_PATH, 'w', encoding='utf-8') as f:
                f.write(_EXAMPLE_TOOL)
            log.info(f"[custom_tools] 已创建示例工具: {_EXAMPLE_PATH}")
        except Exception:
            pass


# 模块导入时: 创建示例 (仅当目录为空时)
_py_files = [f for f in os.listdir(_CUSTOM_TOOLS_DIR) if f.endswith('.py') and f != '__init__.py'] if os.path.isdir(_CUSTOM_TOOLS_DIR) else []
if not _py_files:
    _ensure_example()
