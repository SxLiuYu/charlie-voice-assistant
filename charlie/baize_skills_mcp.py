"""
白泽搜索 MCP 服务器
只保留搜索/互联网/购物相关工具
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "baize-skills",
    "tier": "optional",
    "required_env": [],
    "label": "互联网搜索"
}

import os, requests
from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")
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

def _wigolo_search(query: str, timeout: int = 15) -> list:
    """使用wigolo CLI搜索(带超时)"""
    import subprocess
    try:
        r = subprocess.run(
            ["wigolo", "search", query],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == 0 and r.stdout.strip():
            # wigolo returns JSON results
            import json as _j
            try:
                data = _j.loads(r.stdout)
                if isinstance(data, list):
                    return [{"title": d.get("title", ""), "url": d.get("url", ""), "snippet": d.get("snippet", d.get("content", ""))} for d in data[:8]]
            except Exception as e:
                log.debug(f"[baize] wigolo非JSON输出: {e}")
                # Non-JSON output, return as text
                return [{"title": "wigolo", "url": "", "snippet": r.stdout[:500]}]
    except subprocess.TimeoutExpired:
        log.warning("[baize] wigolo搜索超时")
        pass
    except Exception as e:
        log.warning(f"[baize] wigolo搜索异常: {e}")
        pass
    return []

@mcp.tool()
def web_search_free(query: str) -> str:
    log.info(f"[baize] 免费搜索: {query[:30]}")
    """免费互联网搜索(不需要API Key)：搜新闻、知识、人物、事件等。query=搜索词"""
    try:
        # 优先使用wigolo(如果可用)
        results = _wigolo_search(query)
        if not results:
            results = _bing_scrape(query)
        if not results:
            return f"没搜到「{query}」的相关内容"
        docs = [f"[{i+1}] {r['title']}\n{r['snippet']}" for i, r in enumerate(results)]
        try:
            reply = aliyun_chat([
                {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
                {"role": "user", "content": "\n\n".join(docs)[:9000]}
            ])
            return reply
        except Exception as e:
            # 阿里云API失败时直接返回搜索结果摘要
            return "\n".join(docs[:5])
    except Exception as e:
        return f"搜索失败: {e}"

# ===== 通用互联网搜索（Tavily API，更高质量）=====
@mcp.tool()
def web_search(query: str) -> str:
    log.info(f"[baize] 搜索: {query[:30]}")
    """通用互联网搜索：搜新闻、知识、人物、事件等任何内容。query=搜索词"""
    try:
        if not TAVILY:
            return web_search_free(query)
        body = {"api_key": TAVILY, "query": query, "max_results": 8, "search_depth": "advanced"}
        r = requests.post("https://api.tavily.com/search", json=body, timeout=30).json()
        results = r.get("results", [])
        if not results:
            return f"没搜到「{query}」的相关内容"
        docs = [f"[{i+1}] {d['title']}\n{d.get('content','')}" for i, d in enumerate(results)]
        try:
            reply = aliyun_chat([
                {"role": "system", "content": f"用户搜索「{query}」。从以下搜索结果提取关键信息，用简洁中文总结要点，附来源编号。不要编造未在结果中出现的内容。"},
                {"role": "user", "content": "\n\n".join(docs)[:9000]}
            ])
            return reply
        except Exception:
            return "\n".join(docs[:5])
    except Exception as e:
        return f"搜索失败: {e}"

# ===== 智能购物 =====
@mcp.tool()
def shopping_search(keyword: str) -> str:
    log.info(f"[baize] 购物搜索: {keyword[:30]}")
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


@mcp.tool()
def deep_research(topic: str) -> str:
    log.info(f"[baize] 深度研究: {topic[:30]}")
    """深度研究: 多源搜索+AI聚合分析，适合复杂问题。

    例: deep_research("2024年AI芯片市场格局") → 多源搜索+综合分析
        deep_research("比亚迪vs特斯拉技术对比") → 对比分析
    """
    # 用web_search_free进行快速搜索（已包含多源聚合）
    try:
        return web_search_free(topic + " 深度分析 2024")
    except Exception as e:
        return f"深度研究「{topic}」失败: {e}" 
    if not all_results:
        return f"深度研究「{topic}」未找到足够信息"

    docs = []
    seen = set()
    for r in all_results:
        title = r.get('title', '')
        if title in seen:
            continue
        seen.add(title)
        snippet = r.get('snippet', '')
        docs.append(f"[{len(docs)+1}] {title}\n{snippet}")

    try:
        reply = aliyun_chat([
            {"role": "system", "content": f"你是研究助手。用户想深入了解「{topic}」。"
             "从以下多源搜索结果中综合分析，给出结构化报告："
             "1. 核心要点（3-5条）"
             "2. 关键数据/事实"
             "3. 不同观点/争议"
             "4. 总结建议"
             "用简洁中文，附来源编号。不要编造未在结果中出现的内容。"},
            {"role": "user", "content": "\n\n".join(docs)[:12000]}
        ], temperature=0.3)
        return f"📊 深度研究：{topic}\n\n{reply}"
    except Exception as e:
        # 阿里云失败时返回搜索结果摘要
        return f"📊 深度研究：{topic}（搜索结果摘要）\n\n" + "\n\n".join(docs[:8])
