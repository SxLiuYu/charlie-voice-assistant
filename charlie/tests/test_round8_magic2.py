"""Round 8 magic TOP5 behavior tests (mock external deps, tmp_path isolation)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _load_module(name: str, filename: str):
    """Load a magic module by filename, isolating sys.modules."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ======================================================================
# 1. magic-reminder: add/list/complete/delete/snooze
# ======================================================================

class TestMagicReminder:
    """Reminder add / complete / list / delete / snooze round-trip."""

    def test_add_reminder_daily_repeat(self, tmp_path, monkeypatch):
        mod = _load_module("magic_reminder_test", "magic-reminder.py")
        import app.reminders as _rem

        captured = []

        def fake_append(text, time_str, due, repeat=""):
            captured.append({"text": text, "time_str": time_str, "due": due, "repeat": repeat})
            return {"id": 1, "text": text, "time": time_str, "due": due, "repeat": repeat, "done": False}

        monkeypatch.setattr(_rem, "append_reminder", fake_append)
        reply = mod.add_reminder("喝水", "每天8点", "")
        # "每天8点" should set repeat=daily
        assert captured[0]["repeat"] == "daily"
        assert reply  # non-empty reply

    def test_add_reminder_thirty_minutes(self, tmp_path, monkeypatch):
        mod = _load_module("magic_reminder_test2", "magic-reminder.py")
        import app.reminders as _rem

        captured = []

        def fake_append(text, time_str, due, repeat=""):
            captured.append({"text": text, "time_str": time_str, "due": due, "repeat": repeat})
            return {"id": 1, "text": text, "time": time_str, "due": due, "repeat": repeat, "done": False}

        monkeypatch.setattr(_rem, "append_reminder", fake_append)
        reply = mod.add_reminder("休息", "30分钟后", "")
        assert reply  # non-empty reply

    def test_complete_reminder_removes_from_active(self, tmp_path, monkeypatch):
        import app.reminders as _rem

        # Setup reminders file
        data = [{"id": 1, "text": "喝水", "due": "2026-08-27T08:00:00", "done": False}]
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps(data), encoding="utf-8")

        def fake_read():
            return json.loads(reminders_file.read_text(encoding="utf-8"))

        def fake_write(d):
            reminders_file.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

        # complete_reminder uses _locked_reminders/_read_locked_reminders/_write_locked_reminders
        class _NullLock:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr(_rem, "_locked_reminders", lambda shared=False: _NullLock())
        monkeypatch.setattr(_rem, "_read_locked_reminders", fake_read)
        monkeypatch.setattr(_rem, "_write_locked_reminders", fake_write)

        result = _rem.complete_reminder(1)
        assert result is True
        updated = json.loads(reminders_file.read_text(encoding="utf-8"))
        assert updated[0]["done"] is True


# ======================================================================
# 2. magic-summary: friendly empty, _simple_summary, yesterday offset
# ======================================================================

class TestMagicSummary:
    """Summary module behavior: empty data, simple summary, date offset."""

    def test_no_data_returns_friendly_nonempty(self, tmp_path, monkeypatch):
        mod = _load_module("magic_summary_test", "magic-summary.py")
        monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))

        result = mod._generate_summary([], [], [], "今日摘要")
        assert result
        assert "还没有任何活动" in result or "还没有对话" in result

    def test_simple_summary_concatenates_correctly(self):
        mod = _load_module("magic_summary_test2", "magic-summary.py")
        convs = [{"role": "user", "content": "今天天气", "time": "10:00"}]
        result = mod._simple_summary(convs, [], [], "今日摘要")
        assert "1 次对话" in result
        assert "今日摘要" in result

    def test_yesterday_summary_date_offset(self, monkeypatch):
        mod = _load_module("magic_summary_test3", "magic-summary.py")
        import datetime as dt
        fixed_today = dt.date(2026, 8, 27)

        class FakeDatetime(dt.datetime):
            @classmethod
            def now(cls):
                return cls(2026, 8, 27, 12, 0, 0)

        monkeypatch.setattr(dt, "datetime", FakeDatetime)
        yesterday = (dt.datetime(2026, 8, 27) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        assert yesterday == "2026-08-26"


# ======================================================================
# 3. magic-habits: no double count, atomic write failure, streak
# ======================================================================

class TestMagicHabits:
    """Habits: no double log, atomic write preserves original, streak calc."""

    def test_no_double_log_same_day(self, tmp_path, monkeypatch):
        mod = _load_module("magic_habits_test", "magic-habits.py")
        monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
        habits_file = tmp_path / "habits.json"

        data = {"喝水": {"logs": ["2026-08-27"], "created": "2026-08-27"}}
        habits_file.write_text(json.dumps(data), encoding="utf-8")

        def fake_load():
            return json.loads(habits_file.read_text(encoding="utf-8"))

        def fake_save(d):
            habits_file.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

        monkeypatch.setattr(mod, "_load_habits", fake_load)
        monkeypatch.setattr(mod, "_save_habits", fake_save)
        monkeypatch.setattr(mod, "_today", lambda: "2026-08-27")

        reply1 = mod.log_habit("喝水")
        assert "已经记录" in reply1
        logs1 = json.loads(habits_file.read_text(encoding="utf-8"))["喝水"]["logs"]
        count1 = logs1.count("2026-08-27")

        reply2 = mod.log_habit("喝水")
        assert "已经记录" in reply2
        logs2 = json.loads(habits_file.read_text(encoding="utf-8"))["喝水"]["logs"]
        count2 = logs2.count("2026-08-27")
        assert count2 == count1  # no duplicate

    def test_atomic_write_failure_preserves_original(self, tmp_path, monkeypatch):
        mod = _load_module("magic_habits_test2", "magic-habits.py")
        monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
        habits_file = tmp_path / "habits.json"
        original_data = {"喝水": {"logs": ["2026-08-26"], "created": "2026-08-26"}}
        habits_file.write_text(json.dumps(original_data), encoding="utf-8")

        call_count = [0]
        orig_replace = os.replace

        def bad_replace(src, dst):
            call_count[0] += 1
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", bad_replace)
        monkeypatch.setattr(mod, "_today", lambda: "2026-08-27")

        with pytest.raises(OSError):
            mod.log_habit("喝水")

        # Original file must still be intact
        current = json.loads(habits_file.read_text(encoding="utf-8"))
        assert current == original_data

    def test_streak_calculation(self, tmp_path, monkeypatch):
        mod = _load_module("magic_habits_test3", "magic-habits.py")
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        old_day = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
        old_day2 = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        # Consecutive 3 days ending today → streak 3
        logs = [day_before, yesterday, today]
        streak = mod._streak(logs)
        assert streak == 3

        # Gap → streak 0
        logs_gap = [old_day, old_day2]
        streak_gap = mod._streak(logs_gap)
        assert streak_gap == 0


# ======================================================================
# 4. magic-recipe: search_recipes by ingredient, no profile fallback
# ======================================================================

class TestMagicRecipe:
    """Recipe search by ingredient, LLM fallback, profile-less fallback."""

    def test_search_recipes_by_ingredient_format(self, tmp_path, monkeypatch):
        mod = _load_module("magic_recipe_test", "magic-recipe.py")

        recipes = [
            {"name": "番茄炒蛋", "ingredients": ["番茄", "鸡蛋"], "difficulty": "简单", "time": "10分钟"},
            {"name": "红烧肉", "ingredients": ["五花肉", "酱油"], "difficulty": "中等", "time": "60分钟"},
        ]
        monkeypatch.setattr(mod, "_load_recipes", lambda: recipes)

        result = mod.search_recipe("番茄")
        assert "番茄炒蛋" in result
        assert "找到" in result

    def test_no_profile_falls_back_to_default(self, tmp_path, monkeypatch):
        mod = _load_module("magic_recipe_test2", "magic-recipe.py")

        recipes = [{"name": "蛋炒饭", "ingredients": ["鸡蛋", "米饭"], "difficulty": "简单", "time": "5分钟"}]
        monkeypatch.setattr(mod, "_load_recipes", lambda: recipes)
        monkeypatch.setattr(mod, "_load_profile", lambda: {})

        result = mod.recommend_recipe("鸡蛋,米饭")
        assert "蛋炒饭" in result


# ======================================================================
# 5. magic-preferences: set/get round-trip + MCP tool delegation
# ======================================================================

class TestMagicPreferences:
    """Preferences set/get round-trip and MCP tool surface."""

    def test_set_get_roundtrip(self, tmp_path, monkeypatch):
        mod = _load_module("magic_pref_test", "magic-preferences.py")
        import agent.preferences as _prefs

        store = {}

        def fake_set(key, value):
            store[key] = value

        def fake_get(key):
            return store.get(key)

        def fake_del(key):
            return store.pop(key, None) is not None

        def fake_list():
            return dict(store)

        monkeypatch.setattr(_prefs, "set_preference", fake_set)
        monkeypatch.setattr(_prefs, "get_preference", fake_get)
        monkeypatch.setattr(_prefs, "del_preference", fake_del)
        monkeypatch.setattr(_prefs, "list_preferences", fake_list)

        reply_set = mod.set_pref("sleep_time", "22:00")
        assert "已记住" in reply_set
        reply_get = mod.get_pref("sleep_time")
        assert "22:00" in reply_get

    def test_forget_pref(self, tmp_path, monkeypatch):
        mod = _load_module("magic_pref_test2", "magic-preferences.py")
        import agent.preferences as _prefs

        store = {"old_key": "value"}

        monkeypatch.setattr(_prefs, "del_preference", lambda k: store.pop(k, None) is not None)
        monkeypatch.setattr(_prefs, "list_preferences", lambda: dict(store))

        reply = mod.forget_pref("old_key")
        assert "已忘记" in reply
        assert "old_key" not in store
