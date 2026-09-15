"""Tests for AMAP quota circuit-breaker in app.weather."""
import json
import os
import sys
import time
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _om_geo_payload(results):
    return {"results": results}


def _om_forecast_payload(code=0, temp_max=30, temp_min=18, rain_prob=0,
                         cur_temp=25, cur_code=0):
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


def _amap_quota_payload(infocode="10044", info="USER_DAILY_QUERY_OVER_LIMIT"):
    return {"status": "0", "infocode": infocode, "info": info}


# ---------------------------------------------------------------------------
# Fixture: reset module-level circuit breaker state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_amap_block():
    """Reset the module-level circuit breaker before EACH test."""
    import importlib
    import app.weather as weather_mod
    weather_mod._amap_blocked_until = 0.0
    # Also re-import to pick up any env var changes for _AMAP_QUOTA_BACKOFF
    importlib.reload(weather_mod)
    yield
    weather_mod._amap_blocked_until = 0.0


# ===================================================================
# P0: quota overrun sets _amap_blocked_until and falls back to OM
# ===================================================================

class TestAmapQuotaCircuitBreaker:

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_infocode_10044_triggers_block_and_fallback(self, mock_get):
        """AMAP infocode 10044 → blocked + Open-Meteo fallback."""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp(_amap_quota_payload("10044", "USER_DAILY_QUERY_OVER_LIMIT")),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 32, 22)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"

        import app.weather as weather_mod
        assert weather_mod._amap_blocked_until > time.time()

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_infocode_10021_triggers_block(self, mock_get):
        """infocode 10021 也应触发熔断。"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp(_amap_quota_payload("10021", "some limit error")),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_status_zero_over_limit_info_triggers_block(self, mock_get):
        """status==0 且 info 含 OVER_LIMIT → 触发熔断。"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp({"status": "0", "infocode": "0", "info": "OVER_LIMIT"}),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_status_zero_limit_keyword_triggers_block(self, mock_get):
        """status==0 且 info 含 LIMIT → 触发熔断。"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp({"status": "0", "infocode": "0", "info": "API LIMIT REACHED"}),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"


# ===================================================================
# P1: during backoff window, requests.get must not be called
# ===================================================================

class TestAmapBlockedNoRequests:

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_no_amap_requests_while_blocked(self, mock_get):
        """熔断期内 _amap_get 直接返回 None，requests.get 未被调用。"""
        from app.weather import _amap_get

        import app.weather as weather_mod
        weather_mod._amap_blocked_until = time.time() + 9999

        result = _amap_get("北京")
        assert result is None
        mock_get.assert_not_called()

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    def test_no_amap_requests_via_get_weather_while_blocked(self):
        """通过 get_weather 进入时，熔断期内同样不发 AMAP 请求。"""
        import app.weather as weather_mod
        weather_mod._amap_blocked_until = time.time() + 9999

        with patch("app.weather._open_meteo_get",
                   return_value={"city": "北京", "source": "open-meteo",
                                 "day_weather": "晴", "night_weather": "晴",
                                 "day_temp": 20, "night_temp": 20,
                                 "weather_text": "晴"}) as mock_om:
            from app.weather import get_weather
            w = get_weather("北京")
            assert w["source"] == "open-meteo"
            mock_om.assert_called_once_with("北京")


# ===================================================================
# P2: backoff expires → circuit resets, AMAP is retried
# ===================================================================

class TestAmapBlockExpires:

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_amap_retries_after_backoff(self, mock_get):
        """time.time() 前进超过 backoff 后，_amap_get 重新请求 AMAP。"""
        import app.weather as weather_mod
        from app.weather import _amap_get

        # Set a short backoff so we can advance time within test
        weather_mod._AMAP_QUOTA_BACKOFF = 1.0
        # Block until t=100
        weather_mod._amap_blocked_until = 100.0

        fake_times = [99.0, 101.0]  # first call blocked, second not
        call_idx = [0]

        def fake_time():
            t = fake_times[min(call_idx[0], len(fake_times) - 1)]
            call_idx[0] += 1
            return t

        with patch.object(weather_mod.time, "time", side_effect=fake_time):
            result = _amap_get("北京")
            assert result is None  # still blocked at t=99

            # Now _amap_blocked_until is still 100.0, time returns 101.0 next call
            mock_get.return_value = FakeResp({
                "status": "1", "infocode": "10000",
                "forecasts": [{"casts": [{"dayweather": "晴",
                                          "nightweather": "晴",
                                          "daytemp": "30",
                                          "nighttemp": "20"}]}]
            })
            result2 = _amap_get("北京")
            # time=101 > blocked_until=100, so request goes through
            assert mock_get.call_count >= 1


# ===================================================================
# P3: non-quota AMAP failures do NOT trigger long block
# ===================================================================

class TestAmapNonQuotaFailureNoBlock:

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_network_error_does_not_trigger_long_block(self, mock_get):
        """网络异常等非配额类失败 → 不触发长熔断。"""
        from app.weather import get_weather

        mock_get.side_effect = [
            Exception("ConnectionError"),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"

        import app.weather as weather_mod
        assert weather_mod._amap_blocked_until == 0.0

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"})
    @patch("app.weather.requests.get")
    def test_amap_non_quota_error_does_not_trigger_block(self, mock_get):
        """AMAP 返回非配额类 infocode → 不触发熔断。"""
        from app.weather import get_weather

        mock_get.side_effect = [
            FakeResp({"status": "0", "infocode": "500", "info": "internal error"}),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        w = get_weather("北京")
        assert w["source"] == "open-meteo"

        import app.weather as weather_mod
        assert weather_mod._amap_blocked_until == 0.0


# ===================================================================
# P4: env var AMAP_QUOTA_BACKOFF_HOURS is respected
# ===================================================================

class TestAmapBackoffEnvVar:

    @patch.dict(os.environ, {"AMAP_KEY": "test-amap-key",
                             "AMAP_QUOTA_BACKOFF_HOURS": "1"})
    @patch("app.weather.requests.get")
    def test_backoff_one_hour_from_env(self, mock_get):
        """AMAP_QUOTA_BACKOFF_HOURS=1 → 熔断窗口 1 小时（3600s）。"""
        from app.weather import get_weather
        import importlib
        import app.weather as weather_mod
        # reload to pick up env change
        importlib.reload(weather_mod)

        mock_get.side_effect = [
            FakeResp(_amap_quota_payload("10044")),
            FakeResp(_om_geo_payload([{"latitude": 39.9, "longitude": 116.4}])),
            FakeResp(_om_forecast_payload(0, 30, 20)),
        ]

        get_weather("北京")
        assert weather_mod._AMAP_QUOTA_BACKOFF == 3600.0
        assert weather_mod._amap_blocked_until > time.time()
        assert weather_mod._amap_blocked_until <= time.time() + 3601
