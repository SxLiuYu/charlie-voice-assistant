"""Round 13: 交互体验回归测试。

- 天气模板短答/完整版/实时预报矛盾处理
- barge-in 风暴守卫（结构回归）
- 空回复罐头应答与音乐失败兜底（结构回归）
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = __import__("json").dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _amap_payload(weather="小雨", day_temp=23, night_temp=19,
                  cur_weather=None):
    """AMAP weatherInfo 响应；cur_weather 可注入实时字段矛盾。"""
    casts = [{
        "dayweather": weather, "nightweather": weather,
        "daytemp": str(day_temp), "nighttemp": str(night_temp),
    }]
    if cur_weather is not None:
        casts[0]["dayweather"] = weather
    payload = {"status": "1", "forecasts": [{"casts": casts}]}
    if cur_weather is not None:
        # AMAP 实时字段（lives）由 _amap_get 的 extensions=all 返回结构决定；
        # 这里通过 open-meteo 风格不适用，直接构造 lives 节点
        payload["lives"] = [{"weather": cur_weather, "temperature": "22"}]
    return payload


class TestWeatherTextShortAnswer:
    @patch.dict(os.environ, {"AMAP_KEY": ""})
    @patch("app.weather.requests.get")
    def test_short_answer_excludes_clothing(self, mock_get):
        """默认短答：不含穿衣建议，长度受限（语音 ~3s 内）。"""
        from app.weather import get_weather_text
        mock_get.side_effect = [
            _FakeResp({"results": [{"latitude": 39.9, "longitude": 116.4}]}),
            _FakeResp({"daily": {"weathercode": [61],
                                 "temperature_2m_max": [23],
                                 "temperature_2m_min": [19],
                                 "precipitation_probability_max": [60]},
                       "current_weather": {"temperature": 21,
                                           "weathercode": 3}}),
        ]
        text = get_weather_text("未知城")
        assert "穿" not in text, f"短答不应含穿衣建议: {text}"
        assert "小雨" in text and "19到23度" in text
        assert len(text) <= 40, f"短答应 ≤40 字，实际 {len(text)}: {text}"

    @patch.dict(os.environ, {"AMAP_KEY": ""})
    @patch("app.weather.requests.get")
    def test_detail_includes_clothing(self, mock_get):
        """detail=True：完整版含穿衣建议（晨报/飞书场景）。"""
        from app.weather import get_weather_text
        mock_get.side_effect = [
            _FakeResp({"results": [{"latitude": 39.9, "longitude": 116.4}]}),
            _FakeResp({"daily": {"weathercode": [61],
                                 "temperature_2m_max": [23],
                                 "temperature_2m_min": [19],
                                 "precipitation_probability_max": [60]},
                       "current_weather": {"temperature": 21,
                                           "weathercode": 3}}),
        ]
        text = get_weather_text("未知城", detail=True)
        assert "穿" in text or "带伞" in text


class TestWeatherConflictAnnotation:
    """实时与预报矛盾：预报有雨雪、实时无雨雪 → 实时标注"可能未更新"。"""

    @patch.dict(os.environ, {"AMAP_KEY": ""})
    @patch("app.weather.requests.get")
    def test_forecast_rain_current_clear_annotated(self, mock_get):
        """预报小雨 + 实时阴 → 实时字段标注可能未更新（06:07 用户纠正案例）。"""
        from app.weather import get_weather_text
        mock_get.side_effect = [
            _FakeResp({"results": [{"latitude": 39.9, "longitude": 116.4}]}),
            _FakeResp({"daily": {"weathercode": [61],
                                 "temperature_2m_max": [23],
                                 "temperature_2m_min": [19],
                                 "precipitation_probability_max": [70]},
                       "current_weather": {"temperature": 22,
                                           "weathercode": 3}}),  # 阴
        ]
        text = get_weather_text("未知城", detail=True)
        assert "可能未更新" in text, f"雨/阴矛盾未标注: {text}"

    @patch.dict(os.environ, {"AMAP_KEY": ""})
    @patch("app.weather.requests.get")
    def test_consistent_no_annotation(self, mock_get):
        """预报与实时一致 → 不加标注。"""
        from app.weather import get_weather_text
        mock_get.side_effect = [
            _FakeResp({"results": [{"latitude": 39.9, "longitude": 116.4}]}),
            _FakeResp({"daily": {"weathercode": [61],
                                 "temperature_2m_max": [23],
                                 "temperature_2m_min": [19],
                                 "precipitation_probability_max": [70]},
                       "current_weather": {"temperature": 22,
                                           "weathercode": 61}}),  # 小雨
        ]
        text = get_weather_text("未知城", detail=True)
        assert "可能未更新" not in text


class TestBargeInStormGuard:
    def test_storm_guard_source_present(self):
        """风暴守卫：10s 内 ≥3 次 barge-in 且无有效语音 → goodbye（结构回归）。"""
        import inspect
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        assert "_barge_times" in src, "缺少 barge-in 风暴计数"
        assert "背景音干扰" in src, "缺少风暴 goodbye 逻辑"
        assert "_barge_times.clear()  # 有效语音" in src, \
            "有效 ASR 应重置风暴计数"

    def test_sentinel_canned_ack_source_present(self):
        """空回复（sentinel 零句）→ 播罐头"嗯？"避免纯沉默（结构回归）。"""
        import inspect
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        assert "_play_canned_ack" in src
        assert "嗯？" in src

    def test_music_failure_fallback_source_present(self):
        """音乐下载失败 → TTS 兜底而非静默 continue（结构回归）。"""
        import inspect
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        assert "抱歉，音乐加载失败" in src

    def test_canned_prewarm_source_present(self):
        """罐头音应在 prewarm 中预热（进 TTS 缓存，零延迟应答）。"""
        import inspect
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        assert '_synth_mp3(_canned)' in src, "prewarm 缺少罐头音预热"


class TestBriefingUsesDetail:
    def test_briefing_requests_detail(self):
        """晨报是文本场景，应取完整版天气。"""
        import inspect
        from app import briefing
        src = inspect.getsource(briefing)
        assert "detail=True" in src
