"""Round 22: 提醒 ESP32 离线推飞书兜底 + 早安简报飞书日历集成 回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestReminderFeishuFallback:
    """P1-A: 提醒在 ESP32 离线（enqueued）时应推飞书兜底。"""

    def test_enqueued_pushes_feishu(self):
        """play_reminder_audio 在 delivery=='enqueued' 时应调用 push_feishu_async。"""
        notif_path = os.path.join(PROJECT_DIR, "app", "notifications.py")
        src = open(notif_path).read()
        # 定位 enqueued 分支
        enqueued_idx = src.find('delivery == "enqueued"')
        assert enqueued_idx > 0, "应存在 enqueued 分支"
        block = src[enqueued_idx:enqueued_idx + 300]
        assert "push_feishu_async" in block, \
            "ESP32 离线（enqueued）时应调用 push_feishu_async 推飞书兜底"

    def test_direct_does_not_push_feishu(self):
        """直推成功（direct）时不应额外推飞书（避免重复通知）。"""
        notif_path = os.path.join(PROJECT_DIR, "app", "notifications.py")
        src = open(notif_path).read()
        # 定位 direct 分支
        direct_idx = src.find('delivery == "direct"')
        assert direct_idx > 0
        # direct 分支到下一个 elif 之间不应有 push_feishu_async
        block = src[direct_idx:direct_idx + 200]
        elif_idx = block.find("elif")
        if elif_idx > 0:
            direct_block = block[:elif_idx]
        else:
            direct_block = block
        assert "push_feishu_async" not in direct_block, \
            "直推成功时不应额外推飞书（ESP32 在线已收到提醒）"

    def test_feishu_fallback_logged(self):
        """飞书兜底推送应有日志记录。"""
        notif_path = os.path.join(PROJECT_DIR, "app", "notifications.py")
        src = open(notif_path).read()
        enqueued_idx = src.find('delivery == "enqueued"')
        block = src[enqueued_idx:enqueued_idx + 300]
        assert "飞书" in block or "feishu" in block.lower(), \
            "飞书兜底推送应记录日志说明原因"


class TestBriefingCalendarIntegration:
    """P1-B: 早安简报应接入飞书日历获取今日日程。"""

    def test_agenda_not_empty_stub(self):
        """_get_today_agenda 不应再是空 stub。"""
        briefing_path = os.path.join(PROJECT_DIR, "app", "briefing.py")
        src = open(briefing_path).read()
        func_idx = src.find("def _get_today_agenda")
        assert func_idx > 0
        func_body = src[func_idx:func_idx + 800]
        # 不应再是 "return """ 的空 stub
        assert 'return ""' not in func_body[:100], \
            "_get_today_agenda 不应再是空 stub"
        # 不应再有"集成已断开"的注释
        assert "断开" not in func_body, \
            "不应再有'集成已断开'的注释"

    def test_agenda_calls_feishu_api(self):
        """_get_today_agenda 应调用飞书日历 API。"""
        briefing_path = os.path.join(PROJECT_DIR, "app", "briefing.py")
        src = open(briefing_path).read()
        func_idx = src.find("def _get_today_agenda")
        func_body = src[func_idx:func_idx + 1500]
        assert "calendar" in func_body.lower(), \
            "应调用飞书日历 API"
        assert "get_tenant_access_token" in func_body, \
            "应通过 feishu_token 获取 access token"
        assert "events" in func_body, \
            "应获取日历事件列表"

    def test_agenda_handles_failure_gracefully(self):
        """日历获取失败时应返回空字符串，不崩溃。"""
        briefing_path = os.path.join(PROJECT_DIR, "app", "briefing.py")
        src = open(briefing_path).read()
        func_idx = src.find("def _get_today_agenda")
        func_body = src[func_idx:func_idx + 1800]
        assert "except Exception" in func_body, \
            "应有异常处理"
        assert 'return ""' in func_body, \
            "异常时应返回空字符串"

    def test_agenda_formats_output(self):
        """日程输出应格式化为用户友好的文本。"""
        briefing_path = os.path.join(PROJECT_DIR, "app", "briefing.py")
        src = open(briefing_path).read()
        func_idx = src.find("def _get_today_agenda")
        func_body = src[func_idx:func_idx + 1500]
        assert "日程" in func_body, \
            "输出应包含'日程'关键字"
        # 应限制返回数量（最多 5 条）
        assert "[:5]" in func_body or "[:10]" in func_body, \
            "应限制返回的日程数量"

    def test_briefing_includes_agenda(self):
        """morning_briefing 应调用 _get_today_agenda。"""
        briefing_path = os.path.join(PROJECT_DIR, "app", "briefing.py")
        src = open(briefing_path).read()
        # morning_briefing 函数应调用 _get_today_agenda
        morning_idx = src.find("def morning_briefing")
        if morning_idx > 0:
            morning_body = src[morning_idx:morning_idx + 1000]
            assert "_get_today_agenda" in morning_body, \
                "morning_briefing 应调用 _get_today_agenda"
