"""magic-douyin: 抖音MCP (3个工具: 搜索视频/获取视频信息/获取热门)

通过抖音网页版API搜索和获取视频信息，无需官方API密钥。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-douyin",
    "tier": "optional",
    "required_env": [],
    "label": "抖音MCP"
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

mcp = FastMCP("magic-douyin")


@mcp.tool()
def search_videos(keyword: str, count: int = 5) -> str:
    log.debug(f"[douyin] search_videos(query={query})")
    """搜索抖音视频。keyword=搜索词, count=返回条数

    例: search_videos("美食教程") → 搜索抖音美食视频
        search_videos("Python") → 搜索Python教程
    """
    try:
        # 使用抖音搜索API
        r = requests.get("https://www.douyin.com/aweme/v1/web/search/item/",
            params={"keyword": keyword, "count": count, "type": 1},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            },
            timeout=10)
        data = r.json()
        videos = data.get("data", [])
        if not videos:
            return f"没搜到「{keyword}」相关视频"
        lines = [f"🎬 抖音搜索「{keyword}」结果："]
        for v in videos[:count]:
            desc = (v.get("desc", "") or "")[:80]
            author = v.get("author", {}).get("nickname", "未知")
            stats = v.get("statistics", {})
            likes = stats.get("digg_count", 0)
            lines.append(f"• {desc} - {author} (❤️{likes})")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索抖音视频失败: {e}"


@mcp.tool()
def get_trending(count: int = 10) -> str:
    log.debug("[get_trending] 被调用")
    """获取抖音热门视频/热搜

    例: get_trending() → 查看抖音热搜榜
    """
    try:
        r = requests.get("https://www.douyin.com/aweme/v1/web/hot/search/list/",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            },
            timeout=10)
        data = r.json()
        hot_list = data.get("data", {}).get("trending_list", []) or data.get("data", {}).get("hot_list", [])
        if not hot_list:
            return "获取抖音热搜失败"
        lines = [f"🔥 抖音热搜榜（前{len(hot_list)}名）："]
        for i, item in enumerate(hot_list[:count], 1):
            word = item.get("word", "?")
            hot_value = item.get("hot_value", 0)
            lines.append(f"  {i}. {word} (热度{hot_value})")
        return "\n".join(lines)
    except Exception as e:
        return f"获取抖音热搜失败: {e}"


@mcp.tool()
def get_video_info(url: str) -> str:
    log.debug("[get_video_info] 被调用")
    """获取抖音视频信息（无水印视频链接）。url=视频分享链接

    例: get_video_info("https://www.douyin.com/video/xxx") → 获取视频信息和下载链接
    """
    try:
        # 提取视频ID
        video_id = ""
        if "video/" in url:
            video_id = url.split("video/")[-1].split("?")[0]
        if not video_id:
            return "无法解析视频链接，请提供完整抖音视频URL"
        r = requests.get(f"https://www.douyin.com/aweme/v1/web/aweme/detail/",
            params={"aweme_id": video_id},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            },
            timeout=10)
        data = r.json()
        aweme = data.get("aweme_detail", {})
        if not aweme:
            return f"获取视频信息失败"
        desc = aweme.get("desc", "无描述")[:100]
        author = aweme.get("author", {}).get("nickname", "未知")
        stats = aweme.get("statistics", {})
        likes = stats.get("digg_count", 0)
        comments = stats.get("comment_count", 0)
        # 无水印视频链接
        video_url = aweme.get("video", {}).get("play_addr", {}).get("url_list", [None])[0] or ""
        lines = [
            f"🎬 {desc}",
            f"👤 {author}  ❤️{likes}  💬{comments}",
        ]
        if video_url:
            lines.append(f"📹 无水印视频: {video_url}")
        return "\n".join(lines)
    except Exception as e:
        return f"获取视频信息失败: {e}"


if __name__ == "__main__":
    mcp.run()
