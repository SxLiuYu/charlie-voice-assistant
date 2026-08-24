"""Tests for app.reminders: complete_reminder_delivery repeat logic."""
import datetime as dt
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Helpers
# ======================================================================

def _make_reminder(reminder_id: int, text: str, due: str, repeat: str, done: bool = False):
    return {
        "id": reminder_id,
        "text": text,
        "due": due,
        "repeat": repeat if repeat in ("daily", "weekly", "weekdays") else "",
        "done": done,
        "time": "",
    }


# ======================================================================
# P1-3: complete_reminder_delivery repeat generation
# ======================================================================

class TestCompleteReminderDeliveryRepeat:
    """完成提醒后，循环提醒生成下一次到期时间。"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_daily_repeat_generates_next_due(self, mock_read, mock_write):
        """daily: due=昨天, complete 后新 due > now"""
        from app.reminders import complete_reminder_delivery
        import app.reminders as rem

        yesterday = (dt.datetime.now() - dt.timedelta(days=1)).isoformat()
        reminders = [_make_reminder(1, "喝水", yesterday, "daily", done=False)]
        mock_read.return_value = reminders

        # Patch the module's dt reference with a fake module that has datetime/timedelta
        fake_now = dt.datetime(2026, 8, 22, 12, 0, 0)

        class FakeDt:
            class datetime:
                @staticmethod
                def now():
                    return fake_now

                @staticmethod
                def fromisoformat(s):
                    return dt.datetime.fromisoformat(s)

            class timedelta:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def __radd__(self, other):
                    return other + dt.timedelta(**self.kwargs)

        orig_dt = rem.dt
        rem.dt = FakeDt()
        try:
            complete_reminder_delivery(1)
        finally:
            rem.dt = orig_dt

        # The original reminder should be marked done
        assert reminders[0]["done"] is True
        assert reminders[0]["delivery_state"] == "delivered"

        # A new reminder should have been appended
        assert len(reminders) == 2
        new_reminder = reminders[1]
        assert new_reminder["text"] == "喝水"
        assert new_reminder["repeat"] == "daily"
        assert new_reminder["done"] is False
        # due should be >= fake_now
        new_due = dt.datetime.fromisoformat(new_reminder["due"])
        assert new_due >= fake_now

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_weekly_repeat_generates_next_same_weekday(self, mock_read, mock_write):
        """weekly: due=上周, complete 后下次是 > now 的同一星期几"""
        from app.reminders import complete_reminder_delivery
        import app.reminders as rem

        fake_now = dt.datetime(2026, 8, 22, 12, 0, 0)  # Saturday
        last_saturday = (fake_now - dt.timedelta(days=7)).isoformat()
        reminders = [_make_reminder(2, "周报", last_saturday, "weekly", done=False)]
        mock_read.return_value = reminders

        class FakeDt:
            class datetime:
                @staticmethod
                def now():
                    return fake_now

                @staticmethod
                def fromisoformat(s):
                    return dt.datetime.fromisoformat(s)

            class timedelta:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def __radd__(self, other):
                    return other + dt.timedelta(**self.kwargs)

        orig_dt = rem.dt
        rem.dt = FakeDt()
        try:
            complete_reminder_delivery(2)
        finally:
            rem.dt = orig_dt

        assert len(reminders) == 2
        new_reminder = reminders[1]
        new_due = dt.datetime.fromisoformat(new_reminder["due"])
        # Should be next Saturday (same weekday, 7 days later)
        assert new_due.weekday() == 5  # Saturday
        assert new_due >= fake_now

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_weekdays_repeat_skips_weekend(self, mock_read, mock_write):
        """weekdays: due=上周五, complete 后下次是 >= now 的工作日（周一）"""
        from app.reminders import complete_reminder_delivery
        import app.reminders as rem

        fake_now = dt.datetime(2026, 8, 17, 9, 0, 0)  # Sunday
        last_friday = (fake_now - dt.timedelta(days=7)).isoformat()
        reminders = [_make_reminder(3, "上班", last_friday, "weekdays", done=False)]
        mock_read.return_value = reminders

        class FakeDt:
            class datetime:
                @staticmethod
                def now():
                    return fake_now

                @staticmethod
                def fromisoformat(s):
                    return dt.datetime.fromisoformat(s)

            class timedelta:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def __radd__(self, other):
                    return other + dt.timedelta(**self.kwargs)

        orig_dt = rem.dt
        rem.dt = FakeDt()
        try:
            complete_reminder_delivery(3)
        finally:
            rem.dt = orig_dt

        assert len(reminders) == 2
        new_reminder = reminders[1]
        new_due = dt.datetime.fromisoformat(new_reminder["due"])
        # Should be Monday (skip weekend)
        assert new_due.weekday() == 0  # Monday
        assert new_due >= fake_now

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_non_repeat_no_new_reminder(self, mock_read, mock_write):
        """无 repeat 的提醒 complete 后不生成新提醒"""
        from app.reminders import complete_reminder_delivery

        now = dt.datetime.now().isoformat()
        reminders = [_make_reminder(4, "一次性", now, "", done=False)]
        mock_read.return_value = reminders

        complete_reminder_delivery(4)

        # Only the original reminder, marked done
        assert len(reminders) == 1
        assert reminders[0]["done"] is True

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_unknown_repeat_type_no_new_reminder(self, mock_read, mock_write):
        """未知 repeat 值不生成新提醒"""
        from app.reminders import complete_reminder_delivery

        now = dt.datetime.now().isoformat()
        reminders = [_make_reminder(5, "异常", now, "monthly", done=False)]
        mock_read.return_value = reminders

        complete_reminder_delivery(5)

        assert len(reminders) == 1
        assert reminders[0]["done"] is True

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_daily_repeat_when_due_is_far_in_past(self, mock_read, mock_write):
        """daily: due=很久以前, complete 后新 due 应该 > now"""
        from app.reminders import complete_reminder_delivery
        import app.reminders as rem

        fake_now = dt.datetime(2026, 8, 22, 12, 0, 0)
        long_ago = (fake_now - dt.timedelta(days=30)).isoformat()
        reminders = [_make_reminder(6, "老提醒", long_ago, "daily", done=False)]
        mock_read.return_value = reminders

        class FakeDt:
            class datetime:
                @staticmethod
                def now():
                    return fake_now

                @staticmethod
                def fromisoformat(s):
                    return dt.datetime.fromisoformat(s)

            class timedelta:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def __radd__(self, other):
                    return other + dt.timedelta(**self.kwargs)

        orig_dt = rem.dt
        rem.dt = FakeDt()
        try:
            complete_reminder_delivery(6)
        finally:
            rem.dt = orig_dt

        assert len(reminders) == 2
        new_due = dt.datetime.fromisoformat(reminders[1]["due"])
        # The loop stops when next_due >= now, so new_due can equal now
        assert new_due >= fake_now
