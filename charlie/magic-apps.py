"""magic-apps: 常用App浏览器控制 (13个工具)

通过 ego-browser 控制国内常用App的网页版，复用用户登录态。
让 Charlie 能操作微信、支付宝、抖音、淘宝等没有API的App。

底层 ego-browser 调用已迁移至共享库 ego_lite_client.EgoClient。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-apps",
    "tier": "optional",
    "required_env": [],
    "label": "常用App浏览器控制"
}

import json, os, time
from mcp.server.fastmcp import FastMCP
from ego_lite_client import EgoClient, EgoError
import logging
log = logging.getLogger("magic")

mcp = FastMCP("magic-apps")

# App 网页版URL
APP_URLS = {
    "wechat": "https://wx.qq.com/",           # 微信网页版
    "alipay": "https://mobile.alipay.com/",     # 支付宝
    "douyin": "https://www.douyin.com/",        # 抖音
    "toutiao": "https://www.toutiao.com/",      # 今日头条
    "meituan": "https://www.meituan.com/",      # 美团
    "taobao": "https://www.taobao.com/",        # 淘宝
    "jd": "https://www.jd.com/",                # 京东
    "pdd": "https://mobile.yangkeduo.com/",     # 拼多多
    "dianping": "https://www.dianping.com/",    # 大众点评
    "maoyan": "https://www.maoyan.com/",        # 猫眼电影
    "damai": "https://www.damai.cn/",           # 大麦
    "xianyu": "https://www.goofish.com/",       # 咸鱼
    "feishu": "https://www.feishu.cn/messenger/",# 飞书
}

APP_NAMES = {
    "wechat": "微信", "alipay": "支付宝", "douyin": "抖音",
    "toutiao": "今日头条", "meituan": "美团", "taobao": "淘宝",
    "jd": "京东", "pdd": "拼多多", "dianping": "大众点评",
    "maoyan": "猫眼电影", "damai": "大麦", "xianyu": "咸鱼",
    "feishu": "飞书",
}


_ego_client = EgoClient("charlie_apps")


def _run_ego(code: str, timeout: int = 25) -> str:
    """通过 ego_lite_client 执行代码，返回解析后的结果"""
    try:
        output = _ego_client.run_raw(code, timeout=timeout)
    except EgoError as e:
        log.warning(f"[apps] ego执行失败(EgoError): {e}")
        return {"status": "error", "text": str(e)[:2000]}
    except Exception as e:
        log.error(f"[apps] ego执行异常: {e}")
        return {"status": "error", "text": str(e)}
    # 解析 JSON 输出
    lines = output.split('\n')
    for line in reversed(lines):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                log.debug(f"[apps] ego输出非JSON，按纯文本处理: {line[:80]}")
                pass
    return {"status": "ok", "text": output[:3000]}


def _open_and_read(url: str, app_name: str, action: str = "读取") -> str:
    """通用：打开App网页版并读取内容"""
    log.info(f"[apps] {action} {app_name}: {url}")
    result = _run_ego(f"""
await openOrReuseTab('{url}', {{ wait: true, timeout: 15 }})
await wait(2)
const info = await pageInfo()
const text = await snapshotText()
const lines = (text || '').split('\\n').filter(l => l.trim().length > 0).slice(0, 30)
cliLog(JSON.stringify({{ title: info.title, url: info.url, content: lines.join('\\n')[:2000] }}))
""")
    if result.get("status") == "error":
        log.warning(f"[apps] {app_name}{action}失败: {result.get('text', '')[:100]}")
        return f"{app_name}{action}失败：{result.get('text', '未知错误')}"
    log.debug(f"[apps] {app_name}{action}成功: {result.get('title', '')[:50]}")
    return f"📱 {app_name}已打开\n{result.get('title', '')}\n\n{result.get('content', '')[:1500]}"


def _search_app(url: str, app_name: str, keyword: str) -> str:
    """通用：在App中搜索"""
    search_url = f"{url}search?keyword={keyword}"
    log.info(f"[apps] {app_name}搜索「{keyword}」")
    result = _run_ego(f"""
await openOrReuseTab('{search_url}', {{ wait: true, timeout: 15 }})
await wait(3)
const info = await pageInfo()
const text = await snapshotText()
const lines = (text || '').split('\\n').filter(l => l.trim().length > 5).slice(0, 25)
cliLog(JSON.stringify({{ title: info.title, url: info.url, content: lines.join('\\n')[:2000] }}))
""")
    if result.get("status") == "error":
        log.warning(f"[apps] {app_name}搜索失败: {result.get('text', '')[:100]}")
        return f"{app_name}搜索失败：{result.get('text', '未知错误')}"
    log.debug(f"[apps] {app_name}搜索成功，结果: {result.get('content', '')[:80]}")
    return f"🔍 {app_name}搜索「{keyword}」\n{result.get('content', '')[:1500]}"


# ===== 各App专用工具 =====

@mcp.tool()
def open_wechat() -> str:
    """打开微信网页版，查看聊天消息

    例: open_wechat() → 打开微信网页版
    """
    return _open_and_read(APP_URLS["wechat"], "微信", "已打开")


@mcp.tool()
def open_alipay() -> str:
    """打开支付宝网页版，查看账单/余额

    例: open_alipay() → 打开支付宝
    """
    return _open_and_read(APP_URLS["alipay"], "支付宝")


@mcp.tool()
def browse_douyin(keyword: str = "") -> str:
    """打开抖音网页版，浏览/搜索视频。keyword=搜索词(可选)

    例: browse_douyin() → 打开抖音首页
        browse_douyin("Python教程") → 搜索Python教程视频
    """
    if keyword:
        return _search_app("https://www.douyin.com/search/", "抖音", keyword)
    return _open_and_read(APP_URLS["douyin"], "抖音")


@mcp.tool()
def browse_toutiao(keyword: str = "") -> str:
    """打开今日头条网页版，浏览/搜索新闻。keyword=搜索词(可选)

    例: browse_toutiao() → 打开今日头条首页
        browse_toutiao("AI") → 搜索AI相关新闻
    """
    if keyword:
        return _search_app("https://so.toutiao.com/search?keyword=", "今日头条", keyword)
    return _open_and_read(APP_URLS["toutiao"], "今日头条")


@mcp.tool()
def browse_meituan(keyword: str = "") -> str:
    """打开美团网页版，搜索外卖/团购/酒店。keyword=搜索词(可选)

    例: browse_meituan("火锅") → 搜索火锅餐厅
        browse_meituan() → 打开美团首页
    """
    if keyword:
        return _search_app("https://www.meituan.com/s/", "美团", keyword)
    return _open_and_read(APP_URLS["meituan"], "美团")


@mcp.tool()
def browse_taobao(keyword: str = "") -> str:
    """打开淘宝网页版，搜索商品。keyword=商品名(可选)

    例: browse_taobao("手机壳") → 搜索手机壳
        browse_taobao() → 打开淘宝首页
    """
    if keyword:
        return _search_app("https://s.taobao.com/search?q=", "淘宝", keyword)
    return _open_and_read(APP_URLS["taobao"], "淘宝")


@mcp.tool()
def browse_jd(keyword: str = "") -> str:
    """打开京东网页版，搜索商品。keyword=商品名(可选)

    例: browse_jd("耳机") → 京东搜索耳机
        browse_jd() → 打开京东首页
    """
    if keyword:
        return _search_app("https://search.jd.com/Search?keyword=", "京东", keyword)
    return _open_and_read(APP_URLS["jd"], "京东")


@mcp.tool()
def browse_pdd(keyword: str = "") -> str:
    """打开拼多多网页版，搜索商品。keyword=商品名(可选)

    例: browse_pdd("纸巾") → 拼多多搜索纸巾
    """
    if keyword:
        return _search_app("https://mobile.yangkeduo.com/search_result.html?search_key=", "拼多多", keyword)
    return _open_and_read(APP_URLS["pdd"], "拼多多")


@mcp.tool()
def browse_dianping(keyword: str = "") -> str:
    """打开大众点评网页版，搜索餐厅/商家。keyword=搜索词(可选)

    例: browse_dianping("日料") → 搜索日料餐厅
    """
    if keyword:
        return _search_app("https://www.dianping.com/search/keyword/", "大众点评", keyword)
    return _open_and_read(APP_URLS["dianping"], "大众点评")


@mcp.tool()
def browse_maoyan() -> str:
    """打开猫眼电影网页版，查看正在热映的电影

    例: browse_maoyan() → 查看正在热映的电影
    """
    return _open_and_read(APP_URLS["maoyan"], "猫眼电影")


@mcp.tool()
def browse_damai() -> str:
    """打开大麦网页版，查看演出/演唱会/展览

    例: browse_damai() → 查看近期演出
    """
    return _open_and_read(APP_URLS["damai"], "大麦")


@mcp.tool()
def browse_xianyu(keyword: str = "") -> str:
    """打开咸鱼网页版，搜索二手商品。keyword=商品名(可选)

    例: browse_xianyu("iPhone") → 搜索二手iPhone
    """
    if keyword:
        return _search_app("https://www.goofish.com/search?q=", "咸鱼", keyword)
    return _open_and_read(APP_URLS["xianyu"], "咸鱼")


@mcp.tool()
def browse_feishu() -> str:
    """打开飞书网页版，查看消息/文档

    例: browse_feishu() → 打开飞书消息
    """
    return _open_and_read(APP_URLS["feishu"], "飞书")


if __name__ == "__main__":
    mcp.run()
