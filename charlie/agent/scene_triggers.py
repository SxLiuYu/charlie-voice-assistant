"""scene_triggers.py: 场景自动触发引擎 v1

在 _proactive_suggestions 的 state-change 触发之外，
增加基于时间 + 天气的定时触发能力。

触发来源:
1. 用户状态变化（home_sleeping / away / home_awake）— 已有 proactive_suggestions 处理
2. 固定时间触发 — 此处新增
3. 天气条件触发 — 此处新增

设计约束:
- 不做有副作用的自动操作（如关空调、关电视），仅通知 + 播报
- 同场景 1 小时冷却（与 proactive_suggestions 共享 cooldown）
- 每日去重（与 proactive_suggestions 共享 claim_suggest_state）
- 环境变量 SCENE_TRIGGERS_ENABLED=0 可全局禁用
"""
import os, json, re, logging, time, datetime
from typing import Optional

log = logging.getLogger("magic")

# 全局开关
SCENE_TRIGGERS_ENABLED = os.getenv("SCENE_TRIGGERS_ENABLED", "1") == "1"

# 冷却时间（秒）：同一场景至少间隔 1 小时
_TRIGGER_COOLDOWN = 3600

# 内置时间触发器定义
_BUILTIN_TIME_TRIGGERS = {
    # key → (hour, minute, label, scene_key, message_template)
    "goodnight_22_30": (22, 30, "晚安提醒", "goodnight",
                       "主人，现在是 22:30，需要启动晚安模式吗？说「晚安」即可。"),
    "morning_08_00": (8, 0, "早安提醒", "good_morning",
                      "主人，早上好！说「早上好」启动早安模式。"),
    "movie_19_30": (19, 30, "电影提醒", "movie_time",
                    "主人，建议观影时间到了，说「看电影」启动电影模式。"),
}

# 天气触发器定义
_BUILTIN_WEATHER_TRIGGERS = {
    # key → (keyword, time_range_start, time_range_end, label, message_template)
    "rain_umbrella": ("雨", "06:00", "22:00", "带伞提醒",
                      "主人，今天有雨，出门记得带伞哦。"),
    "snow_warning": ("雪", "06:00", "22:00", "雪天提醒",
                     "主人，今天有雪，出门注意保暖和安全。"),
    "high_temp": ("高温", "10:00", "16:00", "高温提醒",
                  "主人，今天气温高，注意防暑降温。"),
    "cold_warning": ("低温", "06:00", "08:00", "低温提醒",
                     "主人，今天气温低，注意保暖，多穿衣服。"),
}


class SceneTriggerEngine:
    """定时 + 天气场景触发器"""

    def __init__(self):
        self._last_fired: dict[str, float] = {}  # key → 上次触发时间戳

    def check_time_triggers(self, now: datetime.datetime) -> list[dict]:
        """检查是否有时间触发器匹配

        返回触发列表: [{"key": str, "scene": str, "message": str, "fired": bool}, ...]
        """
        if not SCENE_TRIGGERS_ENABLED:
            return []

        current_hhmm = now.strftime("%H:%M")
        today_str = now.strftime("%Y-%m-%d")
        fired = []

        for key, (hour, minute, label, scene_key, msg) in _BUILTIN_TIME_TRIGGERS.items():
            trigger_time = f"{hour:02d}:{minute:02d}"
            if current_hhmm != trigger_time:
                continue

            # 冷却检查（1小时内不重复触发）
            last = self._last_fired.get(key, 0)
            if time.time() - last < _TRIGGER_COOLDOWN:
                continue

            # 每日一次持久化去重（进程重启后冷却失效，用磁盘持久化兜底）
            try:
                from app.reminders import _save_suggest_state
                daily_key = f"last_scene_trigger_{key}"
                state = self._read_trigger_dedup()
                if state.get(daily_key) == today_str:
                    continue  # 今日已触发
                state[daily_key] = today_str
                self._write_trigger_dedup(state)
            except Exception:
                pass  # 去重非关键路径，失败跳过

            self._last_fired[key] = time.time()
            fired.append({
                "key": key,
                "label": label,
                "scene": scene_key,
                "message": msg,
                "fired": True,
            })
            log.info(f"[scene_triggers] 时间触发: {label} ({trigger_time})")

        return fired

    def check_weather_triggers(self, now: datetime.datetime, weather_text: str) -> list[dict]:
        """检查天气触发器

        weather_text: 当天天气描述（如 "晴 25度"、"小雨转多云 18到22度"）
        """
        if not SCENE_TRIGGERS_ENABLED:
            return []

        current_hhmm = now.strftime("%H:%M")
        today_str = now.strftime("%Y-%m-%d")
        fired = []

        for key, (keyword, tr_start, tr_end, label, msg) in _BUILTIN_WEATHER_TRIGGERS.items():
            # 时间窗口检查
            if not (tr_start <= current_hhmm <= tr_end):
                continue

            # 天气关键词检查
            if keyword not in weather_text:
                continue

            # 冷却检查
            last = self._last_fired.get(key, 0)
            if time.time() - last < _TRIGGER_COOLDOWN:
                continue

            # 每日去重
            try:
                daily_key = f"last_scene_trigger_{key}"
                state = self._read_trigger_dedup()
                if state.get(daily_key) == today_str:
                    continue
                state[daily_key] = today_str
                self._write_trigger_dedup(state)
            except Exception:
                pass

            self._last_fired[key] = time.time()
            fired.append({
                "key": key,
                "label": label,
                "scene": None,  # 天气提醒不绑定具体场景
                "message": msg,
                "fired": True,
            })
            log.info(f"[scene_triggers] 天气触发: {label} (weather={weather_text[:20]})")

        return fired

    def fire_trigger(self, trigger: dict):
        """执行触发器：通过通知系统推送消息（不做副作用操作）

        Args:
            trigger: check_time_triggers / check_weather_triggers 返回的 dict
        """
        message = trigger.get("message", "")
        scene_key = trigger.get("scene")
        label = trigger.get("label", "")

        if not message:
            return

        # 如果有绑定的场景，在消息中提示
        if scene_key:
            try:
                from magic_scenes_runtime import _load_protocols
                protocols = _load_protocols()
                proto = protocols.get(scene_key, {})
                scene_name = proto.get("name", scene_key)
                message = f"[{scene_name}] {message}"
            except Exception:
                pass

        # 推送通知（飞书/MQTT）
        try:
            from app.notifications import add_notification
            add_notification(message, "scene_trigger")
        except Exception as e:
            log.warning(f"[scene_triggers] 推送通知失败: {e}")

        # 播报语音（ESP32 在线时）
        try:
            from app.notifications import play_reminder_audio
            play_reminder_audio(message, _feishu_done=True)
        except Exception as e:
            log.warning(f"[scene_triggers] 语音播报失败: {e}")

        log.info(f"[scene_triggers] 已触发: {label}")

    # ===== 持久化去重（独立文件，与 suggestions_state 隔离） =====

    _TRIGGER_DEDUP_FILE = None
    _TRIGGER_DEDUP_LOCK = None

    def _get_dedup_file(self) -> str:
        if self._TRIGGER_DEDUP_FILE is None:
            data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR",
                                       os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self._TRIGGER_DEDUP_FILE = os.path.join(data_dir, "scene_trigger_dedup.json")
        return self._TRIGGER_DEDUP_FILE

    def _read_trigger_dedup(self) -> dict:
        try:
            with open(self._get_dedup_file(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_trigger_dedup(self, state: dict):
        try:
            import tempfile
            directory = os.path.dirname(self._get_dedup_file()) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".scene_trigger_", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._get_dedup_file())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            log.warning(f"[scene_triggers] 写入去重文件失败: {e}")

    def get_current_weather_text(self) -> str:
        """获取当天天气描述文本（用于天气触发器判断）"""
        try:
            from app.schedulers import _get_weather
            casts = _get_weather()
            if casts:
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                forecast = _forecast_for_date(casts, today)
                if forecast:
                    parts = []
                    for w in (forecast.get("dayweather", ""), forecast.get("nightweather", "")):
                        w = str(w).strip()
                        if w and w not in parts:
                            parts.append(w)
                    temp = forecast.get("daytemp", "")
                    if temp:
                        parts.append(f"{temp}度")
                    return " ".join(parts)
        except Exception as e:
            log.debug(f"[scene_triggers] 获取天气失败: {e}")
        return ""


def _forecast_for_date(casts, target_date):
    """从 casts 列表中按日期匹配（与 schedulers.py 中的逻辑一致）"""
    for cast in casts:
        if str(cast.get("date", "")).strip() == target_date:
            return cast
    if casts and all(not str(cast.get("date", "")).strip() for cast in casts):
        return casts[0]
    return {}


# ===== 供 proactive_suggestions 调用的接口 =====

_trigger_engine: Optional[SceneTriggerEngine] = None


def get_trigger_engine() -> SceneTriggerEngine:
    global _trigger_engine
    if _trigger_engine is None:
        _trigger_engine = SceneTriggerEngine()
    return _trigger_engine


def check_and_fire(now: Optional[datetime.datetime] = None):
    """检查所有触发器并执行，供 proactive_suggestions 每次循环调用

    Args:
        now: 当前时间，默认使用 datetime.datetime.now()
    """
    engine = get_trigger_engine()
    now = now or datetime.datetime.now()

    # 1. 时间触发器
    time_triggers = engine.check_time_triggers(now)
    for trigger in time_triggers:
        engine.fire_trigger(trigger)

    # 2. 天气触发器（约每小时检查一次即可，proactive_suggestions 循环间隔 60s）
    # 通过 weather_check 标记避免每 60 秒都查天气
    try:
        from app.reminders import _save_suggest_state, SUGGESTIONS_STATE
        last_weather_check = SUGGESTIONS_STATE.get("last_scene_weather_check", 0)
        if time.time() - float(last_weather_check) > 3600:
            weather_text = engine.get_current_weather_text()
            if weather_text:
                weather_triggers = engine.check_weather_triggers(now, weather_text)
                for trigger in weather_triggers:
                    engine.fire_trigger(trigger)
            _save_suggest_state({"last_scene_weather_check": str(time.time())})
    except Exception as e:
        log.debug(f"[scene_triggers] 天气触发器检查跳过: {e}")
