"""Round 13 UX fixes: enqueued marking, expiry priority, quiet hours, afplay constraint, codec gain."""
import copy
import datetime as dt
import os
import subprocess
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Helpers
# ======================================================================

def _make_reminder(reminder_id: int, text: str, due: str, repeat: str, done: bool = False,
                   delivery_state: str = "", retry_after: str = "", claim_started_at: str = ""):
    return {
        "id": reminder_id,
        "text": text,
        "due": due,
        "repeat": repeat if repeat in ("daily", "weekly", "weekdays") else "",
        "done": done,
        "time": "",
        "delivery_state": delivery_state,
        "retry_after": retry_after,
        "claim_started_at": claim_started_at,
        "attempt_count": 0,
    }


def _freeze_dt(module, fake_now: dt.datetime):
    """把 module.dt 替换为冻结时间的 fake datetime/timedelta"""
    orig_dt = module.dt

    class _FakeDatetime:
        @staticmethod
        def now():
            return fake_now

        @staticmethod
        def fromisoformat(s):
            return dt.datetime.fromisoformat(s)

    fake_dt = types.SimpleNamespace(
        datetime=_FakeDatetime,
        timedelta=dt.timedelta,
    )
    module.dt = fake_dt
    return orig_dt, fake_dt


# ======================================================================
# Fix1a: mark_reminder_enqueued
# ======================================================================

class TestMarkReminderEnqueued:
    """enqueued 标记：5min 内不再被 claim；5min 后可重试"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_enqueued_sets_state_and_retry_after(self, mock_read, mock_write):
        """mark_reminder_enqueued 后 delivery_state=enqueued，retry_after=now+5min"""
        from app.reminders import mark_reminder_enqueued

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        reminders = [_make_reminder(1, "测试", fake_now.isoformat(), "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            mark_reminder_enqueued(1)
        finally:
            rem.dt = orig_dt

        assert reminders[0]["delivery_state"] == "enqueued"
        assert "retry_after" in reminders[0]
        retry_dt = dt.datetime.fromisoformat(reminders[0]["retry_after"])
        assert retry_dt - fake_now == dt.timedelta(minutes=5)
        assert "claim_started_at" not in reminders[0]

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_enqueued_not_claimed_within_5min(self, mock_read, mock_write):
        """enqueued 且 retry_after 未到 → 不被 claim_due_reminders 返回"""
        from app.reminders import claim_due_reminders, mark_reminder_enqueued

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due = fake_now.isoformat()
        reminders = [_make_reminder(1, "测试", due, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            # 先标记为 enqueued
            mark_reminder_enqueued(1)
            # 同一时间 claim → 不应返回（retry_after 未到）
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 0

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_enqueued_claimed_after_5min(self, mock_read, mock_write):
        """enqueued 且 retry_after 已过 → 可被重新 claim"""
        from app.reminders import claim_due_reminders, mark_reminder_enqueued

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due = fake_now.isoformat()
        reminders = [_make_reminder(1, "测试", due, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            # 先标记为 enqueued
            mark_reminder_enqueued(1)
            # 6 分钟后 claim → 应返回
            fake_dt.datetime.now = staticmethod(lambda: fake_now + dt.timedelta(minutes=6))
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["delivery_state"] == "delivering"


# ======================================================================
# Fix1b: 过期优先于状态检查
# ======================================================================

class TestExpiryPriorityOverClaimLifetime:
    """过期检查移到状态检查之前， delivering 状态 + overdue>2h → expired"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_delivering_overdue_3h_marked_expired(self, mock_read, mock_write):
        """一次性提醒 delivering + overdue 3h → expired（不被 delivering 状态绕过）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due_3h_ago = (fake_now - dt.timedelta(hours=3)).isoformat()
        reminders = [_make_reminder(1, "过期提醒", due_3h_ago, "", done=False,
                                    delivery_state="delivering",
                                    claim_started_at=fake_now.isoformat())]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 0
        assert reminders[0]["done"] is True
        assert reminders[0]["delivery_state"] == "expired"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_delivering_overdue_1h_with_fresh_claim_not_reclaimed(self, mock_read, mock_write):
        """一次性提醒 delivering + overdue 1h + claim 未过期 → 仍跳过（不 re-claim）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due_1h_ago = (fake_now - dt.timedelta(hours=1)).isoformat()
        reminders = [_make_reminder(2, "近期提醒", due_1h_ago, "", done=False,
                                    delivery_state="delivering",
                                    claim_started_at=fake_now.isoformat())]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        # claim_started_at 新鲜（0s < 900s），不重复申领
        assert len(claimed) == 0
        assert reminders[0]["delivery_state"] == "delivering"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_delivering_overdue_1h_expired_claim_reclaimed(self, mock_read, mock_write):
        """一次性提醒 delivering + overdue 1h + claim 已过期 → 可重新 claim"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due_1h_ago = (fake_now - dt.timedelta(hours=1)).isoformat()
        # claim_started_at 为 20 分钟前，超过 900 秒超时
        old_claim = (fake_now - dt.timedelta(minutes=20)).isoformat()
        reminders = [_make_reminder(2, "近期提醒", due_1h_ago, "", done=False,
                                    delivery_state="delivering",
                                    claim_started_at=old_claim)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["delivery_state"] == "delivering"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_enqueued_overdue_3h_marked_expired(self, mock_read, mock_write):
        """一次性提醒 enqueued + overdue 3h → expired（不被 enqueued 状态绕过）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due_3h_ago = (fake_now - dt.timedelta(hours=3)).isoformat()
        reminders = [_make_reminder(3, "过期enqueued", due_3h_ago, "", done=False,
                                    delivery_state="enqueued")]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 0
        assert reminders[0]["done"] is True
        assert reminders[0]["delivery_state"] == "expired"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_repeat_overdue_3d_not_expired(self, mock_read, mock_write):
        """循环提醒 overdue 3 天 → 仍正常 claim（循环提醒不受过期影响）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 28, 12, 0, 0)
        due_3d_ago = (fake_now - dt.timedelta(days=3)).isoformat()
        reminders = [_make_reminder(4, "每日喝水", due_3d_ago, "daily", done=False,
                                    delivery_state="delivering")]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["repeat"] == "daily"


# ======================================================================
# Fix1d: Quiet hours
# ======================================================================

class TestQuietHours:
    """22:00-07:00 静音时段：不播报只 add_notification，retry_after=07:05"""

    def test_quiet_hours_22_to_7_detected(self, monkeypatch):
        """22:00-07:00 检测为静音时段"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 23, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        assert sched_mod._is_quiet_hours(fake_now) is True

    def test_quiet_hours_5am_detected(self, monkeypatch):
        """05:00 仍在静音时段"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 5, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        assert sched_mod._is_quiet_hours(fake_now) is True

    def test_quiet_hours_8am_not_detected(self, monkeypatch):
        """08:00 不在静音时段"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 8, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        assert sched_mod._is_quiet_hours(fake_now) is False

    def test_quiet_hours_disabled_when_same_start_end(self, monkeypatch):
        """start == end 时禁用静音时段"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 23, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "7")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        assert sched_mod._is_quiet_hours(fake_now) is False

    def test_quiet_hours_retry_time_23_is_next_day_7_05(self, monkeypatch):
        """23:00 时补播时间为明天 07:05"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 23, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        retry = sched_mod._get_quiet_hours_retry(fake_now)
        assert retry == dt.datetime(2026, 8, 29, 7, 5, 0)

    def test_quiet_hours_retry_time_5am_is_today_7_05(self, monkeypatch):
        """05:00 时补播时间为今天 07:05"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 5, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        retry = sched_mod._get_quiet_hours_retry(fake_now)
        assert retry == dt.datetime(2026, 8, 28, 7, 5, 0)

    def test_quiet_hours_no_retry_when_not_quiet(self, monkeypatch):
        """非静音时段返回 None"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 10, 0, 0)
        monkeypatch.setenv("REMINDER_QUIET_START", "22")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        retry = sched_mod._get_quiet_hours_retry(fake_now)
        assert retry is None

    def test_scheduler_skips_playback_in_quiet_hours(self, monkeypatch):
        """22:00-07:00 → add_notification 照常，_play_async 被跳过，retry_after 设为 07:05"""
        import app.schedulers as sched_mod
        from app.reminders import set_reminder_retry_after

        fake_now = dt.datetime(2026, 8, 28, 23, 0, 0)
        fake_reminder = {"id": 1, "text": "夜间提醒", "due": fake_now.isoformat(), "repeat": ""}
        add_calls = []
        play_calls = []
        retry_calls = []

        def mock_claim(now):
            return [copy.deepcopy(fake_reminder)]

        def mock_add(text, ntype):
            add_calls.append((text, ntype))

        def mock_play(text, reminder_id=None, _feishu_done=False):
            play_calls.append((text, reminder_id))

        def mock_set_retry(rid, retry_time):
            retry_calls.append((rid, retry_time))

        monkeypatch.setattr(sched_mod, "claim_due_reminders", mock_claim)
        monkeypatch.setattr(sched_mod, "add_notification", mock_add)
        monkeypatch.setattr(sched_mod, "_play_async", mock_play)
        monkeypatch.setattr(sched_mod, "set_reminder_retry_after", mock_set_retry)

        # Mock datetime at module level with a module-like object
        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now
            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt_mod = types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        monkeypatch.setattr(sched_mod, "datetime", fake_dt_mod)

        # Mock time.sleep to break the loop after first iteration
        def mock_sleep(seconds):
            raise StopIteration("break scheduler loop")
        monkeypatch.setattr(sched_mod.time, "sleep", mock_sleep)

        with pytest.raises(StopIteration):
            sched_mod._reminder_scheduler()

        assert len(add_calls) == 1
        assert "⏰ 提醒：夜间提醒" in add_calls[0][0]
        assert len(play_calls) == 0  # no playback in quiet hours
        assert len(retry_calls) == 1
        assert retry_calls[0][0] == 1
        expected_retry = dt.datetime(2026, 8, 29, 7, 5, 0)
        assert retry_calls[0][1] == expected_retry

    def test_scheduler_plays_normally_outside_quiet_hours(self, monkeypatch):
        """08:00 → 正常播放"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 8, 0, 0)
        fake_reminder = {"id": 1, "text": "晨间提醒", "due": fake_now.isoformat(), "repeat": ""}
        add_calls = []
        play_calls = []

        def mock_claim(now):
            return [copy.deepcopy(fake_reminder)]

        def mock_add(text, ntype):
            add_calls.append((text, ntype))

        def mock_play(text, reminder_id=None, _feishu_done=False):
            play_calls.append((text, reminder_id))

        monkeypatch.setattr(sched_mod, "claim_due_reminders", mock_claim)
        monkeypatch.setattr(sched_mod, "add_notification", mock_add)
        monkeypatch.setattr(sched_mod, "_play_async", mock_play)

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now
            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt_mod = types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        monkeypatch.setattr(sched_mod, "datetime", fake_dt_mod)

        def mock_sleep(seconds):
            raise StopIteration
        monkeypatch.setattr(sched_mod.time, "sleep", mock_sleep)

        with pytest.raises(StopIteration):
            sched_mod._reminder_scheduler()

        assert len(add_calls) == 1
        assert len(play_calls) == 1  # playback happens outside quiet hours
        assert play_calls[0][0] == "晨间提醒"

    def test_quiet_hours_env_disabled_allows_playback(self, monkeypatch):
        """REMINDER_QUIET_START=REMINDER_QUIET_END 禁用静音时段，23:00 也播放"""
        import app.schedulers as sched_mod

        fake_now = dt.datetime(2026, 8, 28, 23, 0, 0)
        fake_reminder = {"id": 1, "text": "深夜提醒", "due": fake_now.isoformat(), "repeat": ""}
        add_calls = []
        play_calls = []

        def mock_claim(now):
            return [copy.deepcopy(fake_reminder)]

        def mock_add(text, ntype):
            add_calls.append((text, ntype))

        def mock_play(text, reminder_id=None, _feishu_done=False):
            play_calls.append((text, reminder_id))

        monkeypatch.setattr(sched_mod, "claim_due_reminders", mock_claim)
        monkeypatch.setattr(sched_mod, "add_notification", mock_add)
        monkeypatch.setattr(sched_mod, "_play_async", mock_play)
        monkeypatch.setenv("REMINDER_QUIET_START", "7")
        monkeypatch.setenv("REMINDER_QUIET_END", "7")

        class _FakeDatetime:
            @staticmethod
            def now():
                return fake_now
            @staticmethod
            def fromisoformat(s):
                return dt.datetime.fromisoformat(s)

        fake_dt_mod = types.SimpleNamespace(
            datetime=_FakeDatetime,
            timedelta=dt.timedelta,
        )
        monkeypatch.setattr(sched_mod, "datetime", fake_dt_mod)

        def mock_sleep(seconds):
            raise StopIteration
        monkeypatch.setattr(sched_mod.time, "sleep", mock_sleep)

        with pytest.raises(StopIteration):
            sched_mod._reminder_scheduler()

        assert len(add_calls) == 1
        assert len(play_calls) == 1  # playback allowed when quiet hours disabled


# ======================================================================
# Fix1c: afplay constraint (enqueued → no local playback)
# ======================================================================

class TestAfplayConstraint:
    """enqueued 时不本地播放；direct 时才 afplay"""

    @patch("app.notifications.push_feishu_async")
    @patch("app.reminders.mark_reminder_enqueued")
    @patch("app.notifications.push_tts_to_xiaozhi", return_value="enqueued")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    @patch("platform.system", return_value="Darwin")
    @patch("voice_agent.runtime_temp_audio_path", return_value="/tmp")
    @patch("tempfile.NamedTemporaryFile")
    @patch("threading.Thread")
    def test_enqueued_no_afplay_on_darwin(
        self, mock_thread, mock_tmp, mock_runtime, mock_platform, mock_tts, mock_push, mock_mark, mock_feishu
    ):
        """Darwin + delivery=enqueued → 不调用 afplay，调用 mark_reminder_enqueued"""
        from app.notifications import play_reminder_audio

        play_reminder_audio("吃药", reminder_id=42)

        mock_tts.assert_called_once_with("主人，提醒您：吃药")
        mock_push.assert_called_once_with("主人，提醒您：吃药", b"x" * 500, reminder_id=42)
        mock_mark.assert_called_once_with(42)
        # ESP32 离线时应推飞书兜底
        mock_feishu.assert_called_once_with("主人，提醒您：吃药")
        # afplay thread must NOT be started
        mock_thread.assert_not_called()

    @patch("app.reminders.complete_reminder_delivery")
    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    @patch("platform.system", return_value="Darwin")
    @patch("voice_agent.runtime_temp_audio_path", return_value="/tmp")
    @patch("tempfile.NamedTemporaryFile")
    @patch("threading.Thread")
    def test_direct_does_afplay_on_darwin(
        self, mock_thread, mock_tmp, mock_runtime, mock_platform, mock_tts, mock_push, mock_complete
    ):
        """Darwin + delivery=direct → 调用 afplay thread"""
        from app.notifications import play_reminder_audio

        mock_tmp.return_value = MagicMock(
            __enter__=MagicMock(return_value=MagicMock(write=lambda d: None, close=lambda: None)),
            __exit__=MagicMock()
        )
        play_reminder_audio("吃药", reminder_id=42)

        mock_thread.assert_called_once()
        mock_complete.assert_called_once_with(42)


# ======================================================================
# Fix2: codec gain_db interface (already present as volume_db)
# ======================================================================

class TestCodecGain:
    """per-provider 增益接口：ffmpeg -af volume，缓存 key 区分"""

    def test_mp3_to_pcm_gain_db_adds_volume_filter(self):
        """volume_db=6.0 → ffmpeg 命令含 -af volume=6.0dB"""
        import app.xiaozhi_codec as codec

        mp3 = b"fake_mp3_data_long_enough_for_validation" * 10
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"\x00" * 2880,  # one opus frame worth of PCM
                stderr=b"")
            codec._mp3_to_pcm(mp3, sample_rate=24000, volume_db=6.0)

        cmd = mock_run.call_args[0][0]
        assert "-af" in cmd
        assert "volume=6.0dB" in cmd

    def test_mp3_to_pcm_zero_gain_no_filter(self):
        """volume_db=0.0 → 不加 -af"""
        import app.xiaozhi_codec as codec

        mp3 = b"fake_mp3_data_long_enough_for_validation" * 10
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"\x00" * 2880,
                stderr=b"")
            codec._mp3_to_pcm(mp3, sample_rate=24000, volume_db=0.0)

        cmd = mock_run.call_args[0][0]
        assert "-af" not in cmd

    def test_mp3_to_opus_cache_key_differs_by_volume(self):
        """不同 volume_db 产生不同缓存 key，不串"""
        import app.xiaozhi_codec as codec

        mp3 = b"fake_mp3_data_long_enough_for_validation" * 10
        with codec._OPUS_CACHE_LOCK:
            codec._OPUS_CACHE.clear()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"\x00" * 2880,
                stderr=b"")
            codec.mp3_to_opus_packets(mp3, sample_rate=24000, volume_db=0.0)
            codec.mp3_to_opus_packets(mp3, sample_rate=24000, volume_db=6.0)

        assert len(codec._OPUS_CACHE) == 2
        keys = list(codec._OPUS_CACHE.keys())
        assert keys[0][3] == 0.0
        assert keys[1][3] == 6.0
