"""magic-reminder: 提醒/定时器/日历 (3个工具)"""
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("magic-reminder")


@mcp.tool()
def add_reminder(text: str, time: str = "", repeat: str = "") -> str:
    """添加提醒。text=提醒内容, time=时间(如'下午3点'/'30分钟后'/'每天8点'), repeat=重复类型(daily/weekly/weekdays, 留空=一次性)

    repeat 值:
    - 留空: 一次性提醒
    - daily: 每天重复
    - weekly: 每周重复
    - weekdays: 工作日(周一到周五)重复
    """
    from utils import parse_time_str
    from app.reminders import append_reminder

    due = parse_time_str(time) if time else None
    repeat_clean = repeat if repeat in ("daily", "weekly", "weekdays") else ""
    if not repeat_clean and time:
        if "每天" in time or "每日" in time:
            repeat_clean = "daily"
        elif "每周" in time:
            repeat_clean = "weekly"
        elif "工作日" in time:
            repeat_clean = "weekdays"
    time_clean = time
    for w in ("每天", "每日", "每周", "工作日"):
        time_clean = time_clean.replace(w, "")
    if not time_clean.strip():
        time_clean = time
    due = parse_time_str(time_clean) if time_clean.strip() else None

    item = append_reminder(text, time_clean, due, repeat=repeat_clean)
    repeat_desc = {"daily": "（每天重复）", "weekly": "（每周重复）", "weekdays": "（工作日重复）"}.get(repeat_clean, "")
    when = f"，时间{due.replace('T', ' ')}" if due else ""
    return f"已添加提醒：{text}{when}{repeat_desc}"


@mcp.tool()
def set_timer(minutes: int, message: str = "") -> str:
    """设置倒计时定时器。minutes=分钟数, message=到点播报内容(可选)

    例: set_timer(5, "关火") → 5分钟后播报"主人，5分钟到了：关火"
    """
    from datetime import datetime as _dt, timedelta as _td
    from app.reminders import append_reminder

    msg = message if message else f"{minutes}分钟定时器"
    due = (_dt.now() + _td(minutes=minutes)).isoformat()
    item = append_reminder(msg, f"{minutes}分钟后", due, repeat="")
    return f"已设置{minutes}分钟定时器：{msg}，到点会提醒你。"


@mcp.tool()
def get_calendar_today() -> str:
    """获取今天的日历日程（从 Apple Calendar 读取）"""
    import subprocess
    try:
        script = '''
        set today to current date
        set time of today to 0
        set tomorrow to today + (1 * days)
        set output to ""
        tell application "Calendar"
            repeat with c in calendars
                set evts to (every event of c whose start date >= today and start date < tomorrow)
                repeat with e in evts
                    set s to start date of e
                    set t to summary of e
                    set h to hours of s
                    set m to minutes of s
                    set output to output & (h as string) & ":" & (text -2 thru -1 of ("0" & (m as string))) & " " & t & "\\n"
                end repeat
            end repeat
        end tell
        return output
        '''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        result = r.stdout.strip()
        if not result:
            return "今天没有日程安排。"
        return f"今天的日程：\n{result}"
    except Exception as e:
        return f"读取日历失败：{e}。请直接看手机日历。"


if __name__ == "__main__":
    mcp.run()
