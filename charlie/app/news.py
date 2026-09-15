"""多源新闻聚合 — RSSHub + LLM 摘要

支持百度热搜、微博热搜、知乎热榜、36氪、少数派等。
"""
import logging
import os
import time
import hashlib
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

log = logging.getLogger("magic")

RSSHUB_BASE = os.getenv("RSSHUB_BASE", "https://rsshub.app")

# 新闻源配置
SOURCES = {
    "baidu_hot": "/baidu/topnews",
    "weibo_hot": "/weibo/search/hot",
    "zhihu_hot": "/zhihu/hotlist",
    "36kr": "/36kr/newsflashes",
    "sspai": "/sspai/matrix",
}

# 简单缓存
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300  # 5分钟
_CACHE_MAX = 50  # 最多缓存 50 个 feed


def fetch_feed(source: str) -> list[dict]:
    """拉取单个 RSS 源

    Returns:
        [{"title", "summary", "link", "published", "source"}]
    """
    if source not in SOURCES:
        return []

    # 检查缓存
    cache_key = source
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data

    try:
        import feedparser
        url = RSSHUB_BASE.rstrip("/") + SOURCES[source]
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            log.debug(f"[news] 源 {source} 返回非200: {resp.status_code}")
            return []
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:15]:
            items.append({
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip()[:200],
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": source,
            })
        _cache[cache_key] = (time.time(), items)
        # Evict expired entries when cache is full
        if len(_cache) > _CACHE_MAX:
            now = time.time()
            expired = [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL]
            for k in expired:
                _cache.pop(k, None)
            # If still over limit, drop oldest
            if len(_cache) > _CACHE_MAX:
                oldest = min(_cache, key=lambda k: _cache[k][0])
                _cache.pop(oldest, None)
        return items
    except ImportError:
        log.warning("[news] feedparser 未安装，pip install feedparser")
        return []
    except Exception as e:
        log.debug(f"[news] 拉取 {source} 失败: {e}")
        return []


def _title_similarity(a: str, b: str) -> float:
    """简单标题相似度（基于共同字符）"""
    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _deduplicate(items: list[dict]) -> list[dict]:
    """去重：标题相似度 > 0.6 的合并"""
    if not items:
        return []
    result = [items[0]]
    for item in items[1:]:
        is_dup = False
        for existing in result:
            if _title_similarity(item["title"], existing["title"]) > 0.6:
                is_dup = True
                break
        if not is_dup:
            result.append(item)
    return result


def fetch_all_feeds(sources: Optional[list[str]] = None) -> list[dict]:
    """并发拉取所有新闻源，去重后按时间倒序

    Args:
        sources: 指定源，None=全部
    """
    source_keys = sources or list(SOURCES.keys())
    all_items = []

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_feed, s): s for s in source_keys}
        for future in as_completed(futures, timeout=15):
            try:
                items = future.result()
                all_items.extend(items)
            except Exception as e:
                log.debug(f"[news] 源拉取失败: {e}")

    # 去重
    deduped = _deduplicate(all_items)
    log.info(f"[news] 拉取 {len(all_items)} 条，去重后 {len(deduped)} 条")
    return deduped


def summarize_news(items: list[dict], limit: int = 3) -> str:
    """用 LLM 筛选并摘要新闻

    Returns:
        "标题1：摘要1；标题2：摘要2"
    """
    if not items:
        return ""

    # 如果条目少，直接返回标题
    if len(items) <= limit:
        titles = [it["title"] for it in items[:limit]]
        return "；".join(titles)

    # 用 LLM 筛选
    try:
        from mcp_common import aliyun_chat
        titles = "\n".join(f"{i+1}. {it['title']}" for i, it in enumerate(items[:20]))
        prompt = f"""从以下新闻标题中选出{limit}条最值得关注的（科技/社会/财经优先），
对每条用一句话概括（20字以内）。格式：标题：摘要\n\n{titles}"""
        response = aliyun_chat(prompt, temperature=0.3)
        if response:
            return response.strip()
    except Exception as e:
        log.debug(f"[news] LLM 摘要失败: {e}")

    # 降级：返回前N条标题
    return "；".join(it["title"] for it in items[:limit])


def get_personalized_news(interests: list[str] | None = None, limit: int = 3) -> str:
    """获取个性化新闻摘要

    Args:
        interests: 用户兴趣关键词
        limit: 返回条数
    """
    items = fetch_all_feeds()
    if not items:
        return ""

    # 如果有兴趣标签，筛选相关新闻
    if interests:
        filtered = []
        for item in items:
            title_lower = item["title"].lower()
            if any(kw.lower() in title_lower for kw in interests):
                filtered.append(item)
        if filtered:
            items = filtered

    return summarize_news(items, limit=limit)
