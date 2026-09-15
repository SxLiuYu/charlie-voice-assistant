"""Round 30 回归测试（日志驱动优化第四轮：晚间复盘文案 + auto_trigger 去重）。

1. 晚间复盘"明天北京今天小雨"→ 直接取明日预报（时间参照不再混乱）
2. "。，"标点连用修复
3. auto_trigger 每日持久化去重（重启后不再重复推送睡眠提醒）
"""
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta


class TestEveningWrapupWeather:
    def test_no_today_weather_text_for_tomorrow(self):
        """晚间复盘不应再把 get_weather_text(detail) 冠以明天"""
        src = open('app/briefing.py').read()
        idx = src.find("def evening_wrapup")
        body = src[idx:idx + 2500]
        assert 'parts.append(f"明天{weather}")' not in body
        assert "_forecast_for_date" in body

    def test_uses_tomorrow_forecast(self):
        """应使用明日预报的 dayweather/daytemp"""
        src = open('app/briefing.py').read()
        idx = src.find("def evening_wrapup")
        body = src[idx:idx + 2500]
        assert 'dayweather' in body
        assert 'daytemp' in body
        assert '明天{_w}' in body or '明天' + '{' in body

    def test_evening_output_clean(self, tmp_path):
        """端到端：mock 明日预报，输出应为'明天小雨，最高20度'式干净文本"""
        import app.briefing as br
        fake_cast = {"dayweather": "小雨", "nightweather": "阴",
                     "daytemp": "20", "nighttemp": "15", "date": None}
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        fake_cast["date"] = tomorrow
        with patch("app.schedulers._get_weather", return_value=[fake_cast]), \
             patch("app.reminders.list_reminders", return_value=[]), \
             patch("app.briefing._get_weather_text", side_effect=AssertionError("不应再调用今日天气文本")):
            text = br.evening_wrapup()
        assert "明天小雨" in text
        # "明天"不应再与"今天"的天气信息混搭（"明天北京今天小雨"回归）
        assert "明天北京今天" not in text
        assert "。，" not in text

    def test_punctuation_stripped(self):
        """各段句末句号应剥离，不再出现'。，'"""
        src = open('app/briefing.py').read()
        idx = src.find("def evening_wrapup")
        body = src[idx:idx + 2500]
        assert 'rstrip("。")' in body


class TestAutoTriggerDedup:
    def test_persistent_claim_present(self):
        """auto_trigger 应有每日持久化 claim（重启安全）"""
        src = open('app/schedulers.py').read()
        idx = src.find("check_auto_triggers(state)")
        block = src[idx:idx + 1200]
        assert "_claim_suggest_state(f\"last_auto_trigger_{pkey}\"" in block

    def test_claim_value_includes_state(self):
        """claim 值应含状态（同一天不同状态可各提示一次）"""
        src = open('app/schedulers.py').read()
        idx = src.find("last_auto_trigger_{pkey}")
        line = src[idx:idx + 60]
        assert 'today + "_" + state' in line

    def test_in_memory_cooldown_kept(self):
        """1 小时内存冷却保留（同一状态持续满足时不依赖 claim 频繁写盘）"""
        src = open('app/schedulers.py').read()
        idx = src.find("check_auto_triggers(state)")
        block = src[idx:idx + 600]
        assert "_AUTO_TRIGGER_COOLDOWN" in block
