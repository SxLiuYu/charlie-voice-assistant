"""Round 15: 推送链接+静音保护 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestPushClickableLinks:
    def test_links_use_feishu_post_format(self):
        """推送应使用飞书 post 富文本（tag=a 可点击链接），而非裸 URL。"""
        import personalized_push
        src = inspect.getsource(personalized_push)
        # 应使用飞书 post 富文本 tag=a 格式
        assert '"tag": "a"' in src or "'tag': 'a'" in src or '"tag":"a"' in src, \
            "应使用飞书 post 富文本 tag=a 可点击链接格式"
        # 应使用 post 消息类型
        assert '"msg_type": "post"' in src or "'msg_type': 'post'" in src or 'msg_type": "post"' in src, \
            "应使用 post 消息类型（text 类型不支持可点击链接）"
        # 不应再使用裸 URL 换行格式
        assert 'f"{i}. {word}\\n   {url}"' not in src, \
            "不应再使用裸 URL + 换行格式"


class TestPushQuietHours:
    def test_quiet_hours_function_exists(self):
        """应有推送静音时段检查函数。"""
        import personalized_push
        assert hasattr(personalized_push, "_is_push_quiet_hours"), \
            "应存在 _is_push_quiet_hours 函数"

    def test_quiet_hours_default_22_to_7(self):
        """默认静音时段应为 22:00-07:00。"""
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PERSONALIZED_PUSH_QUIET_START", None)
            os.environ.pop("PERSONALIZED_PUSH_QUIET_END", None)
            import personalized_push
            with patch("personalized_push.datetime") as mock_dt:
                # 23:00 → 应静音
                mock_dt.datetime.now.return_value.hour = 23
                assert personalized_push._is_push_quiet_hours() == True
                # 12:00 → 不静音
                mock_dt.datetime.now.return_value.hour = 12
                assert personalized_push._is_push_quiet_hours() == False

    def test_loop_checks_quiet_hours(self):
        """推送循环应检查静音时段。"""
        import personalized_push
        src = inspect.getsource(personalized_push.personalized_push_loop)
        assert "_is_push_quiet_hours" in src, \
            "推送循环应调用 _is_push_quiet_hours 检查"
