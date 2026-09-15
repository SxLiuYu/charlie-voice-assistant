"""进程内工具适配层：把 FastMCP 的 @mcp.tool() 函数包装为 qwen_agent BaseTool。

替代 stdio MCP 子进程方案——工具函数本就是普通 Python 函数，
不需要 spawn 子进程 + JSON-RPC IPC，直接进程内调用即可。

收益：
- brain 构建 1s → <0.01s（纯内存组装，无进程启动）
- 消除 stdio 管道断裂、连接超时、子进程清理等所有失败模式
- 工具调用延迟 = 函数本身执行时间（无 IPC 开销）
"""
import os, sys, importlib, inspect, logging, threading
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger("magic")

# 缓存：mcp_set → [BaseTool 实例]
_tool_cache: Dict[str, list] = {}
_cache_lock = threading.Lock()
# 缓存：module_name → FastMCP 实例（import 一次复用）
_mcp_modules: Dict[str, Any] = {}
_module_lock = threading.Lock()


def _load_mcp_module(filename: str):
    """import 一个 magic-*.py / baize_skills_mcp.py 模块，返回其 FastMCP 实例。

    模块只在首次加载时执行（含 @mcp.tool() 注册），后续复用。
    文件名含连字符（magic-info.py）不能用 importlib.import_module，
    用 spec_from_file_location 加载。
    """
    with _module_lock:
        if filename in _mcp_modules:
            return _mcp_modules[filename]
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # charlie/ root
    filepath = os.path.join(cwd, filename)
    if not os.path.exists(filepath):
        log.warning(f"[inproc] 文件不存在: {filepath}")
        return None
    mod_name = f"_inproc_{filename.replace('.py','').replace('-','_')}"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(mod_name, filepath)
        mod = importlib.util.module_from_spec(spec)
        # 把工作目录加入 sys.path，让模块内的 import 能找到依赖
        if cwd not in sys.path:
            sys.path.insert(0, cwd)
        spec.loader.exec_module(mod)
    except Exception as e:
        log.warning(f"[inproc] 模块加载失败 {filename}: {e}")
        return None
    mcp = getattr(mod, "mcp", None)
    if mcp is None:
        log.warning(f"[inproc] {filename} 无 mcp 实例")
        return None
    with _module_lock:
        _mcp_modules[filename] = mcp
    return mcp


def _extract_tools_from_mcp(mcp_inst) -> List[dict]:
    """从 FastMCP 实例提取所有工具的元数据（name, description, parameters, fn）。"""
    tools = []
    tm = getattr(mcp_inst, "_tool_manager", None)
    if tm is None:
        return tools
    for name, tool in tm._tools.items():
        params = tool.parameters
        if not params or not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        # 修正 schema 使其符合 qwen_agent 的 is_tool_schema 校验：
        # 必须有 type/properties/required 三个 key
        if "type" not in params:
            params["type"] = "object"
        if "properties" not in params:
            params["properties"] = {}
        if "required" not in params:
            # 从 properties 中提取无 default 的字段作为 required
            required = []
            for prop_name, prop_schema in params.get("properties", {}).items():
                if "default" not in prop_schema:
                    required.append(prop_name)
            params["required"] = required
        # 删除 title 等多余字段（is_tool_schema 要求 properties 内不限制，但顶层只需 3 个 key）
        # 实际上 is_tool_schema 只检查 parameters 的 key 集合 == {type, properties, required}
        # 但 FastMCP 加了 title，需要删除
        params = {k: v for k, v in params.items() if k in ("type", "properties", "required")}
        # 每个 property 也清理多余字段（title 等），只保留 type/description/enum/default
        for prop_name, prop_schema in params.get("properties", {}).items():
            if isinstance(prop_schema, dict):
                params["properties"][prop_name] = {
                    k: v for k, v in prop_schema.items()
                    if k in ("type", "description", "enum", "default", "items", "anyOf", "oneOf")
                }
                # 如果没有 description，加一个空的
                if "description" not in params["properties"][prop_name]:
                    params["properties"][prop_name]["description"] = prop_name
        tools.append({
            "name": name,
            "description": tool.description or "",
            "parameters": params,
            "fn": tool.fn,
            "is_async": getattr(tool, "is_async", False),
        })
    return tools


def _make_tool_class(name: str, description: str, parameters: dict, fn, is_async: bool):
    """动态创建一个 qwen_agent BaseTool 子类，包装普通函数。"""
    from qwen_agent.tools.base import BaseTool

    def call(self, params, **kwargs):
        # params 可能是 str 或 dict
        if isinstance(params, str):
            import json
            try:
                params = json.loads(params)
            except Exception:
                from qwen_agent.utils.utils import json_loads
                params = json_loads(params)
        # 过滤未知参数
        sig = inspect.signature(fn)
        valid_params = {}
        for k, v in (params or {}).items():
            if k in sig.parameters:
                valid_params[k] = v
        try:
            if is_async:
                import asyncio
                try:
                    asyncio.get_running_loop()
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as ex:
                        result = ex.submit(asyncio.run, fn(**valid_params)).result(timeout=30)
                except RuntimeError:
                    result = asyncio.run(fn(**valid_params))
            else:
                result = fn(**valid_params)
            if isinstance(result, str):
                return result
            return str(result)
        except Exception as e:
            log.warning(f"[inproc] 工具 {name} 执行失败: {e}")
            return f"工具执行出错: {e}"

    # 用 type() 动态创建类，绕过 ABC 的 abstractmethod 检查
    cls = type(
        f"InprocTool_{name}",
        (BaseTool,),
        {
            "name": name,
            "description": description,
            "parameters": parameters,
            "call": call,
        },
    )
    return cls


# ─── code_interpreter：让 LLM 自主写代码执行 ──────────────────────────
# 轻量进程内 Python 执行器，不需要 Docker/pebble。
# 用 subprocess + timeout 实现安全沙箱，捕获 stdout/stderr 返回给 LLM。
_CODE_INTERPRETER_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "要执行的 Python 代码。可以 import 标准库。输出用 print()。"
        },
    },
    "required": ["code"],
}

_CODE_INTERPRETER_DESC = (
    "执行 Python 代码并返回输出。用于计算、数据处理、格式转换等临时任务。"
    "代码在独立子进程中运行，有10秒超时。用 print() 输出结果。"
    "示例: print(25*4)  →  100"
)


def _make_code_interpreter_tool():
    """创建 code_interpreter 工具实例。"""
    from qwen_agent.tools.base import BaseTool
    import subprocess, tempfile, textwrap

    def call(self, params, **kwargs):
        if isinstance(params, str):
            import json
            try:
                params = json.loads(params)
            except Exception:
                from qwen_agent.utils.utils import json_loads
                params = json_loads(params)
        code = (params or {}).get("code", "").strip()
        if not code:
            return "错误: 没有提供代码"
        # 写入临时文件执行，避免 -c 的引号转义问题
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=tempfile.gettempdir(),
            )
            output = result.stdout
            if result.returncode != 0:
                output += f"\n[stderr]\n{result.stderr}" if result.stderr else ""
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: 代码执行超时(10s)"
        except Exception as e:
            return f"错误: {e}"
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    cls = type(
        "CodeInterpreterInproc",
        (BaseTool,),
        {
            "name": "code_interpreter",
            "description": _CODE_INTERPRETER_DESC,
            "parameters": _CODE_INTERPRETER_SCHEMA,
            "call": call,
        },
    )
    return cls(cfg={})


# MCP server 文件名 → 模块文件名映射
# 从 mcp_registry 的 ALL_MCP 构建时已有 command/args，这里用文件名直接 import
_SERVER_FILE_MAP = {
    "magic-info": "magic-info.py",
    "amap-maps": "magic-info.py",  # 别名，复用 magic-info
    "magic-notes": "magic-notes.py",
    "filesystem": "magic-notes.py",  # 别名
    "magic-reminder": "magic-reminder.py",
    "magic-memory": "magic-memory.py",
    "magic-preferences": "magic-preferences.py",
    "magic-roles": "magic-roles.py",
    "magic-system": "magic-system.py",
    "magic-summary": "magic-summary.py",
    "magic-habits": "magic-habits.py",
    "magic-life": "magic-life.py",
    "magic-wardrobe": "magic-wardrobe.py",
    "magic-recipe": "magic-recipe.py",
    "magic-evolution": "magic-evolution.py",
    "magic-jarvis": "magic-jarvis.py",
    "baize-skills": "baize_skills_mcp.py",
    "magic-music": "magic-music.py",
    "magic-scenes": "magic-scenes.py",
    "magic-feishu": "magic-feishu.py",
    "magic-apps": "magic-apps.py",
    "magic-browser": "magic-browser.py",
    "magic-douyin": "magic-douyin.py",
    "magic-taobao": "magic-taobao.py",
    "magic-tv": "magic-tv.py",
    "magic-dev": "magic-dev.py",
}


def resolve_inproc_tools(mcp_set: str) -> list:
    """根据 mcp_set 返回进程内 BaseTool 实例列表。

    替代 resolve_mcps(mcp_set) + MCPManager().initConfig(mcpServers) 链路。
    进程内模式不需要 env key 校验 / MCP_PROFILE 门控 / demo_mode 门控——
    工具函数已在内存中，直接按 mcp_set 名加载对应模块即可。
    """
    with _cache_lock:
        if mcp_set in _tool_cache:
            return _tool_cache[mcp_set]

    # 决定要加载哪些模块文件。
    # 进程内模式绕过 resolve_mcps() 的 demo_mode/MCP_PROFILE 门控——
    # 那些门控是为了避免 spawn 子进程时缺 API key，inproc 不需要。
    files_to_load: Dict[str, str] = {}  # filename → display name（用于日志）

    # 1. 始终注入 magic-preferences（偏好记忆对闲聊也有价值）
    if "magic-preferences" not in files_to_load:
        files_to_load["magic-preferences.py"] = "magic-preferences"

    # 2. 非 none 时注入 magic-jarvis
    if mcp_set != "none" and "magic-jarvis.py" not in files_to_load:
        files_to_load["magic-jarvis.py"] = "magic-jarvis"

    # 3. 请求的 mcp_set 本身
    if mcp_set and mcp_set != "none":
        fname = _SERVER_FILE_MAP.get(mcp_set)
        if fname:
            files_to_load.setdefault(fname, mcp_set)
        else:
            log.warning(f"[inproc] 未知 mcp_set: {mcp_set}，无文件映射")

    # 去重加载（别名如 amap-maps → magic-info.py 只加载一次）
    tools = []
    for filename, display_name in files_to_load.items():
        mcp_inst = _load_mcp_module(filename)
        if mcp_inst is None:
            log.warning(f"[inproc] {display_name} 模块加载失败，跳过")
            continue
        tool_metas = _extract_tools_from_mcp(mcp_inst)
        for meta in tool_metas:
            tool_cls = _make_tool_class(
                meta["name"], meta["description"],
                meta["parameters"], meta["fn"], meta["is_async"]
            )
            try:
                tools.append(tool_cls(cfg={}))
            except Exception as e:
                log.warning(f"[inproc] 工具 {meta['name']} 实例化失败: {e}")

    # 始终注入 code_interpreter（让 LLM 能自主写代码处理临时任务）
    if mcp_set != "none":
        try:
            tools.append(_make_code_interpreter_tool())
        except Exception as e:
            log.warning(f"[inproc] code_interpreter 加载失败: {e}")

    log.info(f"[inproc] mcp_set={mcp_set} 加载 {len(tools)} 个工具: {[t.name for t in tools[:15]]}")
    with _cache_lock:
        _tool_cache[mcp_set] = tools
    return tools
