"""习惯追踪 MCP — 语音打卡喝水/运动/冥想等

数据存储: data/habits.json
结构: {habit_name: {"logs": ["2026-08-12", ...], "created": "2026-08-12"}}
"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-habits",
    "tier": "optional",
    "required_env": [],
    "label": "习惯追踪"
}

import os
import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("magic")

DATA_DIR = os.getenv("ASSISTANT_KID_DATA_DIR",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
HABITS_FILE = os.path.join(DATA_DIR, "habits.json")


def _load_habits() -> dict:
    try:
        if os.path.exists(HABITS_FILE):
            with open(HABITS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_habits(habits: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HABITS_FILE + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(habits, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HABITS_FILE)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _streak(logs: list[str]) -> int:
    """计算连续打卡天数"""
    if not logs:
        return 0
    dates = sorted(set(logs), reverse=True)
    today = _today()
    if dates[0] != today and dates[0] != (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
        return 0
    count = 0
    check = datetime.now().date()
    if dates[0] != today:
        check -= timedelta(days=1)
    for d in dates:
        if d == check.strftime("%Y-%m-%d"):
            count += 1
            check -= timedelta(days=1)
        else:
            break
    return count


# ===== MCP 工具 =====

try:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("habits")

    @mcp.tool()
    def log_habit(habit: str) -> str:
        """记录习惯完成。habit=习惯名(喝水/运动/冥想/阅读/早睡等)

        例: log_habit("喝水") → 记录今天已喝水
        """
        habits = _load_habits()
        today = _today()
        if habit not in habits:
            habits[habit] = {"logs": [], "created": today}
        if today not in habits[habit]["logs"]:
            habits[habit]["logs"].append(today)
            _save_habits(habits)
            streak = _streak(habits[habit]["logs"])
            return f"已记录{habit}，连续{streak}天，继续保持！"
        else:
            streak = _streak(habits[habit]["logs"])
            return f"今天已经记录过{habit}了（连续{streak}天）"

    @mcp.tool()
    def habit_streak(habit: str = "") -> str:
        """查询习惯连续打卡天数

        例: habit_streak("运动") → 运动已连续5天
            habit_streak("") → 所有习惯概览
        """
        habits = _load_habits()
        if not habits:
            return "还没有记录任何习惯，试试说\"记录喝水\""
        if habit:
            if habit not in habits:
                return f"还没有记录过{habit}"
            streak = _streak(habits[habit]["logs"])
            return f"{habit}已连续{streak}天" if streak > 0 else f"{habit}还没有连续打卡"
        lines = []
        for name, data in habits.items():
            s = _streak(data["logs"])
            today_done = _today() in data["logs"]
            mark = "✅" if today_done else "⬜"
            lines.append(f"{mark} {name}: 连续{s}天")
        return "；".join(lines)

    @mcp.tool()
    def habit_today() -> str:
        """今日习惯完成概览"""
        habits = _load_habits()
        if not habits:
            return "今天没有习惯记录"
        today = _today()
        done = []
        pending = []
        for name, data in habits.items():
            if today in data["logs"]:
                done.append(name)
            else:
                pending.append(name)
        parts = []
        if done:
            parts.append(f"已完成: {'、'.join(done)}")
        if pending:
            parts.append(f"待完成: {'、'.join(pending)}")
        return "；".join(parts) if parts else "今天没有习惯"

    @mcp.tool()
    def habit_week(habit: str = "") -> str:
        """查看本周习惯打卡情况

        例: habit_week("运动") → 本周运动打卡3天
        """
        habits = _load_habits()
        if not habits:
            return "还没有习惯记录"
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        if habit:
            if habit not in habits:
                return f"还没有记录过{habit}"
            logs = set(habits[habit]["logs"])
            count = sum(1 for i in range(7)
                       if (week_start + timedelta(days=i)).strftime("%Y-%m-%d") in logs)
            return f"本周{habit}打卡{count}/7天"
        lines = []
        for name, data in habits.items():
            logs = set(data["logs"])
            count = sum(1 for i in range(7)
                       if (week_start + timedelta(days=i)).strftime("%Y-%m-%d") in logs)
            lines.append(f"{name}: {count}/7")
        return "；".join(lines)

except ImportError:
    log.debug("[habits] mcp 未安装，工具不注册")
else:
    if __name__ == "__main__":
        mcp.run()
