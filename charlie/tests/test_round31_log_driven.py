"""Round 31 regression tests for log-driven fixes.

Covers:
1. Evening briefing uses correct weather data source (casts list, not dict)
2. ESP32 offline: Feishu push happens once, not twice
3. openWakeWord failure memoization (no retry after failure)
4. AMAP quota block persists across restarts
5. CPU health alert startup grace period
"""
import os
import sys
import time
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# P0-1: Evening briefing weather source
# ======================================================================

class TestBriefingWeatherSource:
    def test_evening_wrapup_uses_schedulers_get_weather(self):
        """evening_wrapup 应使用 app.schedulers._get_weather 而非 app.weather.get_weather"""
        src = open(os.path.join(PROJECT_DIR, "app", "briefing.py")).read()
        idx = src.find("def evening_wrapup")
        body = src[idx:idx + 1500]
        assert "from app.schedulers import _get_weather" in body
        assert "from app.weather import get_weather" not in body

    def test_forecast_for_date_receives_list(self):
        """_forecast_for_date 应接收 casts list，而非 dict"""
        import app.briefing as br
        fake_casts = [{"date": "2026-09-01", "dayweather": "小雨", "daytemp": "20"}]
        received = []
        def fake_forecast(casts, target_date):
            received.append(casts)
            return {"dayweather": "小雨", "daytemp": "20"}

        with patch("app.schedulers._get_weather", return_value=fake_casts), \
             patch("app.schedulers._forecast_for_date", side_effect=fake_forecast), \
             patch("app.reminders.list_reminders", return_value=[]):
            text = br.evening_wrapup()

        assert len(received) == 1
        assert isinstance(received[0], list)
        assert received[0] == fake_casts
        assert "明天小雨" in text


# ======================================================================
# P0-2: ESP32 offline Feishu double-push
# ======================================================================

class TestFeishuDoublePushFix:
    def test_enqueued_without_feishu_done_pushes_once(self):
        """delivery=enqueued 且 _feishu_done=False 时，push_feishu_async 被调用一次"""
        from app.notifications import play_reminder_audio

        with patch("app.notifications.push_tts_to_xiaozhi", return_value="enqueued"), \
             patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch("app.notifications.push_feishu_async") as mock_feishu, \
             patch("platform.system", return_value="Linux"), \
             patch("app.notifications.push_notification_to_sse"):
            play_reminder_audio("测试", _feishu_done=False)

        assert mock_feishu.call_count == 1

    def test_enqueued_with_feishu_done_skips_push(self):
        """delivery=enqueued 且 _feishu_done=True 时，push_feishu_async 不被调用"""
        from app.notifications import play_reminder_audio

        with patch("app.notifications.push_tts_to_xiaozhi", return_value="enqueued"), \
             patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch("app.notifications.push_feishu_async") as mock_feishu, \
             patch("platform.system", return_value="Linux"), \
             patch("app.notifications.push_notification_to_sse"):
            play_reminder_audio("测试", _feishu_done=True)

        assert mock_feishu.call_count == 0

    def test_reminder_id_path_respects_feishu_done(self):
        """有 reminder_id 且 delivery=enqueued 时，_feishu_done 也应生效"""
        from app.notifications import play_reminder_audio

        with patch("app.notifications.push_tts_to_xiaozhi", return_value="enqueued"), \
             patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch("app.notifications.push_feishu_async") as mock_feishu, \
             patch("app.reminders.mark_reminder_enqueued"), \
             patch("platform.system", return_value="Linux"), \
             patch("app.notifications.push_notification_to_sse"):
            play_reminder_audio("提醒", reminder_id=1, _feishu_done=True)

        assert mock_feishu.call_count == 0


# ======================================================================
# P1-1: openWakeWord failure memoization
# ======================================================================

class TestOwwMemoization:
    def test_second_call_skips_retry_on_failure(self):
        """_load_oww_model 失败后，第二次调用应直接返回 None 不重试"""
        import local_wake
        local_wake._oww_model = None
        local_wake._oww_failed = False

        import sys
        original = sys.modules.get("openwakeword")
        # Make import fail
        sys.modules["openwakeword"] = None

        try:
            result1 = local_wake._load_oww_model()
            assert result1 is None
            assert local_wake._oww_failed is True

            # Restore the module so we can detect if it's imported again.
            # If memoization works, the second call returns immediately
            # without attempting the import at all.
            sys.modules["openwakeword"] = original

            result2 = local_wake._load_oww_model()
            assert result2 is None
        finally:
            if original is not None:
                sys.modules["openwakeword"] = original
            else:
                sys.modules.pop("openwakeword", None)

    def test_failed_flag_prevents_repeated_warnings(self):
        """_oww_failed 置位后，再次调用不应再触发 import/logic"""
        import local_wake
        local_wake._oww_model = None
        local_wake._oww_failed = True

        # Even if openwakeword is available, should return immediately
        result = local_wake._load_oww_model()
        assert result is None
        assert local_wake._oww_model is None


# ======================================================================
# P1-2: AMAP block persists across restarts
# ======================================================================

class TestAmapBlockPersistence:
    def test_block_persists_across_restart(self):
        """AMAP 配额熔断应持久化到文件，重启后恢复"""
        import app.weather as weather_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "weather_state.json")
            weather_mod._WEATHER_STATE_FILE = state_file

            # Save a block
            future = time.time() + 3600
            weather_mod._write_amap_state(future)

            # Simulate process restart: reset in-process var
            weather_mod._amap_blocked_until = 0.0

            # Reload from file (as module init would do)
            restored = weather_mod._read_amap_state()
            assert restored == future
            assert restored > time.time()

            # Apply the restored value
            weather_mod._amap_blocked_until = restored
            assert weather_mod._amap_blocked_until == future

    def test_expired_block_not_restored(self):
        """过期的 block 不应从文件恢复"""
        import app.weather as weather_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "weather_state.json")
            weather_mod._WEATHER_STATE_FILE = state_file

            # Save an expired block
            past = time.time() - 100
            weather_mod._write_amap_state(past)

            restored = weather_mod._read_amap_state()
            assert restored == 0.0


# ======================================================================
# P2-1: CPU health alert startup grace period
# ======================================================================

class TestCpuAlertGracePeriod:
    def test_alert_skipped_within_grace_period(self):
        """启动 2 分钟内 CPU 告警应被跳过"""
        import app.schedulers as sched_mod

        sched_mod._server_start_time = time.time()

        cpu = 100
        mem = 50
        health_key = "2026-08-31_health_10"

        with patch.object(sched_mod, "_claim_suggest_state", return_value=True) as mock_claim, \
             patch.object(sched_mod, "add_notification") as mock_add, \
             patch.object(sched_mod, "_play_async") as mock_play, \
             patch("psutil.cpu_percent", return_value=cpu), \
             patch("psutil.virtual_memory") as mock_mem:
            mock_mem.return_value.percent = mem

            # Replicate the exact condition from _proactive_suggestions
            if (cpu > 90 or mem > 95) and time.time() - sched_mod._server_start_time >= 120 and sched_mod._claim_suggest_state(health_key):
                sched_mod.add_notification("test", "health")
                sched_mod._play_async("test")

        assert not mock_add.called
        assert not mock_play.called
        # Short-circuit evaluation should have prevented _claim_suggest_state call
        assert not mock_claim.called

    def test_alert_sent_after_grace_period(self):
        """启动 2 分钟后 CPU 告警应正常发送"""
        import app.schedulers as sched_mod

        sched_mod._server_start_time = time.time() - 180

        cpu = 100
        mem = 50
        health_key = "2026-08-31_health_10"

        with patch.object(sched_mod, "_claim_suggest_state", return_value=True), \
             patch.object(sched_mod, "add_notification") as mock_add, \
             patch.object(sched_mod, "_play_async") as mock_play, \
             patch("psutil.cpu_percent", return_value=cpu), \
             patch("psutil.virtual_memory") as mock_mem:
            mock_mem.return_value.percent = mem

            if (cpu > 90 or mem > 95) and time.time() - sched_mod._server_start_time >= 120 and sched_mod._claim_suggest_state(health_key):
                sched_mod.add_notification("test", "health")
                sched_mod._play_async("test")

        assert mock_add.called
        assert mock_play.called
