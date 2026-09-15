"""示例自定义工具

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
