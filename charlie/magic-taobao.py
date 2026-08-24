"""magic-taobao: 淘宝MCP (3个工具: 搜索商品/获取商品详情/比价)

通过淘宝网页版搜索商品信息，无需官方API密钥。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-taobao",
    "tier": "optional",
    "required_env": [],
    "label": "淘宝/京东MCP"
}

import os, json, requests, re
from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

mcp = FastMCP("magic-taobao")


@mcp.tool()
def search_products(keyword: str, count: int = 5) -> str:
    log.debug(f"[taobao] search_products(query={query})")
    """搜索淘宝商品。keyword=商品关键词, count=返回条数(最多10)

    例: search_products("手机壳") → 搜索手机壳
        search_products("蓝牙耳机 200元以内") → 按价格搜索
    """
    try:
        # 使用淘宝搜索
        r = requests.get("https://s.taobao.com/search",
            params={"q": keyword, "sort": "default", "s": 0, "page": 1},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.taobao.com/",
            },
            timeout=10)
        # 从HTML中提取商品数据
        html = r.text
        # 查找商品数据JSON
        match = re.search(r'g_page_config\s*=\s*({.*?});', html, re.DOTALL)
        if not match:
            return f"搜索「{keyword}」失败，请稍后重试"
        data = json.loads(match.group(1))
        items = data.get("mods", {}).get("itemlist", {}).get("data", {}).get("auctions", [])
        if not items:
            return f"没搜到「{keyword}」相关商品"
        lines = [f"🛒 淘宝搜索「{keyword}」结果："]
        for item in items[:count]:
            title = re.sub(r'<[^>]+>', '', item.get("title", "?"))[:60]
            price = item.get("view_price", "?")
            sales = item.get("view_sales", "0人付款")
            seller = item.get("nick", "?")
            lines.append(f"• {title} ¥{price} {sales} - {seller}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索淘宝商品失败: {e}"


@mcp.tool()
def search_jd(keyword: str, count: int = 5) -> str:
    log.debug("[search_jd] 被调用")
    """搜索京东商品。keyword=商品关键词, count=返回条数

    例: search_jd("笔记本电脑") → 京东搜索笔记本
    """
    try:
        r = requests.get("https://search.jd.com/Search",
            params={"keyword": keyword, "enc": "utf-8", "page": 1},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.jd.com/",
            },
            timeout=10)
        html = r.text
        # 提取商品列表
        items = re.findall(
            r'<div class="p-name".*?<em>(.*?)</em>', html, re.DOTALL
        )
        prices = re.findall(
            r'<div class="p-price".*?<strong.*?>(.*?)</strong>', html, re.DOTALL
        )
        if not items:
            return f"没搜到「{keyword}」相关商品"
        lines = [f"🛒 京东搜索「{keyword}」结果："]
        for i, (title, price) in enumerate(zip(items[:count], prices[:count]), 1):
            title_clean = re.sub(r'<[^>]+>', '', title).strip()[:60]
            price_clean = re.sub(r'<[^>]+>', '', price).strip()
            lines.append(f"  {i}. {title_clean} ¥{price_clean}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索京东商品失败: {e}"


@mcp.tool()
def compare_price(product: str) -> str:
    log.debug("[compare_price] 被调用")
    """跨平台比价：同时搜索淘宝和京东。product=商品名

    例: compare_price("iPhone 16") → 对比淘宝和京东的iPhone 16价格
    """
    results = []
    # 淘宝搜索
    try:
        r = requests.get("https://s.taobao.com/search",
            params={"q": product, "sort": "default", "s": 0},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.taobao.com/"},
            timeout=10)
        html = r.text
        match = re.search(r'g_page_config\s*=\s*({.*?});', html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            items = data.get("mods", {}).get("itemlist", {}).get("data", {}).get("auctions", [])
            if items:
                item = items[0]
                title = re.sub(r'<[^>]+>', '', item.get("title", "?"))[:40]
                price = item.get("view_price", "?")
                results.append(f"  🐱 淘宝: {title} ¥{price}")
    except Exception:
        pass
    # 京东搜索
    try:
        r = requests.get("https://search.jd.com/Search",
            params={"keyword": product, "enc": "utf-8"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.jd.com/"},
            timeout=10)
        html = r.text
        items = re.findall(r'<div class="p-name".*?<em>(.*?)</em>', html, re.DOTALL)
        prices = re.findall(r'<div class="p-price".*?<strong.*?>(.*?)</strong>', html, re.DOTALL)
        if items and prices:
            title = re.sub(r'<[^>]+>', '', items[0]).strip()[:40]
            price = re.sub(r'<[^>]+>', '', prices[0]).strip()
            results.append(f"  🐶 京东: {title} ¥{price}")
    except Exception:
        pass
    if not results:
        return f"比价「{product}」失败，未获取到数据"
    return f"📊 比价: {product}\n" + "\n".join(results)


if __name__ == "__main__":
    mcp.run()
