"""Round 15: 飞书确认+信息播报 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestFeishuConfirmationUserId:
    def test_set_pending_passes_user_id(self):
        """决策引擎 set_pending_confirmation 应传入 FEISHU_PUSH_OPEN_ID。"""
        from app import schedulers
        src = inspect.getsource(schedulers)
        assert "FEISHU_PUSH_OPEN_ID" in src, \
            "schedulers 应导入 FEISHU_PUSH_OPEN_ID 用于 user_id 匹配"
        assert "set_pending_confirmation(rid, msg, user_id=" in src, \
            "set_pending_confirmation 应传入 user_id 参数"


class TestFeishuWebhookChannel:
    def test_webhook_passes_feishu_channel(self):
        """飞书 webhook 路径应传 channel='feishu_text'。"""
        from app.routes import feishu
        src = inspect.getsource(feishu)
        assert 'channel="feishu_text"' in src, \
            "webhook 路径应传 channel='feishu_text' 避免被 TTS 清洗"


class TestOnThisDayBrief:
    def test_brief_mode_short(self):
        """brief=True 应只取2条，每条≤30字，总长度适合语音。"""
        from app.on_this_day import get_events_text
        from unittest.mock import patch
        mock_events = [
            {"year": "1969", "description": "Apollo 11 lands on the Moon, Neil Armstrong becomes first human"},
            {"year": "1945", "description": "World War II ends with Japanese surrender"},
            {"year": "1991", "description": "The World Wide Web is publicly available"},
        ]
        with patch("app.on_this_day.get_events", return_value=mock_events):
            text = get_events_text(month=7, day=20, brief=True)
        assert len(text) <= 110, f"brief 模式应 ≤110 字（含英文标注），实际 {len(text)}: {text}"
        assert "1969" in text
        assert "1945" in text
        # 不应包含第三条
        assert "1991" not in text

    def test_default_count_reduced(self):
        """默认 count 应从 5 减为 3，减少播报时长。"""
        from app.on_this_day import get_events_text
        import inspect as ins
        sig = ins.signature(get_events_text)
        count_default = sig.parameters.get("count").default
        assert count_default <= 3, f"默认 count 应 ≤3，实际 {count_default}"

    def test_brief_uses_spaces_not_newlines(self):
        """brief 模式应用空格分隔（语音友好），不用换行。"""
        from app.on_this_day import get_events_text
        from unittest.mock import patch
        mock_events = [
            {"year": "1969", "description": "Apollo 11 lands"},
            {"year": "1945", "description": "WWII ends"},
        ]
        with patch("app.on_this_day.get_events", return_value=mock_events):
            text = get_events_text(month=7, day=20, brief=True)
        assert "\n" not in text, "brief 模式不应包含换行符"
