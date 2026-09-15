"""Round 6 回归测试：过期提醒不播报、飞书 hmac import、衣橱 classify_clothing await"""
import datetime as dt
import os
import sys
import types
import json
import asyncio
from io import BytesIO
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

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
# Bug 1: expired one-shot reminders must NOT enter claimed list
# ======================================================================

class TestExpiredReminderNotClaimed:
    """过期 2 小时以上的一次性提醒标记 expired 且不进入 claimed"""

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_overdue_3h_not_in_claimed_and_marked_expired(self, mock_read, mock_write):
        """一次性提醒 due 3 小时前 → 不返回 claimed，但标记 done/expired"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_3h_ago = (fake_now - dt.timedelta(hours=3)).isoformat()
        reminders = [_make_reminder(1, "过期提醒", due_3h_ago, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        # 关键回归：expired 提醒不进入 claimed
        assert len(claimed) == 0
        # 副作用：提醒被标记完成并设为 expired
        assert reminders[0]["done"] is True
        assert reminders[0]["delivery_state"] == "expired"
        assert "completed_at" in reminders[0]

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_overdue_1h_still_claimed(self, mock_read, mock_write):
        """一次性提醒 due 1 小时前 → 仍正常出现在 claimed（未超阈值）"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_1h_ago = (fake_now - dt.timedelta(hours=1)).isoformat()
        reminders = [_make_reminder(2, "近期提醒", due_1h_ago, "", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["done"] is False
        assert claimed[0]["delivery_state"] == "delivering"

    @patch("app.reminders._write_locked_reminders")
    @patch("app.reminders._read_locked_reminders")
    def test_repeat_reminder_still_claimed_when_overdue(self, mock_read, mock_write):
        """循环提醒即使过期很久仍正常出现在 claimed"""
        from app.reminders import claim_due_reminders

        fake_now = dt.datetime(2026, 8, 27, 12, 0, 0)
        due_3d_ago = (fake_now - dt.timedelta(days=3)).isoformat()
        reminders = [_make_reminder(3, "每日喝水", due_3d_ago, "daily", done=False)]
        mock_read.return_value = reminders

        import app.reminders as rem
        orig_dt, fake_dt = _freeze_dt(rem, fake_now)
        try:
            claimed = claim_due_reminders()
        finally:
            rem.dt = orig_dt

        assert len(claimed) == 1
        assert claimed[0]["done"] is False
        assert claimed[0]["delivery_state"] == "delivering"


# ======================================================================
# Bug 2: feishu webhook must have hmac imported
# ======================================================================

class TestFeishuWebhookHmac:
    """飞书 webhook 正确处理 verification_token，不因缺少 hmac import 报 500"""

    @patch("app.routes.feishu.FEISHU_VERIFICATION_TOKEN", "secret-token")
    def test_correct_token_returns_normal_response(self):
        """token 正确时返回正常业务响应（非 500）"""
        from app.routes.feishu import feishu_webhook
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={
            "type": "event_callback",
            "token": "secret-token",
            "event": {
                "msg_type": "text",
                "message": {"content": json.dumps({"text": "你好"}), "chat_id": "chat-1"},
                "sender": {"sender_id": {"open_id": "user-1"}},
            },
        })

        # mock brain 调用，避免真实 LLM 请求
        with patch("app.routes.feishu.asyncio.create_task") as mock_create:
            mock_create.return_value = None
            with patch("app.routes.feishu._keep_task"):
                # 直接调用 handler，不应因缺少 hmac import 而 NameError
                raised = None
                try:
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(feishu_webhook(mock_request))
                except Exception as e:
                    raised = e
                finally:
                    loop.close()

                assert raised is None, f"未期待异常: {raised}"
                assert result is not None
                assert result.get("code") == 0

    @patch("app.routes.feishu.FEISHU_VERIFICATION_TOKEN", "secret-token")
    def test_wrong_token_returns_403(self):
        """token 错误时返回 403，不 NameError"""
        from app.routes.feishu import feishu_webhook
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={
            "type": "event_callback",
            "token": "wrong-token",
            "event": {},
        })

        raised = None
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(feishu_webhook(mock_request))
        except HTTPException as e:
            raised = e
        except Exception as e:
            raised = e
        finally:
            loop.close()

        assert raised is not None
        # 应为 403，而非因 NameError 导致的 500
        assert getattr(raised, "status_code", None) == 403


# ======================================================================
# Bug 3: wardrobe classify_clothing must be awaited
# ======================================================================

class TestWardrobeClassifyClothingAwait:
    """classify_clothing 被 await，handler 返回正确字段而非 coroutine 错误"""

    @patch("app.routes.wardrobe_photo.classify_clothing", new_callable=AsyncMock)
    @patch("app.routes.wardrobe_photo.load_wardrobe", return_value=[])
    @patch("app.routes.wardrobe_photo.save_wardrobe")
    @patch("app.routes.wardrobe_photo.Image.open")
    def test_classify_clothing_is_awaited(self, mock_image_open, mock_save, mock_load, mock_classify):
        """handler 中 classify_clothing 被 await，返回 dict 字段而非 coroutine"""
        import voice_server

        # 构造最小图片文件（1x1 像素 PNG）
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        mock_img = MagicMock()
        mock_img.size = (100, 100)
        mock_img.convert.return_value = mock_img
        mock_image_open.return_value = mock_img

        mock_classify.return_value = {
            "name": "白色短袖T恤",
            "category": "上装",
            "color": "白色",
            "style": "休闲",
            "warmth": "薄",
        }

        client = TestClient(voice_server.app)
        response = client.post("/api/wardrobe/photo", files={"file": ("test.png", BytesIO(png_bytes), "image/png")})

        assert response.status_code == 200, f"响应: {response.text}"
        data = response.json()
        assert data["ok"] is True
        assert data["item"]["name"] == "白色短袖T恤"
        assert data["item"]["category"] == "上装"
        assert data["item"]["color"] == "白色"
