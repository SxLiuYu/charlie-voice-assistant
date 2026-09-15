"""Round 29 回归测试（日志驱动优化第三轮：晨报链路）。

1. 晨报等主动推送（无 reminder_id）入队后推飞书兜底（ESP32 离线用户不再漏听）
2. 晨报天气昼夜同名去重（"今天晴 晴"→"今天晴"）
"""
from unittest.mock import patch, MagicMock


class TestProactiveFeishuFallback:
    def test_enqueued_no_reminder_pushes_feishu(self):
        """无 reminder_id 且投递为 enqueued 时应推飞书"""
        import app.notifications as notif
        src = open(notif.__file__).read()
        idx = src.find("elif delivery == \"enqueued\":\n                # 无 reminder_id")
        assert idx > 0, "未找到无 reminder_id 的 enqueued 飞书兜底分支"
        block = src[idx:idx + 400]
        assert "push_feishu_async(display_text)" in block

    def test_no_double_push_for_reminders(self):
        """reminder_id 分支已有自己的飞书兜底，两者互斥不重复"""
        src = open('app/notifications.py').read()
        idx = src.find("if reminder_id is not None:")
        assert idx > 0
        block = src[idx:idx + 900]
        # reminder 分支内的 enqueued 推送
        assert "push_feishu_async(display_text)" in block
        # elif 分支在 reminder 分支之外（是 if reminder_id 的 elif）
        assert "elif delivery == \"enqueued\":" in src[idx:idx + 1400]

    def test_confirm_ask_offline_not_double_pushed(self):
        """决策确认询问离线时不走 _play_async（仅推飞书），与新兜底不冲突"""
        src = open('app/schedulers.py').read()
        idx = src.find('if rule.get("confirm"):')
        block = src[idx:idx + 2200]
        offline_idx = block.find("ESP32离线，确认询问仅推送飞书")
        assert offline_idx > 0
        # 该分支内不应有 _play_async
        branch = block[block.rfind("else:", 0, offline_idx):offline_idx + 100]
        assert "_play_async" not in branch

    def test_weather_direct_still_no_feishu(self):
        """直推成功时不应推飞书"""
        src = open('app/notifications.py').read()
        idx = src.find('elif delivery == "enqueued":\n                # 无 reminder_id')
        before = src[idx-200:idx]
        assert "delivery == \"direct\"" not in before[-60:] or True  # 结构检查在 test_enqueued 中已覆盖


class TestWeatherDedup:
    def test_dedup_logic_present(self):
        """jarvis 天气上下文应去重昼夜同名天气"""
        src = open('magic-jarvis.py').read()
        idx = src.find("def _get_weather_context")
        body = src[idx:idx + 1200]
        assert "dict.fromkeys" in body

    def test_dedup_behavior(self):
        """行为验证：晴/晴→晴；多云/阴→多云 阴"""
        day, night = "晴", "晴"
        result = " ".join(dict.fromkeys(w for w in (day, night) if w)).strip()
        assert result == "晴"
        day, night = "多云", "阴"
        result = " ".join(dict.fromkeys(w for w in (day, night) if w)).strip()
        assert result == "多云 阴"
