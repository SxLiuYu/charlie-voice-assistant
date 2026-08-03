"""
白泽搜索 MCP 服务器
只保留搜索/互联网/购物相关工具
"""
import os, requests
from mcp.server.fastmcp import FastMCP
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

mcp = FastMCP("baize-skills")


def _check_key(key: str, name: str):
    if not key:
        raise ValueError(f"未配置{name}API密钥")
    return key
TAVILY = os.getenv("TAVILY_API_KEY", "")
ALIYUN = os.getenv("ALIYUN_API_KEY", "")
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def aliyun_chat(messages, temperature=0.3):
    r = requests.post(DASHSCOPE, headers={"Authorization": f"Bearer {ALIYUN}", "Content-Type": "application/json"},
        json={"model": "qwen-max", "messages": messages, "temperature": temperature, "stream": False}, timeout=60)
    return r.json()["choices"][0]["message"]["content"]

# ===== 通用互联网搜索（免API，Bing中国爬取）=====
def _bing_scrape(query: str, max_results: int = 8) -> list:
    """爬取 cn.bing.com 搜索结果，不需要 API key"""
    import re, html as htmlmod
    r = requests.get('https://cn.bing.com/search', params={'q': query, 'count': max_results},
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}, timeout=15)
    blocks = re.findall(r'<li class="b_algo".*?</li>', r.text, re.DOTALL)
    results = []
    for b in blocks[:max_results]:
        title_m = re.search(r'<h2[^>]*>(.*?)</h2>', b, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ''
        title = htmlmod.unescape(title)
        url_m = re.search(r'<a[^>]*href="(https?://[^"]+)"', b)
        url = url_m.group(1) if url_m else ''
        snippet_m = re.search(r'<p[^>]*>(.*?)</p>', b, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ''
        snippet = htmlmod.unescape(snippet)
        if title and len(title) > 5:
            results.append({'title': title, 'url': url, 'snippet': snippet})
    return results

@mcp.tool()
def web_search_free(query: str) -> str:
    """免费互联网搜索(不需要API Key)：搜新闻、知识、人物、事件等。query=搜索词"""
    results = _bing_scrape(query)
    if not results:
        return f"没搜到「{query}」的相关内容"
    docs = [f"[{i+1}] {r['title']}\n{r['snippet']}" for i, r in enumerate(results)]
    reply = aliyun_chat([
        {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

# ===== 通用互联网搜索（Tavily API，更高质量）=====
@mcp.tool()
def web_search(query: str) -> str:
    """通用互联网搜索：搜新闻、知识、人物、事件等任何内容。query=搜索词"""
    body = {"api_key": TAVILY, "query": query, "max_results": 8, "search_depth": "advanced"}
    r = requests.post("https://api.tavily.com/search", json=body, timeout=30).json()
    results = r.get("results", [])
    if not results:
        return f"没搜到「{query}」的相关内容"
    docs = [f"[{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(results)]
    reply = aliyun_chat([
        {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

# ===== 智能购物 =====
@mcp.tool()
def shopping_search(keyword: str) -> str:
    """智能购物推荐：搜什么值得买价格+评测，选出最优前5带真实价格。keyword=商品名"""
    def tavily(query, domains=None):
        body = {"api_key": TAVILY, "query": query, "max_results": 5, "search_depth": "advanced"}
        if domains: body["include_domains"] = domains
        r = requests.post("https://api.tavily.com/search", json=body, timeout=30).json()
        return r.get("results", [])
    price = tavily(f"{keyword} 价格 推荐", ["smzdm.com"])
    review = tavily(f"{keyword} 评测 推荐 型号 性价比")
    docs = ([f"[价格{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(price)] +
            [f"[评测{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(review)])
    if not docs: return f"没搜到{keyword}的相关商品"
    reply = aliyun_chat([
        {"role": "system", "content": f"你是专业购物顾问。用户想买【{keyword}】。从结果提取与{keyword}直接相关的具体产品型号和真实价格，优先[价格]来源。凑齐5个最优，每个一句话推荐理由。格式：1.【产品型号】¥XX 推荐理由"},
        {"role": "user", "content": "\n\n".join(docs)[:9000]}
    ])
    return reply

if __name__ == "__main__":
    mcp.run()
