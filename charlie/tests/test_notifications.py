"""Tests for app.notifications: play_reminder_audio and push_tts_to_xiaozhi."""
import asyncio
import datetime
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

class FakeWs:
    def __init__(self):
        self.sent_texts = []
        self.sent_bytes = []

    async def send_text(self, data):
        self.sent_texts.append(data)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)


def _make_client_info(ws, loop):
    return {"ws": ws, "loop": loop}


# ======================================================================
# push_tts_to_xiaozhi
# ======================================================================

class TestPushTtsToXiaozhi:
    @patch("app.state.snapshot_xiaozhi_clients")
    @patch("asyncio.run_coroutine_threadsafe")
    def test_connected_client_calls_run_coroutine(self, mock_run, mock_snapshot):
        """有连接的 client → asyncio.run_coroutine_threadsafe 被调用"""
        from app.notifications import push_tts_to_xiaozhi

        loop = asyncio.new_event_loop()
        ws = FakeWs()
        mock_snapshot.return_value = {
            "ws_1": _make_client_info(ws, loop),
        }

        push_tts_to_xiaozhi("提醒文本", b"fake_mp3_data")

        assert mock_run.called
        # Verify the coroutine was scheduled on the correct loop
        call_args = mock_run.call_args
        assert call_args is not None
        # Second arg should be the event loop from the client info
        assert call_args[0][1] == loop
        loop.close()

    @patch("app.state.enqueue_xiaozhi_pending")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_no_clients_enqueues_pending(self, mock_snapshot, mock_enqueue):
        """无连接 → enqueue_xiaozhi_pending 被调用"""
        from app.notifications import push_tts_to_xiaozhi

        mock_snapshot.return_value = {}
        mock_enqueue.return_value = 1

        push_tts_to_xiaozhi("提醒文本", b"fake_mp3_data")

        assert mock_enqueue.called
        assert mock_enqueue.call_args[0][0] == "提醒文本"


# ======================================================================
# play_reminder_audio
# ======================================================================

class TestPlayReminderAudio:
    @patch("app.reminders.complete_reminder_delivery")
    @patch("app.notifications.push_tts_to_xiaozhi")
    @patch("voice_agent.tts_to_mp3")
    @patch("platform.system", return_value="Darwin")
    @patch("voice_agent.runtime_temp_audio_path", return_value="/tmp")
    @patch("tempfile.NamedTemporaryFile")
    def test_tts_success_triggers_complete_delivery(
        self, mock_tmp, mock_runtime, mock_platform, mock_tts, mock_push, mock_complete
    ):
        """tts_to_mp3 成功 → push_tts_to_xiaozhi → complete_reminder_delivery"""
        from app.notifications import play_reminder_audio

        mock_tts.return_value = b"x" * 500  # > 100 bytes
        mock_push.return_value = "direct"  # 直推成功
        mock_tmp.return_value = MagicMock(__enter__=MagicMock(return_value=MagicMock(write=lambda d: None, close=lambda: None)), __exit__=MagicMock())

        play_reminder_audio("吃药", reminder_id=42)

        mock_tts.assert_called_once_with("主人，提醒您：吃药")
        mock_push.assert_called_once_with("主人，提醒您：吃药", b"x" * 500)
        mock_complete.assert_called_once_with(42)

    @patch("app.reminders.release_failed_reminder")
    @patch("app.reminders.complete_reminder_delivery")
    @patch("app.notifications.push_tts_to_xiaozhi")
    @patch("voice_agent.tts_to_mp3", return_value=None)
    def test_tts_failure_calls_release_not_complete(
        self, mock_tts, mock_push, mock_complete, mock_release
    ):
        """tts_to_mp3 失败 → release_failed_reminder 被调用，complete_reminder_delivery 不被调用"""
        from app.notifications import play_reminder_audio

        play_reminder_audio("吃药", reminder_id=42)

        mock_tts.assert_called_once()
        mock_push.assert_not_called()
        mock_complete.assert_not_called()
        assert mock_release.called
        assert mock_release.call_args[0][0] == 42

    @patch("app.reminders.complete_reminder_delivery")
    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    @patch("platform.system", return_value="Linux")
    @patch("app.notifications.push_notification_to_sse")
    def test_linux_path_uses_sse(
        self, mock_sse, mock_platform, mock_tts, mock_push, mock_complete
    ):
        """Linux 路径通过 SSE 推送音频（不调用 afplay）"""
        from app.notifications import play_reminder_audio

        play_reminder_audio("吃药", reminder_id=42)

        mock_sse.assert_called_once()
        # push_tts_to_xiaozhi should still be called
        mock_push.assert_called_once()
        mock_complete.assert_called_once_with(42)
