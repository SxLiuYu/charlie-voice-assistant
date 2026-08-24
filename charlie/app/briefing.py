"""早安/晚间简报 — 整合天气、日程、待办、新闻

morning_briefing(): 早安播报
evening_wrapup(): 晚间复盘
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("magic")


def _get_weather_text() -> str:
    """获取当前天气简述"""
    try:
        from app.weather import get_weather_text
        return get_weather_text()
    except Exception as e:
        log.debug(f"[briefing] 天气获取失败: {e}")
        return ""


def _get_today_agenda() -> str:
    """获取今日日程（从飞书日历）"""
    # magic_feishu.py 无 get_calendar_events 函数，此处集成已断开，暂时返回空
    return ""


def _get_due_reminders() -> str:
    """获取今日待办数"""
    try:
        from app.reminders import list_reminders
        today = datetime.now().strftime("%Y-%m-%d")
        all_reminders = list_reminders(include_completed=False)
        due = [r for r in all_reminders if not r.get("done")
               and (r.get("due", "") <= today or r.get("repeat"))]
        if not due:
            return ""
        return f"今天有{len(due)}项待办"
    except Exception as e:
        log.debug(f"[briefing] 待办获取失败: {e}")
        return ""


def _get_news_brief(limit: int = 2) -> str:
    """获取个性化新闻摘要"""
    try:
        from app.news import fetch_all_feeds, summarize_news
        feeds = fetch_all_feeds()
        if not feeds:
            return ""
        summary = summarize_news(feeds, limit=limit)
        if summary:
            return f"值得关注：{summary}"
        return ""
    except ImportError:
        # news模块还没创建时降级到热搜
        try:
            from personalized_push import get_hot_topics
            topics = get_hot_topics()[:limit]
            if topics:
                return f"热搜：{'，'.join(topics[:limit])}"
        except Exception:
            pass
        return ""
    except Exception as e:
        log.debug(f"[briefing] 新闻获取失败: {e}")
        return ""


def _get_holiday() -> str:
    """获取今日节日/特殊日期"""
    # magic_info 模块无 get_holiday_name 函数，此处集成已断开，暂时返回空
    return ""


def morning_briefing() -> str:
    """早安简报：天气 + 日程 + 待办 + 新闻 + 节日

    Returns:
        一段自然语言播报文本
    """
    parts = []

    # 问候
    hour = datetime.now().hour
    if hour < 6:
        greeting = "凌晨好"
    elif hour < 9:
        greeting = "早上好"
    elif hour < 11:
        greeting = "上午好"
    else:
        greeting = "你好"

    # 天气
    weather = _get_weather_text()
    if weather:
        parts.append(f"{greeting}！今天{weather}")
    else:
        parts.append(f"{greeting}！")

    # 节日
    holiday = _get_holiday()
    if holiday:
        parts.append(f"今天是{holiday}")

    # 日程
    agenda = _get_today_agenda()
    if agenda:
        parts.append(agenda)

    # 待办
    todo = _get_due_reminders()
    if todo:
        parts.append(todo)

    # 新闻
    news = _get_news_brief()
    if news:
        parts.append(news)

    text = "，".join(parts)
    if not text.endswith(("！", "。", "？", "!")):
        text += "。"
    log.info(f"[briefing] 早安简报: {text[:60]}")
    return text


def evening_wrapup() -> str:
    """晚间复盘：今日完成 + 明日待办 + 天气提醒"""
    parts = []
    parts.append("今天辛苦了")

    # 完成的待办
    try:
        from app.reminders import list_reminders
        today = datetime.now().strftime("%Y-%m-%d")
        all_r = list_reminders(include_completed=True)
        completed = [r for r in all_r if r.get("done")
                     and r.get("completed_at", "").startswith(today)]
        if completed:
            parts.append(f"今天完成了{len(completed)}项待办")
    except Exception:
        pass

    # 明日待办
    try:
        from app.reminders import list_reminders
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        all_r = list_reminders(include_completed=False)
        due_tomorrow = [r for r in all_r if not r.get("done")
                        and (r.get("due", "") == tomorrow or r.get("repeat"))]
        if due_tomorrow:
            parts.append(f"明天有{len(due_tomorrow)}项待办")
    except Exception:
        pass

    # 明日天气
    weather = _get_weather_text()
    if weather:
        parts.append(f"明天{weather}")

    parts.append("早点休息")
    text = "，".join(parts) + "。"
    log.info(f"[briefing] 晚间复盘: {text[:60]}")
    return text
