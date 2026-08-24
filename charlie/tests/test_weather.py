"""Tests for app.weather: Open-Meteo → AMAP fallback, WMO code mapping, alerts."""
import json
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

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _om_geo_payload(results):
    return {"results": results}


def _om_forecast_payload(code, temp_max=30, temp_min=18, rain_prob=0, cur_temp=25, cur_code=0):
    return {
        "daily": {
            "weathercode": [code],
            "temperature_2m_max": [temp_max],
            "temperature_2m_min": [temp_min],
            "precipitation_probability_max": [rain_prob],
        },
        "current_weather": {
            "temperature": cur_temp,
            "weathercode": cur_code,
        },
    }


def _amap_payload(weather="晴", day_temp=30, night_temp=20):
    return {
        "forecasts": [
            {
                "casts": [
                    {
                        "dayweather": weather,
                        "nightweather": weather,
                        "daytemp": str(day_temp),
                        "nighttemp": str(night_temp),
                    }
                ]
            }
        ]
    }


# ======================================================================
# P0-1-a: Open-Meteo success
# ======================================================================

class TestWeatherOpenMeteoSuccess:
    @patch("app.weather.requests.get")
    def test_returns_open_meteo_source(self, mock_get):
        """Open-Meteo geocoding + forecast 成功 → source=open-meteo"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 32, 22, 0, 28, 0)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"
        assert w["day_weather"] == "晴"
        assert w["night_weather"] == "晴"
        assert w["day_temp"] == 32
        assert w["night_temp"] == 22
        assert w["current_temp"] == 28
        assert w["current_weather"] == "晴"
        assert w["rain_prob"] == 0

    @patch("app.weather.requests.get")
    def test_wmo_code_mapping(self, mock_get):
        """WMO code 映射：雨=61→小雨、雷阵雨=95→雷阵雨、未知码→未知"""
        from app.weather import get_weather

        cases = [
            (61, "小雨"),
            (65, "大雨"),
            (95, "雷阵雨"),
            (0, "晴"),
            (3, "阴"),
            (99, "雷阵雨"),
            (999, "未知"),
        ]
        for code, expected in cases:
            mock_get.side_effect = [
                FakeResp(_om_geo_payload([{"latitude": 1.0, "longitude": 2.0}])),
                FakeResp(_om_forecast_payload(code)),
            ]
            w = get_weather("未知城")
            assert w["weather_text"] == expected, f"code={code} expected {expected}"


# ======================================================================
# P0-1-b: Open-Meteo fail → AMAP fallback
# ======================================================================

class TestWeatherFallbackToAmap:
    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_fallback_to_amap_when_open_meteo_fails(self, mock_get):
        """Open-Meteo 失败 → AMAP → source=amap"""
        from app.weather import get_weather

        def side_effect(*args, **kwargs):
            url = args[0] if args else ""
            if "open-meteo" in url:
                raise Exception("OM down")
            return FakeResp(_amap_payload("多云", 28, 20))

        mock_get.side_effect = side_effect

        w = get_weather("北京")
        assert w["source"] == "amap"
        assert w["day_weather"] == "多云"
        assert w["day_temp"] == 28

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_open_meteo_no_results_falls_back(self, mock_get):
        """Open-Meteo geocoding 找不到城市 → fallback AMAP"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp({"results": []}),           # OM 中文找不到
            FakeResp({"results": []}),           # OM 英文拼音找不到
            FakeResp({"geocodes": [{"adcode": "310000"}]}),  # AMAP geocode
            FakeResp(_amap_payload("小雨", 25, 18)),          # AMAP weather
        ]

        w = get_weather("不存在城")
        assert w["source"] == "amap"


# ======================================================================
# P0-1-c: Both fail → default
# ======================================================================

class TestWeatherBothFail:
    @patch("app.weather.requests.get", side_effect=Exception("network down"))
    def test_returns_default_when_both_fail(self, _mock_get):
        """Open-Meteo + AMAP 都失败 → 返回 default 天气"""
        from app.weather import get_weather

        w = get_weather("任何城市")
        assert w["source"] == "default"
        assert w["day_weather"] == "晴"
        assert w["night_weather"] == "晴"
        assert w["day_temp"] == 20
        assert w["night_temp"] == 20

    @patch.dict(os.environ, {}, clear=True)
    @patch("app.weather.requests.get")
    def test_amap_not_configured_then_open_meteo_fails(self, mock_get):
        """AMAP 未配置(AMAP_KEY 为空) + Open-Meteo 失败 → default"""
        # AMAP_KEY 已由 patch.dict 清除; _amap_get 直接返回 None
        mock_get.side_effect = Exception("OM down")

        from app.weather import get_weather
        w = get_weather("北京")
        assert w["source"] == "default"


# ======================================================================
# P0-1-d: WMO mapping unit tests
# ======================================================================

class TestWmoMapping:
    def test_wmo_to_text_known_codes(self):
        from app.weather import _wmo_to_text
        assert _wmo_to_text(0) == "晴"
        assert _wmo_to_text(2) == "多云"
        assert _wmo_to_text(61) == "小雨"
        assert _wmo_to_text(65) == "大雨"
        assert _wmo_to_text(95) == "雷阵雨"
        assert _wmo_to_text(45) == "雾"

    def test_wmo_to_text_unknown(self):
        from app.weather import _wmo_to_text
        assert _wmo_to_text(999) == "未知"


# ======================================================================
# P0-1-e: get_weather_alerts
# ======================================================================

class TestWeatherAlerts:
    @patch("app.weather.get_weather")
    def test_high_temperature_alert(self, mock_get_weather):
        """day_temp >= 35 → 高温预警"""
        from app.weather import get_weather_alerts

        mock_get_weather.return_value = {
            "day_temp": 38, "night_temp": 28,
            "weather_text": "晴", "rain_prob": 0,
        }
        alerts = get_weather_alerts("北京")
        assert any("高温预警" in a for a in alerts)

    @patch("app.weather.get_weather")
    def test_cold_wave_alert(self, mock_get_weather):
        """day_temp <= 0 → 寒潮预警"""
        from app.weather import get_weather_alerts

        mock_get_weather.return_value = {
            "day_temp": -2, "night_temp": -8,
            "weather_text": "晴", "rain_prob": 0,
        }
        alerts = get_weather_alerts("北京")
        assert any("寒潮预警" in a for a in alerts)

    @patch("app.weather.get_weather")
    def test_heavy_rain_alert(self, mock_get_weather):
        """weather_text 含"暴雨" → 暴雨预警"""
        from app.weather import get_weather_alerts

        mock_get_weather.return_value = {
            "day_temp": 22, "night_temp": 18,
            "weather_text": "暴雨", "rain_prob": 90,
        }
        alerts = get_weather_alerts("北京")
        assert any("暴雨预警" in a for a in alerts)

    @patch("app.weather.get_weather")
    def test_no_alerts_for_mild_weather(self, mock_get_weather):
        """温和天气 → 无预警"""
        from app.weather import get_weather_alerts

        mock_get_weather.return_value = {
            "day_temp": 25, "night_temp": 18,
            "weather_text": "多云", "rain_prob": 10,
        }
        alerts = get_weather_alerts("北京")
        assert alerts == []

    @patch("app.weather.get_weather")
    def test_rain_prob_high_triggers_alert(self, mock_get_weather):
        """rain_prob >= 80 → 降雨概率提醒"""
        from app.weather import get_weather_alerts

        mock_get_weather.return_value = {
            "day_temp": 25, "night_temp": 18,
            "weather_text": "阴", "rain_prob": 85,
        }
        alerts = get_weather_alerts("北京")
        assert any("降雨概率" in a for a in alerts)
