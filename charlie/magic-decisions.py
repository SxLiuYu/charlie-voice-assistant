"""magic-decisions: 自主决策引擎

融合多信号(用户状态 + 时间 + 天气 + 日历 + 记忆 + Protocol)做推理，
自主决定"现在应该做什么"，而不是等用户触发。

v2 改进:
- 决策反馈闭环: 用户对 confirm=True 决策的接受/拒绝被记录并影响未来优先级
- 待确认状态: 决策引擎推送确认请求后, brain() 检测用户回应并记录反馈
"""
import os, json, datetime, time, threading, logging, re

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))


def _get_sleep_hours():
    from agent.preferences import get_preference
    sleep_time = get_preference("sleep_time")  # "22:00"
    wake_time = get_preference("wake_time")    # "07:00"
    try:
        sleep_hour = int(sleep_time.split(":")[0]) if sleep_time else 22
        wake_hour = int(wake_time.split(":")[0]) if wake_time else 7
        return (sleep_hour, wake_hour)
    except (ValueError, IndexError):
        return (22, 7)
DECISIONS_FILE = os.path.join(DATA_DIR, "decision_history.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "decision_feedback.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_confirmation.json")
_decision_lock = threading.Lock()

# 冷却时间: 每个决策每12小时最多触发一次（24h太长，会错过当天窗口）
_COOLDOWN_HOURS = 12
# 反馈阈值: 负面反馈率超过此值, 该规则将被跳过
_NEGATIVE_FEEDBACK_THRESHOLD = 0.6
# 确认等待窗口: 秒（5 分钟，给用户看飞书消息并回复的时间）
_CONFIRMATION_WINDOW = 300

# ===== 决策规则 =====
_DECISION_RULES = [
    {
        "id": "late_night_sleep",
        "priority": 90,
        "condition": {
            "states": ["home_resting", "home_sleeping"],
            "hours": _get_sleep_hours,  # callable，evaluate 时动态求值
            "check_desc": "深夜+休息状态",
        },
        "action": {"type": "protocol", "name": "goodnight", "text": "已经很晚了，要帮你执行晚安场景吗？"},
        "confirm": True,
    },
    {
        "id": "morning_wakeup",
        "priority": 80,
        "condition": {
            "states": ["home_awake", "working"],
            "hours": (7, 9),
            "check_desc": "早上活跃状态，播报早安简报",
            "extra_check": "morning_briefing_check",
        },
        "action": {"type": "tts", "text": ""},
        "confirm": False,
    },
    {
        "id": "leaving_reminder",
        "priority": 70,
        "condition": {
            "states": ["away"],
            "hours": (6, 23),
            "check_desc": "检测到出门",
        },
        "action": {"type": "protocol", "name": "leaving_home", "text": "要出门了吗，需要我做什么准备吗？"},
        "confirm": True,
    },
    {
        "id": "lunch_reminder",
        "priority": 40,
        "condition": {
            "states": ["home_awake", "working"],
            "hours": (11, 13),
            "check_desc": "午饭时间+在家/工作",
        },
        "action": {"type": "tts", "text": "到午饭时间了，注意吃饭。"},
        "confirm": False,
    },
    {
        "id": "deadline_reminder",
        "priority": 60,
        "condition": {
            "states": ["home_awake", "working", "home_resting"],
            "hours": (0, 24),
            "check_desc": "记忆中有截止日期",
            "extra_check": "deadline_check",
        },
        "action": {"type": "tts", "text": "您有一个截止日期快到了，记得检查进度。"},
        "confirm": False,
    },
    {
        "id": "evening_wind_down",
        "priority": 50,
        "condition": {
            "states": ["home_awake", "home_resting"],
            "hours": (21, 22),
            "check_desc": "晚间+在家",
        },
        "action": {"type": "tts", "text": "已经晚上9点了，该准备休息了。"},
        "confirm": False,
    },
    {
        "id": "weather_alert",
        "priority": 85,
        "condition": {
            "states": ["home_awake", "working", "home_resting", "away"],
            "hours": (0, 24),
            "check_desc": "极端天气或空气质量预警",
            "extra_check": "weather_alert_check",
        },
        "action": {"type": "tts", "text": ""},
        "confirm": False,
    },
    {
        "id": "sedentary_break",
        "priority": 35,
        "condition": {
            "states": ["working"],
            "hours": (9, 21),
            "check_desc": "工作状态持续90分钟，提醒休息",
            "extra_check": "sedentary_check",
        },
        "action": {"type": "tts", "text": "已经连续工作一个半小时了，站起来活动一下吧。"},
        "confirm": False,
    },
    {
        "id": "evening_wrapup",
        "priority": 45,
        "condition": {
            "states": ["home_awake", "home_resting"],
            "hours": (22, 23),
            "check_desc": "晚间复盘",
            "extra_check": "evening_wrapup_check",
        },
        "action": {"type": "tts", "text": ""},
        "confirm": False,
    },
    {
        "id": "arrive_home",
        "priority": 65,
        "condition": {
            "states": ["home_awake"],
            "hours": (17, 22),
            "check_desc": "刚回家，播报天气+待办",
            "extra_check": "arrive_home_check",
        },
        "action": {"type": "tts", "text": ""},
        "confirm": False,
    },
    {
        "id": "meeting_reminder",
        "priority": 75,
        "condition": {
            "states": ["home_awake", "working"],
            "hours": (0, 24),
            "check_desc": "日历中有即将开始的会议",
            "extra_check": "calendar_check",
        },
        "action": {"type": "tts", "text": "您有一个会议即将开始。"},
        "confirm": False,
    },
    {
        "id": "casual_checkin",
        "priority": 10,
        "condition": {
            "states": ["home_awake", "working", "home_resting", "unknown"],
            "hours": (9, 24),
            "check_desc": "长时间无互动，结合记忆/天气/待办的智能问候",
            "extra_check": "contextual_greeting_check",
        },
        "action": {"type": "tts", "text": ""},
        "confirm": False,
    },
]

# ===== 反馈数据 =====

_feedback_cache: dict | None = None
_feedback_cache_mtime: float = 0


def _load_feedback() -> dict:
    """加载反馈数据: {rule_id: {positive: N, negative: N}}
    注意: 调用方必须已持有 _decision_lock。带文件 mtime 缓存。"""
    global _feedback_cache, _feedback_cache_mtime
    try:
        mtime = os.path.getmtime(FEEDBACK_FILE) if os.path.exists(FEEDBACK_FILE) else 0
    except OSError:
        mtime = 0
    if _feedback_cache is not None and mtime == _feedback_cache_mtime:
        return _feedback_cache
    try:
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {}
    except Exception as e:
        log.debug(f"[decision] 加载反馈失败: {e}")
        data = {}
    _feedback_cache = data
    _feedback_cache_mtime = mtime
    return data


def _save_feedback(feedback: dict):
    """保存反馈数据。注意: 调用方必须已持有 _decision_lock"""
    global _feedback_cache
    try:
        with open(FEEDBACK_FILE, 'w', encoding='utf-8') as f:
            json.dump(feedback, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"[decision] 保存反馈失败: {e}")
    _feedback_cache = None  # 使缓存失效


def record_feedback(rule_id: str, is_positive: bool):
    """记录用户对决策的反馈: True=接受, False=拒绝（原子 read-modify-write）"""
    with _decision_lock:
        feedback = _load_feedback()
        entry = feedback.get(rule_id, {"positive": 0, "negative": 0})
        if is_positive:
            entry["positive"] = entry.get("positive", 0) + 1
        else:
            entry["negative"] = entry.get("negative", 0) + 1
        feedback[rule_id] = entry
        _save_feedback(feedback)
    log.info(f"[decision] 反馈记录: {rule_id} {'正面' if is_positive else '负面'} "
             f"(正面{entry['positive']}, 负面{entry['negative']})")


def _get_feedback_score(rule_id: str) -> float:
    """获取规则的反馈评分: 0~1, 越高越好。无反馈数据的规则默认 0.5"""
    with _decision_lock:
        feedback = _load_feedback()
    entry = feedback.get(rule_id, {})
    pos = entry.get("positive", 0)
    neg = entry.get("negative", 0)
    total = pos + neg
    if total == 0:
        return 0.5  # 无数据, 中性
    return pos / total


def _get_effective_priority(rule_id: str, base_priority: int) -> int:
    """根据反馈评分动态调整规则优先级: score越低，优先级衰减越多"""
    score = _get_feedback_score(rule_id)
    # score 0.0 → 优先级×0.1, score 0.5 → 优先级×0.6, score 1.0 → 优先级×1.0
    adjusted = base_priority * (0.1 + 0.9 * score)
    return max(1, int(round(adjusted)))


def _should_skip_rule(rule_id: str) -> bool:
    """检查是否应该跳过此规则 (负面反馈过多)"""
    score = _get_feedback_score(rule_id)
    return score < (1.0 - _NEGATIVE_FEEDBACK_THRESHOLD)


# ===== 待确认状态 (文件持久化, 跨进程共享) =====

def _load_pending(user_id: str | None = None) -> dict | None:
    """加载待确认状态

    user_id: 如果提供, 只返回匹配该 user_id 的 pending (跨用户隔离)
    格式: feishu_{sender_id} 中的 sender_id 部分
    """
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("rule_id"):
                # user_id 过滤：仅当 user_id 明确传入时才做匹配检查
                if user_id is not None and data.get("user_id") != user_id:
                    return None
                # 检查是否过期
                elapsed = time.time() - data.get("timestamp", 0)
                if elapsed < _CONFIRMATION_WINDOW:
                    return data
                else:
                    os.remove(PENDING_FILE)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"[decision] op failed: {e}")
    return None


def _save_pending(rule_id: str, text: str, user_id: str = ""):
    """保存待确认状态

    user_id: 触发此确认的用户标识 (飞书场景为 open_id, 格式 feishu_{sender_id} 中的 sender_id)
    """
    data = {
        "rule_id": rule_id,
        "text": text,
        "timestamp": time.time(),
        "user_id": user_id,
    }
    try:
        with open(PENDING_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"[decision] op failed: {e}")


def set_pending_confirmation(rule_id: str, text: str, user_id: str = ""):
    """设置待确认决策: 用户需要回应此决策

    user_id: 触发此确认的用户标识
    """
    _save_pending(rule_id, text, user_id=user_id)
    log.info(f"[decision] 待确认: {rule_id} -> {text[:50]} (user_id={user_id})")


def get_pending_confirmation(user_id: str | None = None) -> dict | None:
    """获取当前待确认的决策, 返回 {rule_id, text, timestamp, user_id} 或 None

    user_id: 如果提供, 只返回匹配该 user_id 的 pending
    """
    return _load_pending(user_id=user_id)


def clear_pending_confirmation():
    """清除待确认状态"""
    try:
        if os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"[decision] op failed: {e}")


# ===== 决策历史 =====

def _load_decision_history() -> dict:
    with _decision_lock:
        try:
            if os.path.exists(DECISIONS_FILE):
                with open(DECISIONS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.debug(f"[decision] op failed: {e}")
        return {}


def _save_decision_history(history: dict):
    with _decision_lock:
        try:
            with open(DECISIONS_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.debug(f"[decision] op failed: {e}")


def _check_cooldown(rule_id: str, history: dict) -> bool:
    """True=可以触发"""
    now = time.time()
    last = history.get(rule_id, {}).get("last_trigger", 0)
    elapsed = now - last
    cooldown = _COOLDOWN_HOURS * 3600
    return elapsed > cooldown or last == 0


def _mark_triggered(rule_id: str, history: dict):
    history[rule_id] = {
        "last_trigger": time.time(),
        "trigger_time": datetime.datetime.now().isoformat(),
    }
    _save_decision_history(history)


def mark_triggered(rule_id: str) -> None:
    """公开接口：标记规则已触发（写冷却历史）"""
    history = _load_decision_history()
    _mark_triggered(rule_id, history)


def _calendar_check() -> str | None:
    """检查飞书日历中是否有即将开始的会议, 返回事件摘要或None"""
    try:
        import os as _os, requests as _req
        feishu_id = _os.getenv("FEISHU_APP_ID", "")
        feishu_secret = _os.getenv("FEISHU_APP_SECRET", "")
        if not feishu_id or not feishu_secret:
            return None
        # 获取 token
        r = _req.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": feishu_id, "app_secret": feishu_secret}, timeout=10)
        token = r.json().get("tenant_access_token", "")
        if not token:
            return None
        # 获取日历列表
        r = _req.get("https://open.feishu.cn/open-apis/calendar/v4/calendars",
            headers={"Authorization": f"Bearer {token}"}, timeout=10)
        calendars = r.json().get("data", {}).get("calendar_list", [])
        if not calendars:
            return None
        cal_id = calendars[0].get("calendar_id", "primary")
        # 获取今天的事件
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0).isoformat() + "+08:00"
        end = now.replace(hour=23, minute=59, second=59).isoformat() + "+08:00"
        r = _req.get(f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{cal_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"start_time": start, "end_time": end, "page_size": 20}, timeout=10)
        events = r.json().get("data", {}).get("items", [])
        if not events:
            return None
        now_ts = now.timestamp()
        for e in events:
            start_str = e.get("start_time", {}).get("date_time", "")
            if not start_str:
                continue
            try:
                event_start = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00")).timestamp()
                diff_min = (event_start - now_ts) / 60
                # 15-30分钟内即将开始的会议
                if 10 <= diff_min <= 30:
                    summary = e.get("summary", "无标题")
                    loc = e.get("location", "")
                    loc_str = f" ({loc})" if loc else ""
                    return f"会议即将开始: {summary}{loc_str}"
            except (KeyError, TypeError, ValueError) as e:
                log.debug(f"[decision] 日历事件解析跳过: {e}")
                continue
        return None
    except (OSError, KeyError, TypeError) as e:
        log.debug(f"[decision] 日历检查失败: {e}")
        return None

def _deadline_check() -> str | None:
    """检查记忆中是否有近期截止日期"""
    try:
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("magic_memory", os.path.join(DATA_DIR, "magic-memory.py"))
        if spec and spec.loader:
            mod = _iu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            memories = mod.get_relevant_memories("截止", limit=3)
            for m in memories:
                summary = m.get("summary", "")
                # 过滤纯数字/过短内容，避免推"提醒：12，记得检查进度"
                clean = summary.strip()
                if len(clean) < 6:
                    continue
                if clean.isdigit():
                    continue
                if "deadline" in m.get("tags", []) or "截止" in clean:
                    return clean
    except Exception as e:
        log.debug(f"[decision] deadline_check 异常: {e}")
    return None


# 天气预警去重
_last_weather_alert = {"text": "", "ts": 0}


def _weather_alert_check() -> str | None:
    """检查极端天气/空气质量预警"""
    global _last_weather_alert
    now = time.time()
    # 同一预警 6 小时内不重复
    if _last_weather_alert["ts"] and now - _last_weather_alert["ts"] < 21600:
        return None
    try:
        from app.weather import get_weather_alerts
        alerts = get_weather_alerts()
        if alerts:
            _last_weather_alert = {"text": alerts[0], "ts": now}
            return alerts[0]
    except Exception as e:
        log.debug(f"[decision] weather_alert_check 异常: {e}")
    return None


def _morning_briefing_check() -> str | None:
    """早安简报：天气+日程+待办+新闻"""
    try:
        from app.briefing import morning_briefing
        text = morning_briefing()
        if text and len(text) > 10:
            return text
    except Exception as e:
        log.debug(f"[decision] morning_briefing_check 异常: {e}")
    return None


# 久坐提醒去重：记录上次触发时间
_last_sedentary_alert = 0


def _sedentary_check() -> str | None:
    """检查是否持续工作超过90分钟"""
    global _last_sedentary_alert
    now = time.time()
    # 2小时内不重复
    if now - _last_sedentary_alert < 7200:
        return None
    try:
        from agent.state import get_user_state
        state = get_user_state()
        if state.get("state") != "working":
            return None
        # last_voice_activity 距今的秒数
        last_voice = state.get("last_voice_activity", now)
        working_duration = now - last_voice
        if working_duration >= 5400:  # 90分钟
            _last_sedentary_alert = now
            return "已经连续工作一个半小时了，站起来活动一下吧。"
    except Exception as e:
        log.debug(f"[decision] sedentary_check 异常: {e}")
    return None


# 晚间复盘和回家去重
_last_wrapup = 0
_last_arrive = 0
_previous_state = "unknown"


def _evening_wrapup_check() -> str | None:
    """晚间复盘：今日完成+明日待办+天气"""
    global _last_wrapup
    now = time.time()
    if now - _last_wrapup < 43200:  # 12小时不重复
        return None
    try:
        from app.briefing import evening_wrapup
        text = evening_wrapup()
        if text:
            _last_wrapup = now
            return text
    except Exception as e:
        log.debug(f"[decision] evening_wrapup 异常: {e}")
    return None


def _arrive_home_check() -> str | None:
    """回家场景：检测从 away → home_awake 的状态转换"""
    global _last_arrive, _previous_state
    now = time.time()
    if now - _last_arrive < 14400:  # 4小时不重复
        return None
    try:
        from agent.state import get_user_state
        current = get_user_state().get("state", "unknown")
        just_arrived = _previous_state == "away" and current == "home_awake"
        _previous_state = current
        if just_arrived:
            _last_arrive = now
            from app.weather import get_weather_text
            weather = get_weather_text()
            parts = ["欢迎回家"]
            if weather:
                parts.append(weather)
            # 待办
            try:
                from app.reminders import list_reminders
                today = time.strftime("%Y-%m-%d")
                due = [r for r in list_reminders(include_completed=False)
                       if not r.get("done") and r.get("due", "") <= today]
                if due:
                    parts.append(f"还有{len(due)}项待办")
            except (OSError, KeyError, TypeError) as e:
                log.debug(f"[decision] 待办检查跳过: {e}")
            return "，".join(parts) + "。"
    except Exception as e:
        log.debug(f"[decision] arrive_home 异常: {e}")
    return None


def _contextual_greeting_check() -> str | None:
    """上下文感知问候：结合天气/待办/记忆生成一句话"""
    try:
        parts = []

        # 天气变化提醒
        try:
            from app.weather import get_weather_text
            w = get_weather_text()
            if w and ("雨" in w or "雪" in w):
                parts.append(f"外面{w}，记得带伞")
        except (OSError, TypeError) as e:
            log.debug(f"[decision] 天气检查跳过: {e}")

        # 待办提醒
        try:
            from app.reminders import list_reminders
            today = time.strftime("%Y-%m-%d")
            due = [r for r in list_reminders(include_completed=False)
                   if not r.get("done") and r.get("due", "") <= today]
            if due:
                parts.append(f"你还有{len(due)}项待办没完成")
        except (OSError, KeyError, TypeError) as e:
            log.debug(f"[decision] 待办检查跳过: {e}")

        if parts:
            return "，".join(parts) + "。"

        # 没有特殊上下文时返回 None，让 casual_checkin 走随机问候
        return None
    except Exception as e:
        log.debug(f"[decision] contextual_greeting_check 异常: {e}")
        return None


# ===== 核心评估 =====

def evaluate(user_state: dict, protocol_executor=None) -> list:
    """评估当前状态, 返回需要执行的决策列表(按优先级排序, 跳过负面反馈规则)"""
    decisions = []
    state = user_state.get("state", "unknown")
    now_ts = time.time()
    hour = datetime.datetime.now().hour
    history = _load_decision_history()

    # 假日检查：公共假日跳过工作相关规则（Nager.Date 免费API，无需Key）
    _holiday_rules_skip = {"morning_wakeup", "leaving_reminder", "lunch_reminder", "meeting_reminder"}
    try:
        from app.holiday import is_holiday
        _is_holiday = is_holiday()
        if _is_holiday:
            log.info("[decision] 今天是公共假日，跳过工作相关规则")
    except (ImportError, OSError, TypeError) as e:
        log.debug(f"[decision] 假日检查跳过: {e}")
        _is_holiday = False

    for rule in _DECISION_RULES:
        cond = rule["condition"]
        # 0. 反馈检查: 跳过负面反馈过多的规则
        if _should_skip_rule(rule["id"]):
            continue
        # 0.5 假日跳过工作相关规则
        if _is_holiday and rule["id"] in _holiday_rules_skip:
            continue
        # 1. 状态匹配
        if state not in cond["states"]:
            continue
        # 2. 时间范围（支持 callable 动态求值，如 sleep_time 偏好）
        _hours = cond["hours"]
        if callable(_hours):
            start_h, end_h = _hours()
        else:
            start_h, end_h = _hours
        if start_h <= end_h:
            if not (start_h <= hour < end_h):
                continue
        else:
            if not (hour >= start_h or hour < end_h):
                continue
        # 3. 冷却检查
        if not _check_cooldown(rule["id"], history):
            continue
        # 4. 额外检查
        if rule["id"] == "casual_checkin":
            # 随机问候：4小时未互动才触发，且随机概率避免每2分钟都试
            import random as _rand
            last_brain = history.get("casual_checkin", {}).get("last_trigger", 0)
            if now_ts - last_brain < 4 * 3600:  # 4小时冷却
                continue
            if _rand.random() > 0.3:  # 30% 概率触发，避免每次评估都搭话
                continue
            greetings = [
                "好久没聊了，最近怎么样？",
                "嘿，有什么需要我帮忙的吗？",
                "我在呢，随时可以聊。",
                "刚想到一个有趣的事想跟你说。",
                "你还在忙吗？要不要休息一下？",
            ]
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = _rand.choice(greetings)
        if cond.get("extra_check") == "deadline_check":
            deadline = _deadline_check()
            if not deadline:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = f"提醒：{deadline}，记得检查进度。"
        if cond.get("extra_check") == "weather_alert_check":
            alert = _weather_alert_check()
            if not alert:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = alert
        if cond.get("extra_check") == "morning_briefing_check":
            briefing = _morning_briefing_check()
            if briefing:
                rule = dict(rule)
                rule["action"] = dict(rule["action"])
                rule["action"]["text"] = briefing
            else:
                continue
        if cond.get("extra_check") == "sedentary_check":
            sedentary = _sedentary_check()
            if not sedentary:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = sedentary
        if cond.get("extra_check") == "evening_wrapup_check":
            wrapup = _evening_wrapup_check()
            if not wrapup:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = wrapup
        if cond.get("extra_check") == "arrive_home_check":
            arrive = _arrive_home_check()
            if not arrive:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = arrive
        if cond.get("extra_check") == "contextual_greeting_check":
            greeting = _contextual_greeting_check()
            if greeting:
                rule = dict(rule)
                rule["action"] = dict(rule["action"])
                rule["action"]["text"] = greeting
            else:
                # 无特殊上下文，用随机问候
                greetings = [
                    "好久不见，最近怎么样？",
                    "在忙什么呢？",
                    "有什么我能帮你的吗？",
                    "休息一下吧，别太累了。",
                ]
                rule = dict(rule)
                rule["action"] = dict(rule["action"])
                rule["action"]["text"] = _rand.choice(greetings)
        if cond.get("extra_check") == "calendar_check":
            event = _calendar_check()
            if not event:
                continue
            rule = dict(rule)
            rule["action"] = dict(rule["action"])
            rule["action"]["text"] = event
        decisions.append(rule)

    decisions.sort(key=lambda r: r["priority"], reverse=True)
    return decisions


def execute_decision(rule: dict, protocol_executor, confirmed: bool = False) -> str:
    """执行决策, 返回结果文字

    confirmed=True 时跳过 protocol 的 safe 检查（用户已通过飞书/语音明确确认）。
    """
    action = rule["action"]
    action_type = action["type"]
    history = _load_decision_history()
    _mark_triggered(rule["id"], history)

    # 安全分类：protocol 类型默认需确认（除非标记 safe: True 或 confirmed=True）
    if action_type == "protocol" and not rule.get("safe") and not confirmed:
        proto_name = action.get("name", "未知场景")
        log.info(f"[decision] Protocol {proto_name} 需要人工确认，跳过自动执行")
        return f"「{proto_name}」需要确认，请通过飞书通知决定是否执行。"

    if action_type == "protocol":
        try:
            result = protocol_executor(action["name"])
            cond = rule.get("condition", {})
            hours = cond.get("hours")
            if callable(hours):
                try:
                    hours = list(hours())
                except Exception:
                    hours = "<callable>"
            log.info(f"[decision] 执行Protocol: {action['name']} (触发规则={rule['id']}, state={cond.get('states')}, hours={hours}) -> {result[:80]}")
            return result
        except Exception as e:
            log.warning(f"[decision] Protocol执行失败: {e}")
            return f"执行{action['name']}失败: {e}"
    elif action_type == "tts":
        text = action["text"]
        log.info(f"[decision] TTS消息: {text}")
        return text
    elif action_type == "reminder":
        log.info(f"[decision] 设置提醒: {action.get('text', '')}")
        return f"已设提醒: {action.get('text', '')}"
    return ""


def decisions_summary() -> str:
    """返回决策引擎状态摘要"""
    history = _load_decision_history()
    with _decision_lock:
        feedback = _load_feedback()
    lines = [f"决策规则: {len(_DECISION_RULES)} 条"]
    now = time.time()
    for rule in _DECISION_RULES:
        rid = rule["id"]
        last = history.get(rid, {})
        last_time = last.get("trigger_time", "从未触发")
        remaining = ""
        if last.get("last_trigger"):
            elapsed = now - last["last_trigger"]
            remaining_h = max(0, _COOLDOWN_HOURS - elapsed / 3600)
            remaining = f" (剩余冷却: {remaining_h:.0f}h)" if remaining_h > 0 else " (可触发)"
        fb = feedback.get(rid, {})
        f_pos = fb.get("positive", 0)
        f_neg = fb.get("negative", 0)
        fb_str = ""
        if f_pos + f_neg > 0:
            fb_str = f" [反馈: +{f_pos}/-{f_neg}]"
        skip = " [已跳过]" if _should_skip_rule(rid) else ""
        lines.append(f"  [{rid}] 优先级{rule['priority']}, 上次: {last_time}{remaining}{fb_str}{skip}")
    return "\n".join(lines)


def get_feedback_summary() -> dict:
    """返回反馈摘要给前端"""
    with _decision_lock:
        feedback = _load_feedback()
    result = {}
    for rule in _DECISION_RULES:
        rid = rule["id"]
        fb = feedback.get(rid, {"positive": 0, "negative": 0})
        score = _get_feedback_score(rid)
        result[rid] = {
            "positive": fb.get("positive", 0),
            "negative": fb.get("negative", 0),
            "score": round(score, 2),
            "skipped": _should_skip_rule(rid),
        }
    pending = get_pending_confirmation()
    if pending:
        result["_pending"] = pending
    return result


def get_rules() -> list:
    """返回所有决策规则(供前端展示)"""
    result = []
    for r in _DECISION_RULES:
        cond = dict(r["condition"])
        hours = cond.get("hours")
        if callable(hours):
            try:
                cond["hours"] = list(hours())
            except Exception:
                cond["hours"] = []
            cond["hours_dynamic"] = True
        result.append({
            "id": r["id"],
            "priority": r["priority"],
            "condition": cond,
            "action": r["action"],
            "confirm": r["confirm"],
        })
    return result


# ===== 确认/拒绝反馈检测 =====

# 完整短语匹配（head 模式，已由调用方提取不含标点的纯文本前缀）
# 短句（<=10字）才走匹配；长句（>10字）直接交给 brain 处理
_POSITIVE_PATTERNS = re.compile(
    r'^(好的?|是的?|可以|执行|确认|同意|没问题|行吧?|要|好|行|嗯|嗯嗯)$'
)
_NEGATIVE_PATTERNS = re.compile(
    r'^(不用了?|取消|不要了?|算了|不了|拒绝|别了?|停|不用|不|别)$'
)


def check_feedback(text: str, user_id: str | None = None, session_id: str = "default") -> str | None:
    """检测用户对 pending 确认决策的反馈。

    参数:
      text: 用户回复文本
      user_id: 用户标识（飞书场景为 sender_id，格式 "ou_xxx"）。只匹配同名
               user_id 的 pending，不同用户的 pending 互不干扰。
               session_id 参数保留兼容，如果 user_id 为 None 则用 session_id 推导。
      session_id: 兼容旧调用，user_id 未传时忽略此参数。

    返回值:
      - str: 已匹配到确认/拒绝，返回回复文本
      - None: 无待确认决策或文本不匹配，交给正常 brain 流程
    """
    pending = get_pending_confirmation(user_id=user_id)
    if not pending:
        return None

    rid = pending.get("rule_id", "")
    rule = next((r for r in _DECISION_RULES if r["id"] == rid), None)
    if not rule:
        clear_pending_confirmation()
        return None

    text_stripped = text.strip()

    # 短句（≤7字）才走正则匹配；长句（>7字）直接交给 brain，避免误判
    # 确认词最长"没问题"3字+标点1字=4字，7字阈值过滤"好的，今天天气怎么样"这类长句
    if len(text_stripped) > 7:
        return None

    text_lower = text_stripped.lower()

    # "好的，xxx" 格式：如果"好的"后紧跟逗号+内容，需区分"确认意图"还是"寒暄"
    # "好的，执行吧" → 有动作词，算确认（走后续 regex 判断）
    # "好的，今天天气怎么样" → 无动作词，视为非确认，交 brain
    # "好的，确认" → "确认"在正面词列表，算确认
    _action_after_hao = re.match(r'^好的[,，](.+)$', text_lower)
    if _action_after_hao:
        tail = _action_after_hao.group(1)
        # 如果尾部不包含任何确认/动作词，视为非确认
        _action_keywords = {"执行", "确认", "同意", "可以", "好", "行", "没问题", "是", "嗯"}
        if not any(kw in tail for kw in _action_keywords):
            return None

    # 提取句首词（含句首标点），忽略尾部补充文字
    # "好的，执行吧" → "好的"；"不用了谢谢" → "不用了"
    head = ""
    for ch in text_lower:
        if '\u4e00' <= ch <= '\u9fff' or ch.isalnum():
            head += ch
        else:
            break  # 遇到标点停止

    # 先判负面（"不用"包含"用"但应为拒绝；注意"不用"也在正面列表所以先判负面）
    if _NEGATIVE_PATTERNS.match(head):
        record_feedback(rid, False)
        clear_pending_confirmation()
        log.info(f"[decision] 用户拒绝: {rid} (text={text[:30]})")
        return f"好的，已取消「{rule['action'].get('name', rid)}」。"

    if _POSITIVE_PATTERNS.match(head):
        # 用户确认 → 执行协议
        record_feedback(rid, True)
        clear_pending_confirmation()
        log.info(f"[decision] 用户确认: {rid} (text={text[:30]})")
        try:
            from app import load_magic_module
            _scene = load_magic_module("magic_scenes", "magic-scenes.py")
            if not _scene:
                return f"「{rule['action'].get('name', rid)}」场景模块不可用。"
            result = execute_decision(rule, _scene.execute_protocol, confirmed=True)
            log.info(f"[decision] 执行结果: {result[:80] if result else '(空)'}")
            return result or f"「{rule['action'].get('name', rid)}」已执行。"
        except Exception as e:
            log.warning(f"[decision] 确认后执行失败: {e}")
            return f"执行失败: {e}"

    # 不匹配确认/拒绝 → 返回 None 交给正常对话
    return None


if __name__ == "__main__":
    print("决策引擎测试:")
    print(decisions_summary())
    print()
    print("反馈摘要:")
    print(json.dumps(get_feedback_summary(), ensure_ascii=False, indent=2))
    print()
    test_state = {"state": "home_awake", "confidence": 0.7}
    print(f"状态: {test_state['state']}, 时间: {datetime.datetime.now().hour}:00")
    results = evaluate(test_state)
    print(f"触发决策: {len(results)} 条")
    for r in results:
        print(f"  [{r['id']}] 优先级{r['priority']} -> {r['action'].get('type', '')}: {r['action'].get('text', '') or r['action'].get('name', '')}")
