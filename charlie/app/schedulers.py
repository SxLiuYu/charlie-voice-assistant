"""Background schedulers: reminder, proactive suggestions, evolution, decision engine, wake listener.

Extracted from voice_server.py. These run as daemon threads started from lifespan().
"""
import os, sys, json, time, datetime, logging, threading, hashlib, tempfile
from contextlib import contextmanager

import requests

from app.config import http_port
from app.reminders import (
    REMINDERS_FILE, _load_reminders, acquire_scheduler_lock, claim_due_reminders,
    complete_reminder_delivery, release_failed_reminder,
    SUGGESTIONS_STATE_FILE, PROACTIVE_LOCK_FILE, acquire_proactive_lock,
    DECISION_LOCK_FILE, acquire_decision_lock,
)
from app.notifications import add_notification, play_reminder_audio

log = logging.getLogger("magic")

try:
    import fcntl
except ImportError:
    import fcntl_compat as fcntl

HISTORY_FILE = os.path.join(os.path.dirname(REMINDERS_FILE), "conversation_history.json")

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
    except Exception:
        pass
    return {}

def _write_locked_suggest_state(state):
    directory = os.path.dirname(SUGGEST_STATE_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".suggestions_state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SUGGEST_STATE_FILE)
    except Exception:
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
    except Exception:
        pass

def _save_suggest_state():
    try:
        _update_suggest_state({})
    except Exception:
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

def _claim_suggest_state(key, value):
    with _locked_suggest_state(shared=False):
        state = dict(_SUGGESTIONS_DEFAULT_STATE)
        state.update(_read_locked_suggest_state())
        if state.get(key) == value:
            _refresh_suggestions_state(state)
            return False
        state[key] = value
        _write_locked_suggest_state(state)
        _refresh_suggestions_state(state)
        return True

_load_suggest_state()

def _get_weather():
    if AMAP_KEY and not AMAP_KEY.startswith("你的"):
        try:
            r = requests.get(f"https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": "110000", "key": AMAP_KEY, "extensions": "all"}, timeout=10)
            data = r.json()
            if data.get("forecasts"):
                casts = data["forecasts"][0].get("casts", [])
                if casts:
                    return casts
        except Exception as e:
            log.error(f"[suggest] AMAP天气失败: {e}")
    try:
        from app.weather import _open_meteo_get
        w = _open_meteo_get("北京")
        if w:
            return [{"date": datetime.datetime.now().strftime("%Y-%m-%d"),
                     "dayweather": w["weather_text"], "nightweather": w["weather_text"],
                     "daytemp": str(w["day_temp"]), "nighttemp": str(w["night_temp"])}]
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
    """睡眠场景：根据夜间天气处理空调（热天保留/凉天关闭）并关闭电视，返回与实际操作一致的播报。

    不再说“已关闭空调和电视”这种与实际不符的套话——空调是否关闭取决于夜间温度。
    """
    parts = ["检测到你已休息，晚安。"]
    try:
        sc = _magic_scenes()
        ac_result = sc._ac_sleep()
        sc._tv_control("power_off")
        parts.append(ac_result)
        parts.append("电视已关闭。")
        log.info(f"[suggest] 睡眠设备处理: ac={ac_result}")
    except Exception as e:
        log.warning(f"[suggest] 睡眠设备处理失败: {e}")
        parts.append("电视已关闭。")
    return "".join(parts)


def _preference_state_key(pkey, pval):
    fingerprint = hashlib.sha256(f"{pkey}\0{pval}".encode("utf-8")).hexdigest()[:16]
    return f"last_pref_{fingerprint}", fingerprint

# ===== Reminder scheduler =====
def _reminder_scheduler():
    global _scheduler_lock_handle
    log.info("[reminder] 提醒调度器已启动，正在竞争机器级调度锁")
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
                log.info(f"[reminder] 提醒到期(id={rid}): {text} (due={due_str})")
                try:
                    from app.audit_log import audit_log
                    audit_log("scheduler:reminder", input_data=f"id={rid} text={text} due={due_str}",
                              output_data="delivered", action="reminder_due",
                              session_id="system", source="system")
                except Exception:
                    pass
                add_notification(f"⏰ 提醒：{text}", "reminder")
                play_reminder_audio(text, reminder_id=rid)
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
                        except Exception:
                            pass
                    if screen_active:
                        update_user_state(screen_active=True)
                except Exception:
                    pass
                user_state = get_user_state()
            except Exception:
                user_state = {"state": "unknown", "confidence": 0.0}
            state = user_state.get("state", "unknown")
            state_changed = (state != _last_triggered_state)
            if state_changed:
                log.info(f"[suggest] 用户状态变化: {_last_triggered_state} -> {state}")
                _last_triggered_state = state
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
                        play_reminder_audio(msg)
            if state == "home_sleeping" and state_changed:
                if _claim_suggest_state("last_sleep_scene", today):
                    msg = _sleep_scene_message()
                    add_notification(msg, "sleep")
                    play_reminder_audio(msg)
            elif state == "away" and state_changed and _claim_suggest_state("last_away_scene", today):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                weather_info = f"今天{w}，{temp}度" if w and temp else ""
                msg = f"出门注意，{weather_info}。" if weather_info else "出门注意安全。"
                add_notification(msg, "away")
                play_reminder_audio(msg)
            elif state == "home_awake" and state_changed and _last_triggered_state == "away":
                if _claim_suggest_state("last_home_scene", today):
                    if not weather_loaded:
                        casts = _get_weather()
                    today_forecast = _forecast_for_date(casts, today)
                    w = today_forecast.get("dayweather", "") if today_forecast else ""
                    temp = today_forecast.get("daytemp", "") if today_forecast else ""
                    parts = ["欢迎回家！"]
                    if w and temp:
                        parts.append(f"今天{w}，{temp}度。")
                    pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                    if pending:
                        parts.append(f"今天还有{len(pending)}项待办。")
                    msg = "".join(parts)
                    add_notification(msg, "home")
                    play_reminder_audio(msg)
            if 8 <= hour < 10 and _claim_suggest_state("last_time_suggest", today + "_morning"):
                if not weather_loaded:
                    casts = _get_weather()
                today_forecast = _forecast_for_date(casts, today)
                w = today_forecast.get("dayweather", "") if today_forecast else ""
                temp = today_forecast.get("daytemp", "") if today_forecast else ""
                parts = [f"早上好主人！{'今天' + w + '，最高' + temp + '度，' if w and temp else ''}新的一天加油！"]
                pending = [r for r in _load_reminders() if not r.get("done") and r.get("due", "").startswith(today)]
                if pending:
                    todo = "、".join(r["text"] for r in pending)
                    parts.append(f"今天还有{len(pending)}项待办：{todo}。")
                else:
                    parts.append("今天没有待办事项，轻松一天！")
                msg = "".join(parts)
                add_notification(msg, "morning")
                play_reminder_audio(msg)
            import psutil as _ps
            cpu = _ps.cpu_percent(interval=None)
            mem = _ps.virtual_memory().percent
            health_key = today + f"_health_{hour}"
            if (cpu > 90 or mem > 95) and _claim_suggest_state("last_health_alert", health_key):
                msg = f"系统资源紧张：CPU使用率{cpu:.0f}%，内存{mem:.0f}%，建议关闭一些不必要的程序。"
                add_notification(msg, "health")
                play_reminder_audio(msg)
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
                        if 22 <= hour < 24:
                            bedtime_state = _suggest_state_snapshot().get("last_bedtime_suggest", "")
                            if not bedtime_state.startswith(today):
                                suggestion = f"你设定了{pkey}为{pval}，该准备休息了。"
                    if suggestion and _claim_suggest_state(state_key, pref_suggest_key):
                        add_notification(suggestion, "preference")
                        play_reminder_audio(suggestion)
                        break
            except Exception as e:
                log.debug(f"[suggest] 偏好建议检查异常: {e}")
        except Exception as e:
            log.error(f"[suggest] 主动建议异常: {e}")
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
                    for rule in decisions:
                        if rule.get("confirm"):
                            msg = rule["action"].get("text", "")
                            if msg:
                                add_notification(msg, "decision")
                                play_reminder_audio(msg)
                                _dec.set_pending_confirmation(rule["id"], msg)
                        else:
                            try:
                                _dec.mark_triggered(rule["id"])
                                if rule["action"]["type"] == "protocol":
                                    _scene_mod = load_magic_module("magic_scenes", "magic-scenes.py")
                                    if _scene_mod:
                                        result = _dec.execute_decision(rule, _scene_mod.execute_protocol)
                                        if result:
                                            add_notification(result[:200], "decision")
                                            play_reminder_audio(result[:200])
                                elif rule["action"]["type"] == "tts":
                                    msg = rule["action"].get("text", "")
                                    if msg:
                                        add_notification(msg, "decision")
                                        play_reminder_audio(msg)
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
