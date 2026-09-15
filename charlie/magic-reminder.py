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
    # 时间解析失败时不创建（否则产生无due的僵尸提醒，永远不会触发）
    if time and time.strip() and due is None:
        return (f"没听懂时间“{time}”。请换个说法，例如：3分钟后、明天下午3点、每天早上8点。")
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

    # 过去时间检查：due 已过（>2分钟）不创建，避免被调度器过期逻辑吞掉
    # （用户会先收到"已设置"再收到"过期未送达"，内容从未正常送达）
    if due and not repeat_clean:
        import datetime as _dtmod
        try:
            overdue = _dtmod.datetime.now() - _dtmod.datetime.fromisoformat(due)
            if overdue > _dtmod.timedelta(minutes=2):
                return (f"“{time_clean}”这个时间已经过了。"
                        f"如果想现在提醒，请说“现在提醒我{text}”；或者换个未来的时间。")
        except Exception:
            pass

    item = append_reminder(text, time_clean, due, repeat=repeat_clean)
    repeat_desc = {"daily": "（每天重复）", "weekly": "（每周重复）", "weekdays": "（工作日重复）"}.get(repeat_clean, "")
    when = f"，时间{due.replace('T', ' ')}" if due else ""
    return f"已添加提醒：{text}{when}{repeat_desc}"


@mcp.tool()
def list_reminders() -> str:
    """查询当前所有未完成的提醒。

    返回每条提醒的编号(id)、内容、时间和重复规则。用户问"我有哪些提醒/提醒列表"时调用。
    """
    log.info("[reminder] list_reminders 被调用")
    try:
        from app.reminders import list_reminders as _list
        items = _list(include_completed=False)
        if not items:
            return "当前没有未完成的提醒。"
        lines = []
        for r in items:
            repeat_desc = {"daily": "（每天）", "weekly": "（每周）", "weekdays": "（工作日）"}.get(r.get("repeat", ""), "")
            lines.append(f"id={r.get('id')}：{r.get('text','')}，时间{str(r.get('due','')).replace('T',' ')}{repeat_desc}")
        return "共有%d条提醒：\n" % len(items) + "\n".join(lines)
    except Exception as e:
        return f"查询提醒失败: {e}"


@mcp.tool()
def delete_reminder(reminder_id: int = 0, text: str = "") -> str:
    """删除（取消）一条提醒。优先用 reminder_id 精确删除；不知道 id 时传 text 用内容模糊匹配。

    例: delete_reminder(reminder_id=1788576370) 或 delete_reminder(text="取快递")
    """
    log.info(f"[reminder] delete_reminder(id={reminder_id}, text={text})")
    try:
        from app.reminders import list_reminders as _list, _load_reminders, _write_locked_reminders
        items = _list(include_completed=False)
        if not items:
            return "当前没有可删除的提醒。"
        target = None
        if reminder_id:
            target = next((r for r in items if r.get("id") == reminder_id), None)
        elif text:
            matches = [r for r in items if text in str(r.get("text", ""))]
            if not matches:
                return f"没有找到内容包含“{text}”的提醒。"
            if len(matches) > 1:
                found = "；".join(f"id={r.get('id')}：{r.get('text','')}" for r in matches)
                return f"找到{len(matches)}条匹配提醒，请确认后用 reminder_id 删除：{found}"
            target = matches[0]
        else:
            return "需要提供 reminder_id 或 text 之一。"
        reminders = _load_reminders()
        for r in reminders:
            if r.get("id") == target.get("id"):
                r["done"] = True
                r["delivery_state"] = "cancelled"
                break
        _write_locked_reminders(reminders)
        return f"已删除提醒：{target.get('text','')}（id={target.get('id')}）"
    except Exception as e:
        return f"删除提醒失败: {e}"


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
