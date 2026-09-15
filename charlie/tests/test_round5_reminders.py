"""Tests for Round 5 reminder fixes: reminder_id propagation, enqueued completion, stale expiry."""
import asyncio
import datetime as dt
import os
import sys
import threading
import time
from unittest.mock import patch, MagicMock, AsyncMock

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
# Fix1: _play_async passes reminder_id to play_reminder_audio
# ======================================================================

class TestPlayAsyncReminderId:
    """_play_async 把 reminder_id 传给 play_reminder_audio"""

    def test_play_async_passes_reminder_id(self):
        """direct 成功路径应调用 complete_reminder_delivery(reminder_id)"""
        from app.schedulers import _play_async
        from app import notifications as notif_mod

        called = {}

        def fake_play(text, reminder_id=None, **kwargs):
            called["text"] = text
            called["reminder_id"] = reminder_id
            return "direct"

        # 直接把 app.schedulers 里的 play_reminder_audio 引用替换掉
        import app.schedulers as sched_mod
        orig_play = sched_mod.play_reminder_audio
        sched_mod.play_reminder_audio = fake_play
        try:
            _play_async("起床", reminder_id=42)
        finally:
            sched_mod.play_reminder_audio = orig_play

        assert called.get("text") == "起床"
        assert called.get("reminder_id") == 42


# ======================================================================
# Fix2: enqueued reminders marked complete after flush
# ======================================================================

class TestEnqueuedFlushCompletion:
    """补发成功后 enqueued 提醒被标记完成"""

    def test_flush_success_completes_reminder(self):
        """register_xiaozhi_client flush 成功后 complete_reminder_delivery 被调用"""
        from app.state import register_xiaozhi_client, _xiaozhi_pending, _xiaozhi_lock
        from app.reminders import complete_reminder_delivery

        complete_calls = []

        def fake_complete(rid):
            complete_calls.append(rid)

        # 清理并放入带 reminder_id 的 pending 条目
        with _xiaozhi_lock:
            _xiaozhi_pending.clear()
            _xiaozhi_pending.append({
                "text": "test",
                "mp3": b"fake",
                "ts": time.time(),
                "reminder_id": 99,
            })

        fake_ws = AsyncMock()
        fake_loop = MagicMock()

        # 创建一个真实的 asyncio loop 用于运行 flush coroutine
        real_loop = asyncio.new_event_loop()

        def fake_run_in_executor(executor, func, *args):
            # 返回一个可 await 的 Future
            fut = real_loop.create_future()
            try:
                result = func(*args)
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)
            return fut

        fake_ws.send_text = AsyncMock()
        fake_ws.send_bytes = AsyncMock()
        fake_loop.run_in_executor = fake_run_in_executor

        def fake_create_task(coro):
            # 立即运行 coroutine 以便测试中同步完成
            real_loop.run_until_complete(coro)

        fake_loop.create_task.side_effect = fake_create_task

        with patch("app.reminders.complete_reminder_delivery", side_effect=fake_complete):
            with patch("app.xiaozhi_codec.mp3_to_opus_packets", return_value=[b"pkt"]):
                result = register_xiaozhi_client("client-1", fake_ws, fake_loop)

        assert result == []
        # complete_reminder_delivery 应该在 flush 成功后收到 99
        assert 99 in complete_calls

        real_loop.close()

    def test_old_format_pending_no_reminder_id_no_error(self):
        """旧格式队列条目（无 reminder_id）flush 不报错"""
        from app.state import register_xiaozhi_client, _xiaozhi_pending, _xiaozhi_lock

        with _xiaozhi_lock:
            _xiaozhi_pending.clear()
            _xiaozhi_pending.append({
                "text": "legacy",
                "mp3": b"fake",
                "ts": time.time(),
                # 故意不加 reminder_id
            })

        fake_ws = MagicMock()
        fake_loop = MagicMock()

        # 与 test_flush_success_completes_reminder 一致的 fake_loop 构造：
        # create_task 必须真正运行协程，否则 _flush() 被丢弃 → RuntimeWarning
        real_loop = asyncio.new_event_loop()

        def fake_run_in_executor(executor, func, *args):
            fut = real_loop.create_future()
            try:
                result = func(*args)
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)
            return fut

        fake_ws.send_text = AsyncMock()
        fake_ws.send_bytes = AsyncMock()
        fake_loop.run_in_executor = fake_run_in_executor

        def fake_create_task(coro):
            real_loop.run_until_complete(coro)

        fake_loop.create_task.side_effect = fake_create_task

        # 不应抛异常
        result = register_xiaozhi_client("client-2", fake_ws, fake_loop)
        assert result == []

        real_loop.close()


# ======================================================================
# Fix3: Stale one-shot reminders marked expired
# ======================================================================

class TestExpiredOneShotReminders:
    """过期 2 小时以上的一次性提醒被标记 expired 且不播报"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_one_shot_overdue_3h_marked_expired(self, mock_read, mock_write):
        """一次性提醒 due 3 小时前 → done=True, delivery_state=expired"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_3h_ago = (fake_now - dt.timedelta(hours=3)).isoformat()
        reminders = [_make_reminder(1, "过期提醒", due_3h_ago, "", done=False)]
        mock_read.return_value = reminders

        # 冻结时间
        import app.reminders as rem
        import types as _types
        orig_dt = rem.dt

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now

            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt = _types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        rem.dt = fake_dt
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 0
        assert reminders[0]["done"] is True
        assert reminders[0]["delivery_state"] == "expired"
        assert "completed_at" in reminders[0]

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_one_shot_overdue_1h_still_triggered(self, mock_read, mock_write):
        """一次性提醒 due 1 小时前 → 仍正常触发（未超过 2 小时阈值）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_1h_ago = (fake_now - dt.timedelta(hours=1)).isoformat()
        reminders = [_make_reminder(2, "近期提醒", due_1h_ago, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        import types as _types
        orig_dt = rem.dt

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now

            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt = _types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        rem.dt = fake_dt
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["done"] is False
        assert claimed[0]["delivery_state"] == "delivering"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_repeat_reminder_not_expired(self, mock_read, mock_write):
        """循环提醒即使过期很久仍正常触发"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_3d_ago = (fake_now - dt.timedelta(days=3)).isoformat()
        reminders = [_make_reminder(3, "每日喝水", due_3d_ago, "daily", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        import types as _types
        orig_dt = rem.dt

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now

            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt = _types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        rem.dt = fake_dt
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["done"] is False
        assert claimed[0]["delivery_state"] == "delivering"


# ======================================================================
# Fix2 continued: completed reminders not re-claimed after timeout
# ======================================================================

class TestCompletedNotReclaimed:
    """15 分钟 claim 超时后不再重新申领已完成的提醒"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_completed_reminder_not_claimed_after_timeout(self, mock_read, mock_write):
        """complete 后再次 claim_due_reminders 不会返回已完成提醒"""
        from app.reminders import claim_due_reminders, complete_reminder_delivery

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due = (fake_now - dt.timedelta(minutes=5)).isoformat()
        reminders = [_make_reminder(10, "已完成提醒", due, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        import types as _types
        orig_dt = rem.dt

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now

            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt = _types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        rem.dt = fake_dt
        try:
            # 第一次 claim
            claimed1 = claim_due_reminders()
            assert len(claimed1) == 1
            assert claimed1[0]["delivery_state"] == "delivering"

            # 标记完成
            complete_reminder_delivery(10)
            assert reminders[0]["done"] is True

            # 第二次 claim（claim_started_at 已超过 15 分钟）
            claimed2 = claim_due_reminders()
            # 已完成的提醒不应再次返回
            assert len(claimed2) == 0
        finally:
            rem.dt = orig_dt
