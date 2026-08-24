"""magic-browser: 浏览器控制 (5个工具: 打开/读取/点击/搜索/截图)

通过 ego-lite 控制浏览器，让 Charlie 能做任何需要浏览器操作的事情:
- 登录没有API的网站
- 读取页面信息
- 自动填写表单
- 搜索和提取数据

底层 ego-browser 调用已迁移至共享库 ego_lite_client.EgoClient。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-browser",
    "tier": "optional",
    "required_env": [],
    "label": "浏览器控制"
}

import json, os, re
from mcp.server.fastmcp import FastMCP
from ego_lite_client import EgoClient, EgoError
import logging
log = logging.getLogger("magic")

mcp = FastMCP("magic-browser")

TASK_SPACE_PREFIX = "charlie_browser"
_ego_client = EgoClient(TASK_SPACE_PREFIX)


def _run_ego(code: str, timeout: int = 30) -> dict:
    """通过 ego_lite_client 执行代码，返回解析后的结果"""
    try:
        output = _ego_client.run_raw(code, timeout=timeout)
    except EgoError as e:
        return {"status": "error", "text": str(e)[:2000]}
    except Exception as e:
        return {"status": "error", "text": str(e)}
    # 查找最后一个有效的 JSON 输出
    lines = output.split('\n')
    result = None
    for line in reversed(lines):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            try:
                result = json.loads(line)
                break
            except json.JSONDecodeError:
                pass
    if result is None:
        return {"status": "ok", "text": output[:2000]}
    return result


@mcp.tool()
def browse_page(url: str) -> str:
    log.debug(f"[browser] browse(url={url[:50]})")
    """打开网页并读取页面内容。url=完整网址(含https://)

    例: browse_page("https://www.baidu.com") → 打开百度并读取内容
        browse_page("https://www.zhihu.com") → 打开知乎热榜
    """
    code = f"""
await openOrReuseTab('{url}', {{ wait: true, timeout: 20 }})
const info = await pageInfo()
const text = await snapshotText()
cliLog(JSON.stringify({{ url: info.url, title: info.title, text: text?.slice(0, 3000) }}))
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"打开页面失败：{result.get('text', '未知错误')}"
    content = result.get("text", "")[:2500]
    title = result.get("title", "无标题")
    url = result.get("url", url)
    return f"📄 {title}\n{url}\n\n{content}"


@mcp.tool()
def search_web(query: str, site: str = "") -> str:
    """通过浏览器搜索互联网（无需API Key，直接打开搜索引擎搜索）。
    query=搜索词, site=指定站点(可选)

    例: search_web("今天天气") → 搜索今天天气
        search_web("Python教程", "github.com") → 在github搜索Python教程
    """
    search_url = f"https://www.baidu.com/s?wd={query}"
    if site:
        search_url = f"https://www.baidu.com/s?wd={query}%20site:{site}"
    else:
        search_url = f"https://www.baidu.com/s?wd={query}"
    
    code = f"""
await openOrReuseTab('{search_url}', {{ wait: true, timeout: 20 }})
const text = await snapshotText()
// 截取搜索结果部分
const lines = (text || '').split('\\n').filter(l => l.trim().length > 0)
const results = lines.slice(0, 40)
cliLog(JSON.stringify({{ results: results.slice(0, 30) }}))
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"搜索失败：{result.get('text', '未知错误')}"
    results = result.get("results", [])
    if not results:
        return f"没搜到「{query}」相关内容"
    return "🔍 搜索结果\n" + "\n".join(results[:20])


@mcp.tool()
def read_page() -> str:
    """读取当前浏览器页面的完整内容（文本模式）

    用于读取已打开的页面内容，如新闻文章、搜索结果等。
    """
    code = """
const info = await pageInfo()
const text = await snapshotText()
cliLog(JSON.stringify({ url: info.url, title: info.title, text: text?.slice(0, 5000) }))
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"读取页面失败：{result.get('text', '未知错误')}"
    return f"📄 {result.get('title', '')}\n{result.get('url', '')}\n\n{result.get('text', '')[:4000]}"


@mcp.tool()
def click_element(selector: str) -> str:
    """点击页面中的元素。selector=CSS选择器

    例: click_element("#login-btn") → 点击登录按钮
        click_element(".search-button") → 点击搜索按钮
        click_element("a") → 点击第一个链接
    """
    code = f"""
try {{
    await click('{selector}')
    await wait(1)
    const info = await pageInfo()
    const text = await snapshotText()
    cliLog(JSON.stringify({{ status: 'ok', url: info.url, title: info.title, text: text?.slice(0, 2000) }}))
}} catch(e) {{
    cliLog(JSON.stringify({{ status: 'error', text: e.message }}))
}}
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"点击失败：{result.get('text', '未知错误')}"
    return f"✅ 已点击，当前页面：{result.get('title', '')}\n{result.get('text', '')[:1500]}"


@mcp.tool()
def screenshot() -> str:
    log.debug("[screenshot] 被调用")
    """截取当前浏览器页面的截图，返回页面标题和尺寸

    例: screenshot() → 截取当前页面并返回尺寸信息
    """
    import tempfile, base64
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tmp_path = tmp.name
    tmp.close()
    
    code = f"""
await captureScreenshot('{tmp_path}')
const info = await pageInfo()
cliLog(JSON.stringify({{ url: info.url, title: info.title, width: info.w, height: info.h }}))
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"截图失败：{result.get('text', '未知错误')}"
    
    # 读取截图并转base64
    try:
        with open(tmp_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode()
        os.unlink(tmp_path)
        return f"📸 截图完成：{result.get('title', '')}\n大小：{result.get('width', 0)}x{result.get('height', 0)}\nbase64:{img_b64[:50]}..."
    except Exception as e:
        return f"截图保存失败：{e}"


@mcp.tool()
def fill_form(selector: str, text: str) -> str:
    """填写表单输入框。selector=CSS选择器, text=要输入的文字

    例: fill_form("#search-input", "Python") → 在搜索框输入Python
        fill_form("#username", "myuser") → 输入用户名
        fill_form(".phone", "13800138000") → 输入手机号
    """
    code = f"""
try {{
    await fillInput('{selector}', '{text}')
    await wait(0.5)
    cliLog(JSON.stringify({{ status: 'ok', message: '已填写' }}))
}} catch(e) {{
    cliLog(JSON.stringify({{ status: 'error', text: e.message }}))
}}
"""
    result = _run_ego(code)
    if result.get("status") == "error":
        return f"填写失败：{result.get('text', '未知错误')}"
    return f"✅ 已填写「{text}」到 {selector}"


if __name__ == "__main__":
    mcp.run()
