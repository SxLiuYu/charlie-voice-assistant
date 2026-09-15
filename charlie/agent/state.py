"""用户状态机: 情境触发替代时间触发"""
import os
import time
import threading
import datetime
import logging

log = logging.getLogger("magic")

_user_state = {
    "state": "unknown",
    "confidence": 0.0,
    "last_update": 0.0,
    "last_location": None,
    "last_voice_activity": 0.0,
    "last_screen_active": 0.0,
    "home_location": None,
}
_user_state_lock = threading.Lock()

def update_user_state(**kwargs) -> dict:
    with _user_state_lock:
        now = time.time()
        if "location" in kwargs and kwargs["location"]:
            _user_state["last_location"] = kwargs["location"]
        if "voice_activity" in kwargs:
            _user_state["last_voice_activity"] = now
        if "screen_active" in kwargs:
            _user_state["last_screen_active"] = now
        if "home_location" in kwargs and kwargs["home_location"]:
            _user_state["home_location"] = kwargs["home_location"]
        _infer_user_state()
        _user_state["last_update"] = now
        return dict(_user_state)

def get_user_state() -> dict:
    with _user_state_lock:
        return dict(_user_state)

def _infer_user_state():
    now = time.time()
    hour = datetime.datetime.now().hour
    voice_idle = now - _user_state["last_voice_activity"]
    screen_idle = now - _user_state["last_screen_active"]
    has_location = _user_state["last_location"] is not None

    at_home = False
    presence_score = 0.0

    if has_location and _user_state["home_location"]:
        lat1, lng1 = _user_state["last_location"]
        lat2, lng2 = _user_state["home_location"]
        if abs(lat1 - lat2) < 0.01 and abs(lng1 - lng2) < 0.01:
            at_home = True

    try:
        import presence
        presence_result = presence.detect_devices()
        if presence_result["at_home"] is not None:
            presence_score = presence.get_presence_confidence()
            if presence_score > 0.5:
                at_home = True
    except Exception as e:
        log.debug(f"[agent.state] error at line 63: {e}")
        pass

    if not has_location and presence_score == 0.0:
        at_home = True

    if not at_home:
        _user_state["state"] = "away"
        _user_state["confidence"] = 0.6
        return

    if 22 <= hour or hour < 6:
        if voice_idle > 3600:
            _user_state["state"] = "home_sleeping"
            _user_state["confidence"] = 0.7
        elif voice_idle > 1800:
            _user_state["state"] = "home_resting"
            _user_state["confidence"] = 0.6
        else:
            _user_state["state"] = "home_awake"
            _user_state["confidence"] = 0.5
    elif 6 <= hour < 22:
        if screen_idle < 300 and voice_idle < 600:
            _user_state["state"] = "working"
            _user_state["confidence"] = 0.7
        elif voice_idle > 1800:
            _user_state["state"] = "home_resting"
            _user_state["confidence"] = 0.5
        else:
            _user_state["state"] = "home_awake"
            _user_state["confidence"] = 0.6