"""magic-reminder: 提醒/定时器/日历 (3个工具)"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-reminder",
    "tier": "core",
    "required_env": [],
    "label": "提醒/定时器/日历"
}

from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")
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
    log.info(f"[reminder] add_reminder(text={text}, time={time})")
    from utils import parse_time_str
    from app.reminders import append_reminder

    try:
        due = parse_time_str(time) if time else None
    except Exception:
        due = None
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
    log.debug("[set_timer] 被调用")
    """设置倒计时定时器。minutes=分钟数, message=到点播报内容(可选)

    例: set_timer(5, "关火") → 5分钟后播报"主人，5分钟到了：关火"
        set_timer(30) → 30分钟后提醒
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from app.reminders import append_reminder
        msg = message if message else f"{minutes}分钟定时器"
        due = (_dt.now() + _td(minutes=minutes)).isoformat()
        item = append_reminder(msg, f"{minutes}分钟后", due, repeat="")
        return f"已设置{minutes}分钟定时器：{msg}，到点会提醒你。"
    except Exception as e:
        return f"设置定时器失败: {e}"


@mcp.tool()
def get_calendar_today() -> str:
    log.debug("[get_calendar_today] 被调用")
    """获取今天的日历日程（从 Apple Calendar 读取，仅macOS可用）

    例: get_calendar_today() → 查看今天日程安排
    """
    import subprocess, platform
    if platform.system() != "Darwin":
        return "日历功能仅在macOS原生环境可用，容器内不支持。请直接看手机日历。"
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




@mcp.tool()
def schedule_task(name: str, time: str, action: str = "remind") -> str:
    log.debug("[schedule_task] 被调用")
    """创建定时任务。name=任务名称, time=执行时间(如'每天8点'/'明天10点'/'30分钟后'), action=动作(remind提醒/notify通知)

    例: schedule_task("关空调", "每天22点") → 每天22点提醒关空调
        schedule_task("吃药", "明天8点") → 明天8点提醒吃药
        schedule_task("休息", "30分钟后") → 30分钟后提醒休息
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from app.reminders import append_reminder
        from utils import parse_time_str

        due = parse_time_str(time) if time else None
        if not due:
            return f"无法解析时间：{time}，请使用【明天8点】或【30分钟后】等格式"
        item = append_reminder(name, time, due, repeat="")
        return f"已创建定时任务：{name}，时间：{due.replace('T', ' ')}"
    except Exception as e:
        return f"创建定时任务失败: {e}"


@mcp.tool()
def list_alarms() -> str:
    log.debug("[list_alarms] 被调用")
    """列出所有闹钟和定时任务

    例: list_alarms() → 查看所有已设置的闹钟
    """
    try:
        from datetime import datetime as _dt
        from app.reminders import _load_reminders
        reminders = _load_reminders()
        active = [r for r in reminders if not r.get("done")]
        if not active:
            return "当前没有闹钟或定时任务。"
        lines = [f"共 {len(active)} 个闹钟/定时任务："]
        for r in active[:10]:
            text = r.get("text", "")
            due = r.get("due", "")[:19].replace("T", " ")
            repeat = " 🔄" if r.get("repeat") else ""
            done = " ✅" if r.get("done") else ""
            lines.append(f"• {text} {due}{repeat}{done}")
        return "\n".join(lines)
    except Exception as e:
        return f"列出闹钟失败: {e}"

if __name__ == "__main__":
    mcp.run()
