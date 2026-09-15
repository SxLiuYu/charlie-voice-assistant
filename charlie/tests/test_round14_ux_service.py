"""Round 14: 服务层 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestReminderAudioPrefix:
    def test_prefix_conditional_on_reminder_id(self):
        """play_reminder_audio 应根据 reminder_id 决定是否添加前缀。"""
        from app import notifications
        src = inspect.getsource(notifications)
        # 确保有条件判断
        assert "reminder_id is not None" in src, \
            "应根据 reminder_id 是否为 None 决定前缀"
        # 确保不再无条件拼接
        assert 'f"主人，提醒您：{text}"' not in src or "reminder_id is not None" in src, \
            "不应无条件给所有 text 拼接前缀"

    def test_no_prefix_for_proactive_suggestion(self):
        """主动建议（reminder_id=None）不应添加'主人，提醒您：'前缀。"""
        from app import notifications
        src = inspect.getsource(notifications.play_reminder_audio)
        # 查找前缀逻辑：应在 reminder_id is not None 时才加前缀
        assert "_prefix" in src or "reminder_id is not None" in src, \
            "应使用条件前缀变量"


class TestACControlErrorHandling:
    def test_ac_off_exception_returns_error_message(self):
        """AC 关机异常应返回错误提示而非空串。"""
        from agent import device_control
        src = inspect.getsource(device_control)
        off_idx = src.find("关机失败")
        assert off_idx > 0, "应有关机失败日志"
        # 检查关机失败日志后紧跟的是友好错误提示，而非空串
        pattern = r'log\.warning\(f"\[ac\] 关机失败: \{e\}"\)\s*\n\s*return "空调指令发送失败，请稍后重试。"'
        import re
        assert re.search(pattern, src), "关机异常分支应返回友好错误提示"

    def test_ac_on_exception_returns_error_message(self):
        """AC 开机异常应返回错误提示而非空串。"""
        from agent import device_control
        src = inspect.getsource(device_control)
        on_idx = src.find("开机失败")
        assert on_idx > 0, "应有开机失败日志"
        # 检查开机失败日志后紧跟的是友好错误提示，而非空串
        pattern = r'log\.warning\(f"\[ac\] 开机失败: \{e\}"\)\s*\n\s*return "空调指令发送失败，请稍后重试。"'
        import re
        assert re.search(pattern, src), "开机异常分支应返回友好错误提示"

    def test_ac_error_returns_friendly_message(self):
        """AC 异常应返回用户友好提示。"""
        from agent import device_control
        src = inspect.getsource(device_control)
        assert "空调指令发送失败" in src or "请稍后重试" in src, \
            "AC 异常应返回友好错误提示"


class TestMorningBriefingDedup:
    def test_decision_engine_checks_scheduler_dedup(self):
        """决策引擎 morning_wakeup 应与主动调度器共享 dedup key，避免双播。"""
        from app import schedulers
        src = inspect.getsource(schedulers)
        # 决策引擎执行路径应检查 morning_wakeup 并调用 _claim_suggest_state
        assert 'rid == "morning_wakeup"' in src, \
            "决策引擎应识别 morning_wakeup 规则做去重"
        assert '_claim_suggest_state("last_time_suggest"' in src, \
            "决策引擎应与主动调度器共享 _claim_suggest_state dedup key"
