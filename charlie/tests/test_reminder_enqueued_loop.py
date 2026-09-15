"""真实事故回归测试：起床提醒 enqueued 无限循环 64 次（2026-09-07 用户日志）

事故链路：提醒 08:00 到期 → ESP32 离线 → push_tts_to_xiaozhi 返回 enqueued →
mark_reminder_enqueued 设 retry_after=+5min → 调度循环重新申领 → 再入队 → 无限循环。
两个根因：
1. enqueued 状态没有重试上限（attempt_count 涨到 64 仍在跑）
2. 过期判定 expiry_base = max(due, retry_after)，retry_after 每次 +5min 把过期点
   不断后推，"超过 EXPIRED_AFTER_HOURS 过期"永远不触发

修复：过期锚定 due；enqueued 重试超 ENQUEUED_MAX_ATTEMPTS(默认6) 标记 expired_enqueued。
"""
import datetime as dt
import os
import sys
import types as _types
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _fake_dt_module(fake_now):
    """构造替换 app.reminders.dt 的假 datetime 模块（now 返回固定值）。"""
    class _FakeDatetime:
        @staticmethod
        def now():
            return fake_now

        @staticmethod
        def fromisoformat(s):
            return dt.datetime.fromisoformat(s)

    return _types.SimpleNamespace(datetime=_FakeDatetime, timedelta=dt.timedelta)


def _stuck_wakeup_reminder(attempt_count: int, now: dt.datetime) -> dict:
    """复刻真实数据形状（data/reminders.json id=1788652234 起床）。"""
    return {
        "id": 1788652234,
        "text": "起床",
        "time": "明天8点",
        "due": "2026-09-07T08:00:00",
        "done": False,
        "repeat": "",
        "delivery_state": "enqueued",
        "attempt_count": attempt_count,
        "retry_after": (now + dt.timedelta(minutes=5)).isoformat(),
    }


class TestEnqueuedRetryCap:
    """enqueued 重试上限：设备持续离线时不再无限重投。"""

    def test_over_cap_marks_expired_enqueued(self):
        """attempt_count=64（真实事故值）→ 停止重投。

        due=08:00 距 now=13:19 已超 2 小时，先命中过期锚定检查 → expired。
        """
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 13, 19, 15)
        stuck = _stuck_wakeup_reminder(attempt_count=64, now=fake_now)
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert claimed == []
        assert stuck["done"] is True
        assert stuck["delivery_state"] in ("expired", "expired_enqueued")

    def test_over_cap_within_expiry_window_marks_expired_enqueued(self):
        """due 未超 2 小时但 attempt 超上限 → expired_enqueued（纯上限路径）。

        事故初期形态：08:00 提醒，08:35 已入队 7 次（超过默认上限 6）。
        """
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 8, 35, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=7, now=fake_now)
        stuck["retry_after"] = "2026-09-07T08:34:00"  # 已过，本可重新申领
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert claimed == []
        assert stuck["done"] is True
        assert stuck["delivery_state"] == "expired_enqueued"

    def test_at_cap_boundary_marks_expired_enqueued(self):
        """恰好达到上限（6 次）也停止——真实循环在 5min×6≈30min 后应停。"""
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 8, 30, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=6, now=fake_now)
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert claimed == []
        assert stuck["delivery_state"] == "expired_enqueued"

    def test_below_cap_retry_window_still_waits(self):
        """未达上限且 retry_after 未到 → 不申领也不标记（保持 enqueued）。"""
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 8, 12, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=2, now=fake_now)
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert claimed == []
        assert stuck["done"] is False
        assert stuck["delivery_state"] == "enqueued"

    def test_below_cap_retry_window_elapsed_reclaims(self):
        """未达上限且 retry_after 已过 → 正常重新申领（attempt+1）。"""
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 8, 11, 0)
        # retry_after 已过（8:00+5min=8:05 < 8:11）
        stuck = _stuck_wakeup_reminder(attempt_count=2, now=fake_now)
        stuck["retry_after"] = "2026-09-07T08:05:00"
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert len(claimed) == 1
        assert claimed[0]["id"] == 1788652234
        assert stuck["attempt_count"] == 3
        assert stuck["delivery_state"] == "delivering"


class TestExpiryAnchoredToDue:
    """过期判定锚定 due：retry_after 不再续命。"""

    def test_sliding_retry_after_cannot_extend_life(self):
        """due 已过 2.5 小时，即使 retry_after 刚被推到 1 分钟后（续命场景）也必须过期。

        这正是事故形态：08:00 的提醒在 10:30 仍因 retry_after=10:31 而继续循环。
        """
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 10, 30, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=30, now=fake_now)
        # due=08:00，now=10:30 → 超过 2 小时；retry_after=10:31 刚好在未来
        stuck["retry_after"] = "2026-09-07T10:31:00"
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert claimed == []
        assert stuck["done"] is True
        assert stuck["delivery_state"] == "expired"

    def test_within_2h_of_due_still_claimable(self):
        """due 1 小时前 → 不过期，正常申领（不误伤）。"""
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 9, 0, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=3, now=fake_now)
        stuck["retry_after"] = "2026-09-07T08:55:00"  # 已过
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            claimed = rem.claim_due_reminders()

        assert len(claimed) == 1
        assert stuck["delivery_state"] == "delivering"

    def test_restore_after_restart_uses_due_not_retry_after(self):
        """restore_after_restart 同样锚定 due：过期提醒不再被 retry_after 续命跳过。"""
        from app import reminders as rem

        fake_now = dt.datetime(2026, 9, 7, 10, 30, 0)
        stuck = _stuck_wakeup_reminder(attempt_count=30, now=fake_now)
        stuck["retry_after"] = "2026-09-07T10:31:00"
        with patch.object(rem, "_read_locked_reminders", return_value=[stuck]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(fake_now)):
            # 已过期（due 超 2h）→ restore 不应把它从 enqueued 恢复为 pending
            rem.restore_after_restart()

        assert stuck["delivery_state"] == "enqueued"  # 保持原状，交给 claim 标 expired


class TestEnqueuedCapConfig:
    """ENQUEUED_MAX_ATTEMPTS 可通过环境变量调整。"""

    def test_default_is_6(self):
        from app.reminders import ENQUEUED_MAX_ATTEMPTS
        assert ENQUEUED_MAX_ATTEMPTS == 6

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("REMINDER_ENQUEUED_MAX_ATTEMPTS", "12")
        import importlib
        import app.reminders as rem
        importlib.reload(rem)
        assert rem.ENQUEUED_MAX_ATTEMPTS == 12
        # 还原（reload 副作用）
        monkeypatch.delenv("REMINDER_ENQUEUED_MAX_ATTEMPTS")
        importlib.reload(rem)
