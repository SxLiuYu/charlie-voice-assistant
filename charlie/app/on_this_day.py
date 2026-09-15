"""历史上的今天 — Byabbe 免费 API（无需 Key）

https://byabbe.se/on-this-day/{month}/{day}/events.json
数据来源 Wikipedia，中英文都有。
"""
import datetime, logging, re, requests

log = logging.getLogger("magic")
_cache: dict[str, list[dict]] = {}
_CACHE_MAX = 400  # 最多 366天 × 语言数


def _is_english(text: str) -> bool:
    """粗略检测文本是否主要为英文（含 ASCII 字母比例高则判定为英文）。"""
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_letters = sum(1 for c in letters if ord(c) < 128)
    return ascii_letters / len(letters) > 0.6


def get_events(month: int = None, day: int = None, lang: str = "zh") -> list[dict]:
    """获取历史上今天的事件

    返回 [{"year": "1969", "description": "...", "wikipedia": [...]}, ...]
    """
    now = datetime.date.today()
    if month is None:
        month = now.month
    if day is None:
        day = now.day
    cache_key = f"{month}/{day}/{lang}"
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        r = requests.get(
            f"https://byabbe.se/on-this-day/{month}/{day}/events.json",
            timeout=10,
        )
        events = r.json().get("events", [])
        # 按年份降序排（最近的在前）
        events.sort(key=lambda e: int(e.get("year", 0)), reverse=True)
        _cache[cache_key] = events
        if len(_cache) > _CACHE_MAX:
            _cache.clear()
        return events
    except Exception as e:
        log.debug(f"[on-this-day] 获取失败: {e}")
    return []


def get_events_text(month: int = None, day: int = None, count: int = 3, brief: bool = False) -> str:
    """格式化输出历史上的今天

    brief=True: 语音场景，只取2条，每条≤30字，空格分隔
    brief=False: 文本场景，取count条，每条≤60字，换行分隔
    """
    now = datetime.date.today()
    if month is None:
        month = now.month
    if day is None:
        day = now.day
    events = get_events(month, day)
    if not events:
        return f"获取{month}月{day}日的历史事件失败"
    if brief:
        lines = [f"历史上的今天{month}月{day}日："]
        for e in events[:2]:
            year = e.get("year", "?")
            desc = e.get("description", "")[:30]
            if _is_english(desc):
                desc = f"（英文）{desc}"
            lines.append(f"{year}年，{desc}。")
        return " ".join(lines)
    lines = [f"历史上的{month}月{day}日："]
    for e in events[:count]:
        year = e.get("year", "?")
        desc = e.get("description", "")[:60]
        if _is_english(desc):
            desc = f"（英文）{desc}"
        lines.append(f"  {year}年：{desc}")
    return "\n".join(lines)
