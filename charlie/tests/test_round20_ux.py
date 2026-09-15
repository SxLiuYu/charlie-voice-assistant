"""Round 20: news import 修复 + 天气降级 + 飞书流式中断兜底 + 长消息分段 回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestNewsImportRequests:
    """P0: app/news.py 必须导入 requests，否则新闻功能完全失效。"""

    def test_requests_imported(self):
        """news.py 顶部应导入 requests。"""
        news_path = os.path.join(PROJECT_DIR, "app", "news.py")
        src = open(news_path).read()
        # 检查顶部导入区域（前 15 行）
        import_section = "\n".join(src.split("\n")[:15])
        assert "import requests" in import_section, \
            "app/news.py 必须在顶部导入 requests（fetch_feed 使用 requests.get）"

    def test_fetch_feed_uses_requests(self):
        """fetch_feed 函数体中使用 requests.get。"""
        news_path = os.path.join(PROJECT_DIR, "app", "news.py")
        src = open(news_path).read()
        assert "requests.get" in src, \
            "fetch_feed 应使用 requests.get 拉取 RSS 源"

    def test_no_inline_import_requests(self):
        """不应在函数内部 import requests（应在顶部导入避免 NameError）。"""
        news_path = os.path.join(PROJECT_DIR, "app", "news.py")
        src = open(news_path).read()
        # fetch_feed 函数内部不应有 import requests
        func_idx = src.find("def fetch_feed")
        if func_idx > 0:
            func_body = src[func_idx:func_idx + 500]
            assert "import requests" not in func_body, \
                "requests 应在模块顶部导入，不应在函数内部导入"


class TestWeatherDefaultFallback:
    """P1: 天气双 API 失败时不应返回虚假"晴天"数据。"""

    def test_default_source_returns_unavailable(self):
        """get_weather_text 在 source='default' 时应返回不可用提示。"""
        from app.weather import get_weather_text
        # mock get_weather 返回 default 数据
        from unittest.mock import patch
        default_data = {"city": "北京", "day_weather": "晴", "night_weather": "晴",
                        "day_temp": 20, "night_temp": 20, "weather_text": "晴",
                        "source": "default"}
        with patch("app.weather.get_weather", return_value=default_data):
            result = get_weather_text("北京")
        assert "不可用" in result or "稍后" in result, \
            f"source=default 时应返回不可用提示，实际: {result}"
        assert "晴" not in result or "不可用" in result, \
            "不应返回虚假的'晴天'信息"

    def test_normal_weather_still_works(self):
        """正常天气数据不受影响。"""
        from app.weather import get_weather_text
        from unittest.mock import patch
        normal_data = {"city": "北京", "day_weather": "多云", "night_weather": "阴",
                       "day_temp": 25, "night_temp": 15, "weather_text": "多云",
                       "source": "amap"}
        with patch("app.weather.get_weather", return_value=normal_data):
            result = get_weather_text("北京")
        assert "多云" in result
        assert "15" in result and "25" in result


class TestFeishuStreamInterruptFallback:
    """P1: 飞书流式回复中途失败时应发送中断提示。"""

    def test_interrupt_sends_notice_when_streamed(self):
        """_streamed=True 时 brain 异常应发送中断提示。"""
        ws_path = os.path.join(PROJECT_DIR, "feishu_ws.py")
        src = open(ws_path).read()
        # 定位 brain 异常处理
        brain_err_idx = src.find("brain 调用失败")
        assert brain_err_idx > 0
        err_block = src[brain_err_idx:brain_err_idx + 300]
        assert "_streamed" in err_block, \
            "brain 异常时应检查 _streamed 状态"
        assert "中断" in err_block or "重试" in err_block, \
            "已流式发送后 brain 异常应发送中断提示"

    def test_non_streamed_uses_normal_fallback(self):
        """_streamed=False 时 brain 异常走正常兜底文案。"""
        ws_path = os.path.join(PROJECT_DIR, "feishu_ws.py")
        src = open(ws_path).read()
        # 兜底文案仍存在
        assert "抱歉，我暂时无法回复" in src, \
            "非流式路径应保留原有兜底文案"


class TestFeishuLongMessageSplit:
    """P1: 飞书长消息应自动分段发送。"""

    def test_split_function_exists(self):
        """应存在长文本分段函数。"""
        ws_path = os.path.join(PROJECT_DIR, "feishu_ws.py")
        src = open(ws_path).read()
        assert "_split_long_text" in src, \
            "应存在 _split_long_text 函数处理超长消息分段"

    def test_send_text_reply_checks_length(self):
        """_send_text_reply 应检查消息长度并分段。"""
        ws_path = os.path.join(PROJECT_DIR, "feishu_ws.py")
        src = open(ws_path).read()
        func_idx = src.find("def _send_text_reply")
        func_body = src[func_idx:func_idx + 600]
        assert "3500" in func_body or "FEISHU_MSG_MAX" in func_body, \
            "应设置消息长度上限（约 3500 字符）并在超限时分段"

    def test_split_long_text_logic(self):
        """_split_long_text 应按句号分割且每段不超过上限。"""
        from feishu_ws import _split_long_text
        # 构造超长文本（多个句号分隔的句子）
        long_text = "这是一段很长的文本。" * 500  # ~5000 字符
        segments = _split_long_text(long_text, 3500)
        assert len(segments) >= 2, \
            f"超长文本应分成多段，实际 {len(segments)} 段"
        for seg in segments:
            assert len(seg) <= 3500, \
                f"每段不应超过 3500 字符，实际 {len(seg)}"

    def test_short_text_not_split(self):
        """短文本不应被分段。"""
        from feishu_ws import _split_long_text
        short_text = "这是短消息。"
        segments = _split_long_text(short_text, 3500)
        assert len(segments) == 1, \
            f"短文本不应分段，实际 {len(segments)} 段"
