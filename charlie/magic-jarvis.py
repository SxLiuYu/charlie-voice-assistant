"""magic-jarvis: 贾维斯级能力（主动对话/金融行情/环境感知/体育赛事）

自用定制版专属：在 main 开箱即用版基础上增加贾维斯级感知。
全部免费API，无需额外Key。
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-jarvis",
    "tier": "optional",
    "required_env": [],
    "label": "贾维斯级能力(金融/环境/体育/主动对话)"
}

from mcp.server.fastmcp import FastMCP
import datetime
import json
import logging

log = logging.getLogger("magic")
mcp = FastMCP("magic-jarvis")


# ===== 原有工具：金融/环境/体育 =====

@mcp.tool()
def get_stock(symbol: str = "上证") -> str:
    """查询股票/指数行情。symbol=股票名或代码

    例: get_stock("上证") → 上证指数
        get_stock("茅台") → 贵州茅台
        get_stock("比亚迪") → 比亚迪
        get_stock("sh600519") → 按代码查
    """
    from app.finance import get_stock_text
    return get_stock_text(symbol)


@mcp.tool()
def get_market_overview() -> str:
    """获取市场概览：上证/深证/创业板/汇率"""
    from app.finance import get_stock_batch, get_forex
    indices = get_stock_batch(["上证", "深证", "创业板"])
    forex = get_forex("USDCNY")
    parts = [indices]
    if forex:
        parts.append(f"美元人民币: {forex['bid']:.4f}（买{forex['bid']}/卖{forex['ask']}）")
    return "\n".join(parts)


@mcp.tool()
def get_air_quality(city: str = "北京") -> str:
    """查询空气质量（PM2.5/AQI/紫外线）。city=城市名

    例: get_air_quality() → 北京空气质量
        get_air_quality("上海") → 上海空气质量
    """
    from app.environment import get_air_quality_text
    return get_air_quality_text(city)


@mcp.tool()
def get_earthquake_alert(min_magnitude: float = 4.5) -> str:
    """查询近24小时地震预警。min_magnitude=最低震级(默认4.5)

    例: get_earthquake_alert() → 近24h ≥4.5级地震
        get_earthquake_alert(6.0) → 近24h ≥6.0级重大地震
    """
    from app.environment import get_earthquake_text
    return get_earthquake_text(min_magnitude)


@mcp.tool()
def get_sports(league: str = "英超") -> str:
    """查询今日体育赛事。league=联赛名(英超/西甲/NBA/欧冠等)

    例: get_sports("英超") → 今日英超赛事
        get_sports("NBA") → 今日NBA
        get_sports("全部") → 今日所有赛事
    """
    from app.sports import get_events_text, get_all_sports_today
    if league in ("全部", "所有", "all"):
        return get_all_sports_today()
    return get_events_text(league)


# ===== P0: 主动对话引擎 =====

def _get_time_context() -> dict:
    """获取时间上下文"""
    import datetime
    now = datetime.datetime.now()
    hour = now.hour
    weekday = now.weekday()  # 0=Mon
    period = "凌晨"
    if 5 <= hour < 9:
        period = "早晨"
    elif 9 <= hour < 12:
        period = "上午"
    elif 12 <= hour < 14:
        period = "中午"
    elif 14 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 22:
        period = "晚上"
    elif 22 <= hour or hour < 5:
        period = "深夜"
    return {
        "now": now,
        "hour": hour,
        "weekday": weekday,
        "period": period,
        "is_weekend": weekday >= 5,
    }


def _get_user_context() -> dict:
    """获取用户状态上下文（失败安全，异常返回空状态）"""
    try:
        from agent.state import get_user_state
        return get_user_state()
    except Exception:
        return {}


def _get_recent_history(limit: int = 5) -> list:
    """获取最近对话历史"""
    try:
        from agent.history import _get_history
        hist = _get_history()
        return [m for m in hist[-limit * 2:] if isinstance(m, dict)]
    except Exception:
        return []


def _get_preferences() -> dict:
    """获取用户偏好"""
    try:
        from agent.preferences import list_preferences
        return list_preferences()
    except Exception:
        return {}


def _detect_habit_patterns(history: list) -> list:
    """从历史中提取习惯模式"""
    if not history:
        return []
    hour_counter = {}
    for msg in history:
        if msg.get("role") != "user":
            continue
        ts = msg.get("ts")
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromtimestamp(float(ts))
            h = dt.hour
            hour_counter[h] = hour_counter.get(h, 0) + 1
        except Exception:
            continue
    patterns = []
    for h, count in sorted(hour_counter.items(), key=lambda x: x[1], reverse=True)[:3]:
        if count >= 2:
            patterns.append({"hour": h, "count": count})
    return patterns


@mcp.tool()
def proactive_greeting() -> str:
    """根据当前时间、用户状态和历史生成主动问候。

    例: proactive_greeting() → "早上好！现在是8点，该起床了。"
    """
    import datetime
    tc = _get_time_context()
    uc = _get_user_context()
    hist = _get_recent_history(limit=3)
    prefs = _get_preferences()
    user_state = uc.get("state", "unknown")
    hour = tc["hour"]
    period = tc["period"]
    weekday = tc["weekday"]
    is_weekend = tc["is_weekend"]

    # 时间问候
    if hour < 6:
        time_greet = "还没睡呢？"
    elif hour < 9:
        time_greet = "早上好！"
    elif hour < 12:
        time_greet = "上午好！"
    elif hour < 14:
        time_greet = "中午好！"
    elif hour < 18:
        time_greet = "下午好！"
    elif hour < 22:
        time_greet = "晚上好！"
    else:
        time_greet = "夜深了，"

    # 用户状态调整
    state_note = ""
    if user_state == "home_sleeping":
        state_note = "看起来你在休息，"
    elif user_state == "away":
        state_note = "你似乎出门了，"
    elif user_state == "working":
        state_note = "还在忙吧，"

    # 周末提示
    weekend_note = ""
    if is_weekend and 9 <= hour < 11:
        weekend_note = "周末愉快，"

    # 偏好相关
    pref_note = ""
    if prefs.get("sleep_time") and hour >= int(prefs["sleep_time"].split(":")[0]) - 1:
        pref_note = "快到你的休息时间了，"
    wake_time = prefs.get("wake_time", "")
    if wake_time and hour == int(wake_time.split(":")[0]):
        pref_note = "该起床了，"

    # 历史习惯
    habit_note = ""
    habits = _detect_habit_patterns(hist)
    for habit in habits:
        if habit["hour"] == hour and habit["count"] >= 2:
            habit_note = "这个点你通常会找我，"
            break

    parts = [time_greet, weekend_note, state_note, pref_note, habit_note]
    greeting = "".join(p for p in parts if p).strip()
    if greeting.endswith("，") or greeting.endswith(","):
        greeting = greeting[:-1] + "。"
    return greeting or "你好！"


@mcp.tool()
def suggest_action(context: str = "") -> str:
    """基于当前上下文建议下一步操作。

    例: suggest_action() → "现在10点了，是否启动晚安模式？"
        suggest_action("用户刚说晚安") → 基于该上下文建议
    """
    import datetime
    tc = _get_time_context()
    uc = _get_user_context()
    prefs = _get_preferences()
    hist = _get_recent_history(limit=5)
    hour = tc["hour"]
    weekday = tc["weekday"]
    is_weekend = tc["is_weekend"]
    user_state = uc.get("state", "unknown")

    suggestions = []

    # 时间相关建议
    if hour == 7 and not is_weekend:
        suggestions.append("该起床了，需要播报今日简报吗？")
    if hour == 12:
        suggestions.append("到午饭时间了，要记录吃饭吗？")
    if hour == 22:
        suggestions.append("现在10点了，是否启动晚安模式？")
    if hour == 23:
        suggestions.append("夜深了，要设置明早的闹钟吗？")

    # 用户状态相关
    if user_state == "home_resting" and 21 <= hour < 23:
        suggestions.append("看起来你在休息，需要我帮你放松一下吗？")
    if user_state == "away" and 18 <= hour < 20:
        suggestions.append("快下班了吧，需要我帮你导航回家吗？")

    # 偏好相关
    sleep_time = prefs.get("sleep_time", "")
    if sleep_time:
        try:
            sleep_hour = int(sleep_time.split(":")[0])
            if hour == sleep_hour - 1:
                suggestions.append(f"还有1小时到你通常的休息时间({sleep_time})，需要我到时候提醒你吗？")
        except (ValueError, IndexError):
            pass

    wake_time = prefs.get("wake_time", "")
    if wake_time and hour == int(wake_time.split(":")[0]) - 1:
        suggestions.append(f"明早{wake_time}你有安排，需要我提前准备什么吗？")

    # 习惯模式
    habits = _detect_habit_patterns(hist)
    for habit in habits:
        h = habit["hour"]
        if abs(h - hour) <= 1 and habit["count"] >= 3:
            time_str = f"{h:02d}:00"
            suggestions.append(f"你通常在{time_str}左右找我，今天需要我提前准备什么吗？")

    # 待办提醒
    try:
        import os
        now = datetime.datetime.now()
        reminders_file = os.path.join(
            os.environ.get("ASSISTANT_KID_DATA_DIR", ""),
            "reminders.json"
        )
        if os.path.exists(reminders_file):
            with open(reminders_file, "r", encoding="utf-8") as f:
                reminders = json.load(f)
            pending = [
                r for r in reminders
                if isinstance(r, dict) and not r.get("done")
                and str(r.get("due", ""))[:10] == now.strftime("%Y-%m-%d")
            ]
            if pending:
                suggestions.append(f"今日还有{len(pending)}项待办未完成，要查看吗？")
    except Exception:
        pass

    if not suggestions:
        return ""
    return "建议：" + "；".join(suggestions[:3])


@mcp.tool()
def learn_habit() -> str:
    """从用户行为中提取习惯模式。

    例: learn_habit() → "发现习惯：用户通常在8点询问天气，连续5天"
    """
    hist = _get_recent_history(limit=50)
    if not hist:
        return "目前还没发现明显的使用习惯，继续使用中..."

    habits = _detect_habit_patterns(hist)
    if not habits:
        return "目前还没发现明显的使用习惯，继续使用中..."

    lines = ["发现习惯："]
    for habit in habits:
        h = habit["hour"]
        count = habit["count"]
        lines.append(f"  - 通常在{h:02d}:00左右活跃（{count}次）")
    return "\n".join(lines)


def _get_weather_context() -> str:
    """获取今日天气摘要（失败安全）"""
    try:
        from app.schedulers import _get_weather, _forecast_for_date
        casts = _get_weather()
        if not casts:
            return ""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        forecast = _forecast_for_date(casts, today)
        if not forecast:
            return ""
        weather = " ".join(
            w for w in (forecast.get("dayweather", ""), forecast.get("nightweather", ""))
            if w
        ).strip()
        temp = forecast.get("daytemp", "")
        if weather and temp:
            return f"今天{weather}，{temp}度"
        elif weather:
            return f"今天{weather}"
        elif temp:
            return f"今天{temp}度"
        return ""
    except Exception:
        return ""


def _get_today_reminders() -> tuple[list[str], list[str]]:
    """获取今日待办和明日待办。

    返回 (今日待办列表, 明日待办列表)，列表元素为提醒文本。
    """
    try:
        from app.reminders import _load_reminders
        reminders = _load_reminders()
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        today_items = []
        tomorrow_items = []
        for r in reminders:
            if not isinstance(r, dict):
                continue
            due = str(r.get("due", ""))[:10]
            text = str(r.get("text", ""))[:60]
            if not text:
                continue
            if due == today_str:
                today_items.append(text)
            elif due == tomorrow_str:
                tomorrow_items.append(text)
        return today_items, tomorrow_items
    except Exception:
        return [], []


def _get_pref_suggestions(prefs: dict, hour: int) -> list[str]:
    """基于用户偏好生成建议"""
    suggestions = []
    # 起床时间
    wake_time = prefs.get("wake_time", "")
    if wake_time and hour == int(wake_time.split(":")[0]):
        suggestions.append(f"到您的起床时间了({wake_time})")
    # 睡眠时间
    sleep_time = prefs.get("sleep_time", "")
    if sleep_time:
        try:
            sleep_hour = int(sleep_time.split(":")[0])
            if hour == sleep_hour - 1:
                suggestions.append(f"还有1小时到您的休息时间({sleep_time})")
        except (ValueError, IndexError):
            pass
    # 通勤偏好
    commute = prefs.get("commute_time", "")
    if commute:
        try:
            commute_hour = int(commute.split(":")[0])
            if hour == commute_hour - 1:
                suggestions.append(f"到通勤时间了({commute})，需要我帮您查路况吗？")
        except (ValueError, IndexError):
            pass
    return suggestions


@mcp.tool()
def morning_briefing() -> str:
    """早间简报：聚合时间、天气、今日待办、偏好建议，输出 JARVIS 风格结构化文本。

    例: morning_briefing() → "Sir，早上好。现在是上午8点。今日简报：今天晴，25度。今日待办：3项..."
    """
    tc = _get_time_context()
    prefs = _get_preferences()
    hour = tc["hour"]
    period = tc["period"]
    weekday = tc["weekday"]

    # 时间问候
    if hour < 6:
        time_greet = "还没睡呢，Sir"
    elif hour < 9:
        time_greet = "早上好，Sir"
    elif hour < 12:
        time_greet = "上午好，Sir"
    elif hour < 14:
        time_greet = "中午好，Sir"
    elif hour < 18:
        time_greet = "下午好，Sir"
    elif hour < 22:
        time_greet = "晚上好，Sir"
    else:
        time_greet = "夜深了，Sir"

    parts = [f"{time_greet}。现在是{period}{hour}点。"]

    # 天气
    weather_str = _get_weather_context()
    if weather_str:
        parts.append(weather_str + "。")

    # 今日待办
    today_items, _ = _get_today_reminders()
    if today_items:
        parts.append(f"今日待办共{len(today_items)}项：" + "；".join(today_items[:5]) + "。")
    else:
        parts.append("今日暂无待办事项。")

    # 偏好建议
    pref_suggests = _get_pref_suggestions(prefs, hour)
    if pref_suggests:
        parts.append("。".join(pref_suggests) + "。")

    return "".join(parts)


@mcp.tool()
def evening_report() -> str:
    """晚间简报：今日已触发提醒回顾 + 明日待办 + 管家式收尾。

    例: evening_report() → "Sir，今日简报：共触发5项提醒。明日待办：2项。今日辛苦了，Sir。"
    """
    tc = _get_time_context()
    prefs = _get_preferences()
    hour = tc["hour"]

    # 今日已触发提醒：从提醒文件读取（只统计今日已 done 的）
    today_triggered = 0
    try:
        from app.reminders import _load_reminders
        reminders = _load_reminders()
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        for r in reminders:
            if not isinstance(r, dict):
                continue
            if r.get("done") and str(r.get("due", ""))[:10] == today_str:
                today_triggered += 1
    except Exception:
        pass

    # 明日待办
    _, tomorrow_items = _get_today_reminders()

    # 时间问候
    if hour < 6:
        time_greet = "还没休息呢，Sir"
    elif hour < 22:
        time_greet = "晚上好，Sir"
    else:
        time_greet = "夜深了，Sir"

    parts = [f"{time_greet}。今日简报：共完成{today_triggered}项提醒。"]

    if tomorrow_items:
        parts.append(f"明日待办共{len(tomorrow_items)}项：" + "；".join(tomorrow_items[:5]) + "。")
    else:
        parts.append("明日暂无待办事项。")

    # 偏好相关
    sleep_time = prefs.get("sleep_time", "")
    if sleep_time:
        try:
            sleep_hour = int(sleep_time.split(":")[0])
            if hour >= sleep_hour - 1:
                parts.append(f"快到您的休息时间了({sleep_time})。")
        except (ValueError, IndexError):
            pass

    parts.append("今日辛苦了，Sir。早点休息。")
    return "".join(parts)


if __name__ == "__main__":
    mcp.run()
