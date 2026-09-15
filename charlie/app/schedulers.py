"""Background schedulers: reminder, proactive suggestions, evolution, decision engine, wake listener.

Extracted from voice_server.py. These run as daemon threads started from lifespan().
"""
import os, sys, json, time, datetime, logging, threading, hashlib, tempfile
from contextlib import contextmanager

import requests

from app.config import http_port
from app.reminders import (
    REMINDERS_FILE, _load_reminders, acquire_scheduler_lock, claim_due_reminders,
    complete_reminder_delivery, release_failed_reminder, set_reminder_retry_after,
    SUGGESTIONS_STATE_FILE, PROACTIVE_LOCK_FILE, acquire_proactive_lock,
    DECISION_LOCK_FILE, acquire_decision_lock,
)
from app import load_magic_module
from app.notifications import add_notification, play_reminder_audio, FEISHU_PUSH_OPEN_ID

log = logging.getLogger("magic")


def _is_quiet_hours(now=None) -> bool:
    """检查当前是否在静音时段；env 变量可配置，设为相同值则禁用。"""
    now = now or datetime.datetime.now()
    start_str = os.getenv("REMINDER_QUIET_START", "22")
    end_str = os.getenv("REMINDER_QUIET_END", "7")
    start_hour = int(start_str)
    end_hour = int(end_str)
    if start_hour == end_hour:
        return False  # 禁用静音时段
    hour = now.hour
    if start_hour > end_hour:
        # 跨天：22:00 - 07:00
        return hour >= start_hour or hour < end_hour
    else:
        # 同天时段（不常见但支持）
        return start_hour <= hour < end_hour


def _get_quiet_hours_retry(now=None):
    """如果在静音时段，返回下次可播报时间（静音时段结束后的 07:05）；否则返回 None。"""
    now = now or datetime.datetime.now()
    if not _is_quiet_hours(now):
        return None
    end_str = os.getenv("REMINDER_QUIET_END", "7")
    end_hour = int(end_str)
    retry = now.replace(hour=end_hour, minute=5, second=0, microsecond=0)
    hour = now.hour
    start_str = os.getenv("REMINDER_QUIET_START", "22")
    start_hour = int(start_str)
    if start_hour > end_hour and hour >= start_hour:
        # 跨天：22:00-23:59，补播是明天
        retry += datetime.timedelta(days=1)
    return retry


def _play_async(text: str, reminder_id=None, _feishu_done: bool = False):
    """守护线程派发 play_reminder_audio，避免阻塞调度循环"""
    def _task(_text=text, _rid=reminder_id, _feishu=_feishu_done):
        try:
            if _feishu:
                play_reminder_audio(_text, reminder_id=_rid, _feishu_done=True)
            else:
                play_reminder_audio(_text, reminder_id=_rid)
        except Exception:
            log.exception("[scheduler] 异步播报异常")
    threading.Thread(target=_task, daemon=True).start()

# ── ESP32 在线快速检测（避免往离线设备推音频）─────────────────────────────
_esp32_seen_at = 0.0  # 最近一次收到 ESP32 消息的时间戳
_AUTO_TRIGGER_COOLDOWN = 3600  # 同一 auto_trigger 至少隔 1 小时才能再次执行
_auto_trigger_last_exec: dict[str, float] = {}  # key → 上次执行时间戳
_DECISION_COOLDOWN = 3600  # 同一 confirm 类型决策至少隔 1 小时才能再次询问
_decision_last_ask: dict[str, float] = {}  # key → 上次询问时间戳
_server_start_time = time.time()  # 服务启动时间（用于 CPU 健康告警启动宽限期）


def _is_esp32_online() -> bool:
    """ESP32 在线判断：最近 5 分钟内有过通信。"""
    return (time.time() - _esp32_seen_at) < 300


def _touch_esp32():
    """供 WS 连接等处调用，标记 ESP32 最近活跃。"""
    global _esp32_seen_at
    _esp32_seen_at = time.time()

try:
    import fcntl
except ImportError:
    import fcntl_compat as fcntl

HISTORY_FILE = os.path.join(os.path.dirname(REMINDERS_FILE), "conversation_history.json")

# 城市名 → AMap adcode 映射（天气偏好按此取 city_code）
_CITY_TO_AMAP_CODE: dict[str, str] = {
    "北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
    "杭州": "330100", "成都": "510100", "武汉": "420100", "南京": "320100",
    "西安": "610100", "重庆": "500000", "苏州": "320500", "天津": "120100",
}

# ===== Suggest state management =====
AMAP_KEY = os.getenv("AMAP_KEY", "")
SUGGEST_STATE_FILE = SUGGESTIONS_STATE_FILE
SUGGEST_STATE_LOCK_FILE = SUGGESTIONS_STATE_FILE + ".lock"
_SUGGESTIONS_DEFAULT_STATE = {
    "last_weather_check": 0, "last_rain_suggest": "", "last_time_suggest": "", "last_health_alert": "",
}
SUGGESTIONS_STATE = dict(_SUGGESTIONS_DEFAULT_STATE)
_suggest_state_lock = threading.Lock()
_proactive_lock_handle = None
_proactive_thread = None
_scheduler_lock_handle = None

@contextmanager
def _locked_suggest_state(shared=False):
    os.makedirs(os.path.dirname(SUGGEST_STATE_FILE), exist_ok=True)
    with _suggest_state_lock:
        with open(SUGGEST_STATE_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

def _read_locked_suggest_state():
    try:
        with open(SUGGEST_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        log.debug(f"[app.schedulers] error at line 77: {e}")
        pass
    return {}

def _write_locked_suggest_state(state):
    directory = os.path.dirname(SUGGEST_STATE_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".suggestions_state.", suffix=".tmp", dir=directory)
    f_wrapped = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f_wrapped = True
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SUGGEST_STATE_FILE)
    except Exception:
        if not f_wrapped:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def _refresh_suggestions_state(data):
    SUGGESTIONS_STATE.clear()
    SUGGESTIONS_STATE.update(_SUGGESTIONS_DEFAULT_STATE)
    SUGGESTIONS_STATE.update(data)

def _load_suggest_state():
    try:
        with _locked_suggest_state(shared=True):
            data = _read_locked_suggest_state()
        _refresh_suggestions_state(data)
    except Exception as e:
        log.debug(f"[app.schedulers] error at line 107: {e}")
        pass

def _save_suggest_state():
    try:
        _update_suggest_state({})
    except Exception as e:
        log.debug(f"[app.schedulers] error at line 113: {e}")
        pass

def _suggest_state_snapshot():
    with _locked_suggest_state(shared=True):
        data = _read_locked_suggest_state()
    state = dict(_SUGGESTIONS_DEFAULT_STATE)
    state.update(data)
    _refresh_suggestions_state(state)
    return dict(state)

def _update_suggest_state(updates):
    with _locked_suggest_state(shared=False):
        state = dict(_SUGGESTIONS_DEFAULT_STATE)
        state.update(_read_locked_suggest_state())
        state.update(updates)
        _write_locked_suggest_state(state)
        _refresh_suggestions_state(state)
        return dict(state)

def _prune_stale_pref_keys(state):
    """Remove orphaned last_pref_* keys that no longer match any current preference.

    Called from _claim_suggest_state to prevent unbounded accumulation of
    preference-fingerprint keys when preference values change over time.
    """
    pref_keys = set()
    try:
        from voice_agent import list_preferences
        for pkey, pval in list_preferences().items():
            sk, _ = _preference_state_key(str(pkey), str(pval))
            pref_keys.add(sk)
    except Exception as e:
        log.debug(f"[suggest] prune_pref_keys skipped: {e}")
        return state
    stale = [k for k in state if k.startswith("last_pref_") and k not in pref_keys]
    if stale:
        for k in stale:
            state.pop(k, None)
        log.debug(f"[suggest] pruned {len(stale)} stale last_pref_* keys")
    return state

def _claim_suggest_state(key, value):
    with _locked_suggest_state(shared=False):
        state = dict(_SUGGESTIONS_DEFAULT_STATE)
        state.update(_read_locked_suggest_state())
        if state.get(key) == value:
            _refresh_suggestions_state(state)
            return False
        state[key] = value
        if key.startswith("last_pref_"):
            state = _prune_stale_pref_keys(state)
        _write_locked_suggest_state(state)
        _refresh_suggestions_state(state)
        return True

_load_suggest_state()

def _get_weather():
    city = "北京"
    city_code = "110000"
    try:
        from voice_agent import get_preference
        pref_city = get_preference("weather_city") or get_preference("city")
        if pref_city:
            city = pref_city
            city_code = _CITY_TO_AMAP_CODE.get(city, "110000")
    except Exception as e:
        log.debug(f"[suggest] 读取天气城市偏好失败: {e}")
    try:
        from app.weather import get_weather_casts
        casts = get_weather_casts(city)
        if casts:
            return casts
    except Exception as e:
        log.error(f"[suggest] get_weather_casts 失败: {e}")
    if AMAP_KEY and not AMAP_KEY.startswith("你的"):
        try:
            r = requests.get(f"https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": city_code, "key": AMAP_KEY, "extensions": "all"}, timeout=10)
            data = r.json()
            if data.get("forecasts"):
                casts = data["forecasts"][0].get("casts", [])
                if casts:
                    return casts
        except Exception as e:
            log.error(f"[suggest] AMAP天气失败: {e}")
    try:
        from app.weather import _open_meteo_get_casts
        casts = _open_meteo_get_casts(city)
        if casts:
            return casts
    except Exception as e:
        log.error(f"[suggest] Open-Meteo天气失败: {e}")
    return []

def _forecast_for_date(casts, target_date):
    for cast in casts:
        if str(cast.get("date", "")).strip() == target_date:
            return cast
    if casts and all(not str(cast.get("date", "")).strip() for cast in casts):
        return casts[0]
    return {}


_magic_scenes_mod = None

def _magic_scenes():
    """延迟加载 magic-scenes 模块以复用 _ac_sleep / _tv_control。

    magic-scenes.py 文件名含连字符无法直接 import，用 importlib 按路径加载并缓存。
    """
    global _magic_scenes_mod
    if _magic_scenes_mod is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "magic-scenes.py")
        spec = importlib.util.spec_from_file_location("magic_scenes_runtime", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _magic_scenes_mod = mod
    return _magic_scenes_mod


def _sleep_scene_message() -> str:
    parts = ["检测到你已休息，晚安。"]
    try:
        sc = _magic_scenes()
        ac_result = sc._ac_sleep()
        tv_result = sc._tv_control("power_off")
        if ac_result:
            parts.append(ac_result if ac_result.endswith("。") else ac_result + "。")
        if tv_result:
            parts.append(tv_result if tv_result.endswith("。") else tv_result + "。")
        log.info(f"[suggest] sleep devices: ac={ac_result} tv={tv_result}")
    except Exception as e:
        log.warning(f"[suggest] sleep device handling failed: {e}")
    return "".join(parts)


def _preference_state_key(pkey, pval):
    fingerprint = hashlib.sha256(f"{pkey}\0{pval}".encode("utf-8")).hexdigest()[:16]
    return f"last_pref_{fingerprint}", fingerprint

# ===== Reminder scheduler =====
def _reminder_scheduler():
    global _scheduler_lock_handle
    log.info("[reminder] 提醒调度器已启动，正在竞争机器级调度锁")
    # 重启恢复：重置卡在中间状态的提醒
    try:
        from app.reminders import restore_after_restart
        restored = restore_after_restart()
        if restored:
            log.info(f"[reminder] 重启恢复 {restored} 条提醒完成")
    except Exception as e:
        log.warning(f"[reminder] 重启恢复失败: {e}")
    cleanup_counter = 0
    while True:
        try:
            if _scheduler_lock_handle is None:
                _scheduler_lock_handle = acquire_scheduler_lock()
                if _scheduler_lock_handle is None:
                    time.sleep(30)
                    continue
                log.info("[reminder] 已获取机器级调度锁，开始检查到期提醒")
            cleanup_counter += 1
            if cleanup_counter >= 20:
                cleanup_counter = 0
                from utils import cleanup_temp_files, truncate_history_file
                from voice_agent import runtime_temp_audio_path
                cleanup_temp_files(extra_dirs=[runtime_temp_audio_path()])
                truncate_history_file(HISTORY_FILE, 100)
            now = datetime.datetime.now()
            due_reminders = claim_due_reminders(now)
            for reminder in due_reminders:
                rid = reminder.get("id", 0)
                text = reminder.get("text", "提醒")
                due_str = reminder.get("due", "")
                repeat = reminder.get("repeat", "")
                attempt = int(reminder.get("attempt_count", 0) or 0)
                log.info(f"[reminder] 提醒到期(id={rid}): {text} (due={due_str}, attempt={attempt})")
                try:
                    from app.audit_log import audit_log
                    audit_log("scheduler:reminder", input_data=f"id={rid} text={text} due={due_str}",
                              output_data="delivered", action="reminder_due",
                              session_id="system", source="system")
                except Exception as e:
                    log.debug(f"[app.schedulers] error at line 249: {e}")
                    pass
                # 首次送达才推飞书；ESP32 离线导致的 5 分钟重试不重复推送
                if attempt <= 1:
                    add_notification(f"⏰ 提醒：{text}", "reminder")

                # 静音时段：跳过音频播报（ESP32 + afplay），仅保留通知通道；标记补播时间
                if _is_quiet_hours(now):
                    retry_time = _get_quiet_hours_retry(now)
                    if retry_time:
                        set_reminder_retry_after(rid, retry_time)
                        log.info(f"[reminder] 静音时段，提醒{rid}补播时间={retry_time.isoformat()}")
                    # add_notification 已推飞书，无需重复推送
                    continue

                _play_async(text, reminder_id=rid, _feishu_done=True)
        except Exception as e:
            log.error(f"[reminder] 调度器异常: {e}")
        time.sleep(30)

def start_scheduler():
    t = threading.Thread(target=_reminder_scheduler, daemon=True)
    t.start()

# ===== Proactive suggestions =====
def _proactive_suggestions():
    global _proactive_lock_handle
    log.info("[suggest] 主动建议系统已启动，正在竞争机器级运行锁")
    _last_triggered_state = None
    while True:
        try:
            if _proactive_lock_handle is None:
                _proactive_lock_handle = acquire_proactive_lock()
                if _proactive_lock_handle is None:
                    time.sleep(60)
                    continue
                log.info("[suggest] 已获取机器级运行锁，开始状态感知建议")
            now = datetime.datetime.now()
            hour = now.hour
            today = now.strftime("%Y-%m-%d")

            # 场景定时/天气触发器（每次循环检查）
            try:
                from agent.scene_triggers import check_and_fire
                check_and_fire(now)
            except Exception as e:
                log.debug(f"[suggest] scene_triggers 跳过: {e}")

            suggest_state = _suggest_state_snapshot()
            try:
                from voice_agent import get_user_state, update_user_state
                import psutil
                try:
                    screen_active = False
                    for proc in psutil.process_iter(['name', 'cpu_percent']):
                        try:
                            if proc.info['name'] in ('WindowServer', 'Dock', 'Finder'):
                                if proc.info['cpu_percent'] > 0.5:
                                    screen_active = True
                                    break
                        except Exception as e:
                            log.debug(f"[app.schedulers] error at line 294: {e}")
                            pass
                    if screen_active:
                        update_user_state(screen_active=True)
                except Exception as e:
                    log.debug(f"[app.schedulers] error at line 298: {e}")
                    pass
                user_state = get_user_state()
            except Exception:
                user_state = {"state": "unknown", "confidence": 0.0}
            state = user_state.get("state", "unknown")
            state_changed = (state != _last_triggered_state)
            if state_changed:
                log.info(f"[suggest] 用户状态变化: {_last_triggered_state} -> {state}")
                _last_triggered_state = state
                # 接线 auto_trigger：状态变化时检查匹配的场景
                try:
                    _scene_mod = load_magic_module("magic_scenes", "magic-scenes.py")
                    if _scene_mod and hasattr(_scene_mod, "check_auto_triggers"):
                        auto_keys = _scene_mod.check_auto_triggers(state)
                        for pkey in auto_keys:
                            # 同一场景 1 小时内不重复执行（防止夜间状态持续满足时狂发消息）
                            last_exec = _auto_trigger_last_exec.get(pkey, 0)
                            if time.time() - last_exec < _AUTO_TRIGGER_COOLDOWN:
                                log.debug(f"[suggest] auto_trigger 冷却中({pkey})，跳过")
                                continue
                            # 每日一次持久化去重：内存冷却在重启后失效，
                            # 曾导致 22:27 与 22:37（重启后）重复推送睡眠提醒
                            if not _claim_suggest_state(f"last_auto_trigger_{pkey}", today + "_" + state):
                                log.debug(f"[suggest] auto_trigger 今日已提示({pkey})，跳过")
                                continue
                            _auto_trigger_last_exec[pkey] = time.time()
                            _proto = _scene_mod._load_protocols().get(pkey, {})
                            _proto_name = _proto.get("name", pkey)
                            if _proto.get("auto_trigger", {}).get("require_confirm"):
                                # require_confirm=True 的场景不自动执行设备操作（可能误判），
                                # 仅播报提醒，让用户主动触发
                                msg = f'检测到你{state}，如果准备休息可以说"晚安"来启动{pkey}模式。'
                                if _is_esp32_online():
                                    _play_async(msg, _feishu_done=True)
                                else:
                                    add_notification(msg, "auto_trigger")
                                    _play_async(msg, _feishu_done=True)
                                log.info(f"[suggest] require_confirm=true，仅提醒不直执: {pkey}")
                                continue
                            else:
                                try:
                                    result = _scene_mod.execute_protocol(pkey)
                                    if result:
                                        add_notification(result[:200], "auto_trigger")
                                        _play_async(result[:200], _feishu_done=True)
                                    log.info(f"[suggest] auto_trigger 执行: {pkey} (state={state})")
                                except Exception as e:
                                    log.warning(f"[suggest] auto_trigger 执行失败({pkey}): {e}")
                except Exception as e:
                    log.debug(f"[suggest] auto_trigger 检查跳过: {e}")
            casts = None
            weather_loaded = False
            now_ts = time.time()
            if now_ts - float(suggest_state.get("last_weather_check", 0) or 0) > 3600:
                should_check_weather = _claim_suggest_state("last_weather_check", now_ts)
            else:
                should_check_weather = False
            if should_check_weather:
                casts = _get_weather()
                weather_loaded = bool(casts)
                today_forecast = _forecast_for_date(casts, today)
                if today_forecast:
                    c = today_forecast
                    weather_parts = []
                    for weather_name in (c.get("dayweather", ""), c.get("nightweather", "")):
                        weather_name = str(weather_name).strip()
                        if weather_name and weather_name not in weather_parts:
                            weather_parts.append(weather_name)
                    weather = " ".join(weather_parts)
                    if any("雨" in weather_name or "雪" in weather_name for weather_name in weather_parts) and _claim_suggest_state("last_rain_suggest", today):
                        msg = f"主人，今天天气预报有{weather}，出门记得带伞哦。"
                        add_notification(msg, "weather")
                        log.info(f"[suggest] 主动天气建议: {msg}")
                        _play_async(msg, _feishu_done=True)
            if state == "home_sleeping" and state_changed:
                if _claim_suggest_state("last_sleep_scene", today):
                    msg = _sleep_scene_message()
                    add_notification(msg, "sleep")
                    _play_async(msg, _feishu_done=True)
            elif state == "away" and state_changed and _claim_suggest_state("last_away_scene", today):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                weather_info = f"今天{w}，{temp}度" if w and temp else ""
                try:
                    _jarvis = load_magic_module("magic_jarvis", "magic-jarvis.py")
                    if _jarvis:
                        _away_greet = _jarvis.proactive_greeting()
                        if _away_greet:
                            msg = f"{_away_greet}{weather_info}。" if weather_info else _away_greet
                        else:
                            msg = f"出门注意，{weather_info}。" if weather_info else "出门注意安全。"
                    else:
                        msg = f"出门注意，{weather_info}。" if weather_info else "出门注意安全。"
                except Exception as e:
                    log.debug(f"[suggest] proactive_greeting(away)跳过: {e}")
                    msg = f"出门注意，{weather_info}。" if weather_info else "出门注意安全。"
                add_notification(msg, "away")
                _play_async(msg, _feishu_done=True)
            elif state == "home_awake" and state_changed and _last_triggered_state == "away":
                if _claim_suggest_state("last_home_scene", today):
                    if not weather_loaded:
                        casts = _get_weather()
                    today_forecast = _forecast_for_date(casts, today)
                    w = today_forecast.get("dayweather", "") if today_forecast else ""
                    temp = today_forecast.get("daytemp", "") if today_forecast else ""
                    try:
                        _jarvis = load_magic_module("magic_jarvis", "magic-jarvis.py")
                        if _jarvis:
                            _home_greet = _jarvis.proactive_greeting()
                        else:
                            _home_greet = ""
                    except Exception as e:
                        log.debug(f"[suggest] proactive_greeting(home)跳过: {e}")
                        _home_greet = ""
                    parts = [_home_greet] if _home_greet else ["欢迎回家！"]
                    if w and temp:
                        parts.append(f"今天{w}，{temp}度。")
                    pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                    if pending:
                        parts.append(f"今天还有{len(pending)}项待办。")
                    msg = "".join(parts)
                    add_notification(msg, "home")
                    _play_async(msg, _feishu_done=True)
            if hour == 7 and minute == 30 and _claim_suggest_state("last_time_suggest", today + "_morning"):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                _is_jarvis = False
                try:
                    from agent.roles import get_current_role
                    _is_jarvis = get_current_role() == "jarvis"
                except Exception as e:
                    log.debug(f"[app.schedulers] error at line 439: {e}")
                    pass
                try:
                    _jarvis = load_magic_module("magic_jarvis", "magic-jarvis.py")
                    # 所有角色都用 morning_briefing（JARVIS 能力是 Charlie 的默认能力）
                    if _jarvis:
                        msg = _jarvis.morning_briefing()
                    else:
                        _jarvis_proc = _jarvis if _jarvis else None
                        _morning_greet = ""
                        if _jarvis_proc:
                            try:
                                _morning_greet = _jarvis_proc.proactive_greeting()
                            except Exception:
                                _morning_greet = ""
                        weather_str = f"今天{w}，最高{temp}度，" if w and temp else ""
                        parts = [_morning_greet or f"早上好主人！{weather_str}新的一天加油！"]
                        pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                        if pending:
                            todo = "、".join(r["text"] for r in pending)
                            parts.append(f"今天还有{len(pending)}项待办：{todo}。")
                        else:
                            parts.append("今天没有待办事项，轻松一天！")
                        msg = "".join(parts)
                except Exception as e:
                    log.debug(f"[suggest] morning briefing跳过: {e}")
                    msg = "早上好主人！"
                add_notification(msg, "morning")
                _play_async(msg, _feishu_done=True)
                try:
                    if _jarvis:
                        _suggest = _jarvis.suggest_action("早晨问候后")
                        if _suggest:
                            _suggest_msg = f"💡 {_suggest}"
                            add_notification(_suggest_msg, "suggestion")
                            _play_async(_suggest_msg, _feishu_done=True)
                except Exception as e:
                    log.debug(f"[suggest] suggest_action(morning)跳过: {e}")
            import psutil as _ps
            cpu = _ps.cpu_percent(interval=None)
            mem = _ps.virtual_memory().percent
            health_key = today + f"_health_{hour}"
            if (cpu > 90 or mem > 95) and time.time() - _server_start_time >= 120 and _claim_suggest_state("last_health_alert", health_key):
                msg = f"系统资源紧张：CPU使用率{cpu:.0f}%，内存{mem:.0f}%，建议关闭一些不必要的程序。"
                add_notification(msg, "health")
                _play_async(msg)
            # 磁盘空间监控：使用率 > 90% 时每日一次推送飞书告警
            try:
                import shutil as _shutil
                _data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                _disk_usage = _shutil.disk_usage(_data_dir)
                _disk_pct = _disk_usage.used / _disk_usage.total * 100
                _disk_key = today + f"_disk_{_disk_pct:.0f}"
                if _disk_pct > 90 and _claim_suggest_state("last_disk_alert", _disk_key):
                    _disk_msg = f"磁盘空间不足：已使用{_disk_pct:.0f}%，建议清理临时文件。"
                    add_notification(_disk_msg, "health")
                    _play_async(_disk_msg)
            except Exception as e:
                log.debug(f"[suggest] 磁盘空间检查失败: {e}")
            try:
                from voice_agent import list_preferences
                prefs = list_preferences()
                for pkey, pval in prefs.items():
                    state_key, pref_fingerprint = _preference_state_key(str(pkey), str(pval))
                    pref_suggest_key = f"{today}_pref_{pref_fingerprint}"
                    suggestion = None
                    if "下班" in pkey or "下班" in pval:
                        if 17 <= hour < 19:
                            suggestion = f"主人，快到下班时间了({pval})，需要我帮你查查路况或叫个车吗？"
                    elif "食物" in pkey or "喜欢吃" in pkey:
                        if 11 <= hour < 13 or 17 <= hour < 19:
                            suggestion = f"到饭点了，我记得你喜欢{pval}，要不要帮你找附近的餐厅？"
                    elif "运动" in pkey or "锻炼" in pkey:
                        if 6 <= hour < 8 or 18 <= hour < 20:
                            suggestion = f"是你平时的运动时间，今天别忘了{pval}哦。"
                    elif "睡" in pkey or "休息" in pkey:
                        # 从偏好值动态计算提醒时间（sleep_time 前2小时开始）
                        try:
                            sleep_hour = int(pval.split(":")[0]) if pval else 22
                        except (ValueError, IndexError):
                            sleep_hour = 22
                        if sleep_hour - 2 <= hour < sleep_hour:
                            bedtime_state = _suggest_state_snapshot().get("last_bedtime_suggest", "")
                            if not bedtime_state.startswith(today):
                                suggestion = f"你设定了{pkey}为{pval}，该准备休息了。"
                    if suggestion and _claim_suggest_state(state_key, pref_suggest_key):
                        add_notification(suggestion, "preference")
                        _play_async(suggestion, _feishu_done=True)
                        break
            except Exception as e:
                log.debug(f"[suggest] 偏好建议检查异常: {e}")
        except Exception as e:
            log.error(f"[suggest] 主动建议异常: {e}")
        # Feishu WS 线程 watchdog：如果 Lark SDK 自动重连失败线程退出，自动重启
        try:
            from feishu_ws import is_running as _fw_is_running, is_ever_started as _fw_started, start as _fw_start
            if not _fw_is_running():
                # 进程刚启动 WS 尚未起（正常启动时序）不打 WARNING，
                # 只有"启动过又退出"才算异常，避免误导排障（日志审查曾把
                # 62 次正常进程重启的启动时序误判为 WS 反复掉线）
                if _fw_started():
                    log.warning("[suggest] 飞书 WS 线程已退出，尝试重启")
                _fw_start()
        except Exception as e:
            log.debug(f"[suggest] 飞书 WS watchdog 跳过: {e}")
        time.sleep(60)

def start_proactive():
    global _proactive_thread
    if _proactive_thread is not None and _proactive_thread.is_alive():
        log.debug("[proactive] 已在运行，跳过")
        return
    log.info("[proactive] 启动主动建议守护线程")
    _proactive_thread = threading.Thread(target=_proactive_suggestions, daemon=True)
    _proactive_thread.start()

# ===== Evolution =====
def start_evolution():
    def _evolve_loop():
        import time
        try:
            from voice_agent import _get_brain
            brain = _get_brain("magic-evolution")
            for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
                pass
            log.info("[evolution] 启动时自进化学习完成")
        except Exception as e:
            log.debug(f"[evolution] 启动时学习跳过: {e}")
        while True:
            time.sleep(1800)
            try:
                brain = _get_brain("magic-evolution")
                for rsp in brain.run([{"role": "user", "content": "learn_from_history()"}]):
                    pass
                log.info("[evolution] 定时自进化学习完成")
            except Exception as e:
                log.debug(f"[evolution] 定时学习跳过: {e}")
    threading.Thread(target=_evolve_loop, daemon=True).start()

# ===== Decision engine =====
def start_decision_engine():
    def _decision_loop():
        import time
        time.sleep(10)
        log.info("[decision] 自主决策引擎已启动，正在竞争机器级运行锁")
        _decision_lock_handle = None
        _cycle_count = 0
        while True:
            try:
                if _decision_lock_handle is None:
                    _decision_lock_handle = acquire_decision_lock()
                    if _decision_lock_handle is None:
                        time.sleep(60)
                        continue
                    log.info("[decision] 已获取机器级运行锁，开始评估决策")
                from voice_agent import get_user_state
                user_state = get_user_state()
                from app import load_magic_module
                _dec = load_magic_module("magic_decisions", "magic-decisions.py")
                if _dec:
                    decisions = _dec.evaluate(user_state)
                    pending = _dec.get_pending_confirmation()
                    _cycle_count += 1
                    if _cycle_count % 10 == 0:
                        pending_ids = [s.get("rule_id") for s in [pending] if isinstance(s, dict) and s.get("pending")]
                        log.info(
                            f"[decision] state: {len(getattr(_dec, '_rules', []))} rules, "
                            f"pending={pending or 'none'}, cycle={_cycle_count}"
                        )
                    for rule in decisions:
                        rid = rule["id"]
                        if rule.get("confirm"):
                            # 用户已在睡眠状态：不再询问确认（问了也听不到，避免整夜推送）
                            if user_state.get("state") == "home_sleeping":
                                log.debug(f"[decision] 用户睡眠中({rid})，跳过确认询问")
                                continue
                            # 冷却期内不重复询问（late_night_sleep 使用 8 小时冷却，避免整夜轰炸）
                            last_ask = _decision_last_ask.get(rid, 0)
                            cooldown = 28800 if rid == "late_night_sleep" else _DECISION_COOLDOWN
                            if time.time() - last_ask < cooldown:
                                log.debug(f"[decision] 冷却中({rid}, cooldown={cooldown}s)，跳过")
                                continue
                            # 已有待确认的询问不重复发（用户还没回复）
                            if pending and pending.get("rule_id") == rid:
                                log.debug(f"[decision] 已有待确认({rid})，跳过")
                                continue
                            # 每日询问上限（跨重启持久化）：用户未回应时不整夜轰炸
                            try:
                                if _dec.get_confirm_ask_count(rid) >= _dec._CONFIRM_MAX_ASKS_PER_DAY:
                                    log.debug(f"[decision] 今日询问已达上限({rid})，跳过")
                                    continue
                            except AttributeError:
                                log.warning(f"[decision] {rid}: confirm_ask 接口缺失，每日上限未生效")
                            msg = rule["action"].get("text", "")
                            if msg:
                                _decision_last_ask[rid] = time.time()
                                try:
                                    _dec.record_confirm_ask(rid)
                                except AttributeError:
                                    log.warning(f"[decision] {rid}: record_confirm_ask 接口缺失，每日上限未生效")
                                add_notification(msg, "decision")
                                if _is_esp32_online():
                                    _play_async(msg, _feishu_done=True)
                                else:
                                    log.info(f"[decision] ESP32离线，确认询问仅推送飞书({rid})")
                                _dec.set_pending_confirmation(rid, msg, user_id=FEISHU_PUSH_OPEN_ID)
                        else:
                            try:
                                # 晨报去重：决策引擎与主动调度器共享同一 claim key，
                                # 先到先得，避免同一早晨播报两次早安简报
                                if rid == "morning_wakeup":
                                    _today = datetime.datetime.now().strftime("%Y-%m-%d")
                                    if not _claim_suggest_state("last_time_suggest", _today + "_morning"):
                                        log.debug("[decision] 晨报已被主动调度器播报，跳过")
                                        continue
                                _dec.mark_triggered(rule["id"])
                                if rule["action"]["type"] == "protocol":
                                    _scene_mod = load_magic_module("magic_scenes", "magic-scenes.py")
                                    if _scene_mod:
                                        result = _dec.execute_decision(rule, _scene_mod.execute_protocol)
                                        if result:
                                            add_notification(result[:200], "decision")
                                            _play_async(result[:200], _feishu_done=True)
                                elif rule["action"]["type"] == "tts":
                                    msg = rule["action"].get("text", "")
                                    if msg:
                                        add_notification(msg, "decision")
                                        _play_async(msg, _feishu_done=True)
                            except Exception as e:
                                log.warning(f"[decision] 执行失败: {e}")
            except Exception as e:
                log.debug(f"[decision] 评估异常: {e}")
            time.sleep(120)
    threading.Thread(target=_decision_loop, daemon=True).start()

# ===== Wake listener =====
def _process_wake_command(wav_bytes):
    from voice_agent import voice_loop
    try:
        text, reply, audio_out = voice_loop(wav_bytes, "wav")
        if text and reply:
            log.info(f"[wake] cmd: asr={text[:30]} reply={reply[:30]}")
            add_notification(f"{text[:50]} -> {reply[:50]}", "wake")
            if audio_out:
                import local_wake
                local_wake._play_audio(audio_out)
    except Exception as e:
        log.warning(f"[wake] cmd failed: {e}")

def start_wake_listener():
    def _wake_loop():
        import time
        time.sleep(20)
        try:
            import local_wake
            local_wake.start_wake_detector(_process_wake_command)
            log.info("[wake] local wake listener started")
        except Exception as e:
            log.warning(f"[wake] start failed: {e}")
    threading.Thread(target=_wake_loop, daemon=True).start()
