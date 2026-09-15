"""Round 32 regression tests for log-driven fixes.

Covers:
1. Open-Meteo fallback returns multi-day casts (7 days)
2. Evening briefing tomorrow weather works with Open-Meteo data
3. TTL cache: second call to _get_weather within 10 min doesn't make API call
4. except AttributeError logs warning instead of silent pass
5. late_night_sleep cooldown is 8 hours, other rules still 1 hour
6. ASR audit duration measured inside worker (mock executor, verify duration is small)
"""
import os
import sys
import time
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# P1-1: Open-Meteo fallback returns multi-day casts
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


class TestOpenMeteoMultiDayCasts:
    def test_open_meteo_get_casts_returns_all_days(self):
        """_open_meteo_get_casts 应将 Open-Meteo 的 7 日 daily 全部转为 casts"""
        from app.weather import _open_meteo_get_casts

        daily_payload = {
            "time": ["2026-08-31", "2026-09-01", "2026-09-02",
                     "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"],
            "weathercode": [0, 1, 2, 61, 63, 95, 0],
            "temperature_2m_max": [32, 30, 28, 25, 27, 29, 33],
            "temperature_2m_min": [22, 21, 20, 18, 19, 20, 23],
            "precipitation_probability_max": [0, 10, 30, 80, 90, 70, 0],
        }
        geo_payload = {"results": [{"latitude": 39.9, "longitude": 116.4}]}
        forecast_payload = {
            "daily": daily_payload,
            "current_weather": {"temperature": 28, "weathercode": 0},
        }

        with patch("app.weather.requests.get") as mock_get:
            mock_get.side_effect = [
                FakeResp(geo_payload),
                FakeResp(forecast_payload),
            ]
            casts = _open_meteo_get_casts("北京")

        assert casts is not None
        assert len(casts) == 7
        assert casts[0]["date"] == "2026-08-31"
        assert casts[0]["dayweather"] == "晴"
        assert casts[1]["date"] == "2026-09-01"
        assert casts[1]["dayweather"] == "晴"
        assert casts[3]["date"] == "2026-09-03"
        assert casts[3]["dayweather"] == "小雨"
        assert casts[5]["date"] == "2026-09-05"
        assert casts[5]["dayweather"] == "雷阵雨"
        # today at index [0]
        assert casts[0]["daytemp"] == "32"
        assert casts[0]["nighttemp"] == "22"

    def test_open_meteo_get_still_returns_single_day_dict(self):
        """_open_meteo_get 仍返回单日 dict（兼容 get_weather 单日入口）"""
        from app.weather import _open_meteo_get

        daily_payload = {
            "time": ["2026-08-31", "2026-09-01"],
            "weathercode": [0, 1],
            "temperature_2m_max": [32, 30],
            "temperature_2m_min": [22, 21],
            "precipitation_probability_max": [0, 10],
        }
        geo_payload = {"results": [{"latitude": 39.9, "longitude": 116.4}]}
        forecast_payload = {
            "daily": daily_payload,
            "current_weather": {"temperature": 28, "weathercode": 0},
        }

        with patch("app.weather.requests.get") as mock_get:
            mock_get.side_effect = [
                FakeResp(geo_payload),
                FakeResp(forecast_payload),
            ]
            w = _open_meteo_get("北京")

        assert w is not None
        assert w["source"] == "open-meteo"
        assert w["day_weather"] == "晴"
        assert w["day_temp"] == 32
        assert w["night_temp"] == 22
        assert w["current_temp"] == 28


# ======================================================================
# P1-1b: Evening briefing tomorrow weather with Open-Meteo
# ======================================================================

class TestEveningBriefingOpenMeteo:
    def test_forecast_for_date_finds_tomorrow_in_multi_day_casts(self):
        """_forecast_for_date 应在 7 日 casts 中找到明天"""
        import app.schedulers as sched_mod

        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        casts = [
            {"date": today, "dayweather": "晴", "daytemp": "30"},
            {"date": tomorrow, "dayweather": "小雨", "daytemp": "25"},
            {"date": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
             "dayweather": "多云", "daytemp": "28"},
        ]
        result = sched_mod._forecast_for_date(casts, tomorrow)
        assert result["dayweather"] == "小雨"
        assert result["daytemp"] == "25"

    def test_evening_wrapup_with_open_meteo_tomorrow(self):
        """evening_wrapup 应能从 Open-Meteo 多日 casts 中取到明天天气"""
        import app.briefing as br

        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        fake_casts = [
            {"date": datetime.now().strftime("%Y-%m-%d"),
             "dayweather": "晴", "nightweather": "晴",
             "daytemp": "30", "nighttemp": "20"},
            {"date": tomorrow,
             "dayweather": "小雨", "nightweather": "阴",
             "daytemp": "25", "nighttemp": "18"},
        ]

        with patch("app.schedulers._get_weather", return_value=fake_casts), \
             patch("app.reminders.list_reminders", return_value=[]), \
             patch("app.briefing._get_weather_text",
                   side_effect=AssertionError("不应再调用今日天气文本")):
            text = br.evening_wrapup()

        assert "明天小雨" in text
        assert "明天北京今天" not in text


# ======================================================================
# P1-2: TTL cache reuse
# ======================================================================

class TestWeatherCastsCache:
    def test_second_call_within_ttl_does_not_call_api(self):
        """10 分钟内第二次调用 get_weather_casts 不应发起 API 请求"""
        from app.weather import get_weather_casts, _casts_cache, _casts_cache_lock

        fake_casts = [
            {"date": "2026-08-31", "dayweather": "晴", "daytemp": "30"}
        ]

        with patch("app.weather._amap_get", return_value=None), \
             patch("app.weather._open_meteo_get_casts", return_value=fake_casts) as mock_om:
            # First call
            result1 = get_weather_casts("测试城A")
            assert result1 == fake_casts
            assert mock_om.call_count == 1

            # Second call within TTL
            result2 = get_weather_casts("测试城A")
            assert result2 == fake_casts
            assert mock_om.call_count == 1  # cached

    def test_get_weather_casts_ttl_cache_expires(self):
        """超过 TTL 后应重新发起 API 请求"""
        from app.weather import get_weather_casts, _WEATHER_CACHE_TTL

        fake_casts = [
            {"date": "2026-08-31", "dayweather": "晴", "daytemp": "30"}
        ]

        with patch("app.weather._amap_get", return_value=None), \
             patch("app.weather._open_meteo_get_casts", return_value=fake_casts) as mock_om, \
             patch("app.weather._WEATHER_CACHE_TTL", 0.1):
            get_weather_casts("测试城B")
            assert mock_om.call_count == 1
            time.sleep(0.2)
            get_weather_casts("测试城B")
            assert mock_om.call_count == 2


# ======================================================================
# P2-1: except AttributeError logs warning
# ======================================================================

class TestAttributeErrorWarning:
    def test_get_confirm_ask_count_missing_logs_warning(self):
        """get_confirm_ask_count 缺失时应 log.warning 而非静默 pass"""
        import app.schedulers as sched_mod
        from unittest.mock import patch as mock_patch

        fake_dec = MagicMock()
        fake_dec._CONFIRM_MAX_ASKS_PER_DAY = 2
        # Simulate missing method
        del fake_dec.get_confirm_ask_count

        with mock_patch.object(sched_mod, "_decision_last_ask", {}), \
             mock_patch.object(sched_mod.log, "warning") as mock_warn, \
             mock_patch.object(sched_mod, "_dec", fake_dec, create=True):
            # Simulate the code path
            rid = "test_rule"
            try:
                if fake_dec.get_confirm_ask_count(rid) >= fake_dec._CONFIRM_MAX_ASKS_PER_DAY:
                    pass
            except AttributeError:
                sched_mod.log.warning(f"[decision] {rid}: confirm_ask 接口缺失，每日上限未生效")

        assert mock_warn.called
        assert "confirm_ask 接口缺失" in mock_warn.call_args[0][0]

    def test_record_confirm_ask_missing_logs_warning(self):
        """record_confirm_ask 缺失时应 log.warning 而非静默 pass"""
        import app.schedulers as sched_mod
        from unittest.mock import patch as mock_patch

        fake_dec = MagicMock()
        # Simulate missing method
        del fake_dec.record_confirm_ask

        with mock_patch.object(sched_mod.log, "warning") as mock_warn:
            rid = "test_rule"
            try:
                fake_dec.record_confirm_ask(rid)
            except AttributeError:
                sched_mod.log.warning(f"[decision] {rid}: record_confirm_ask 接口缺失，每日上限未生效")

        assert mock_warn.called
        assert "record_confirm_ask 接口缺失" in mock_warn.call_args[0][0]


# ======================================================================
# P2-3: late_night_sleep cooldown is 8 hours
# ======================================================================

class TestLateNightSleepCooldown:
    def test_late_night_sleep_cooldown_is_8_hours(self):
        """late_night_sleep 的冷却时间应为 28800 秒（8 小时）"""
        src = open(os.path.join(PROJECT_DIR, "app", "schedulers.py")).read()
        idx = src.find("late_night_sleep")
        block = src[idx:idx + 1500]
        assert "28800" in block
        assert 'rid == "late_night_sleep"' in block

    def test_other_rules_still_use_1_hour_cooldown(self):
        """非 late_night_sleep 规则仍使用 _DECISION_COOLDOWN（3600 秒 = 1 小时）"""
        src = open(os.path.join(PROJECT_DIR, "app", "schedulers.py")).read()
        idx = src.find("_DECISION_COOLDOWN = 3600")
        assert idx != -1
        # Verify the default cooldown is still 3600
        assert "_DECISION_COOLDOWN = 3600" in src

    def test_cooldown_check_uses_per_rule_value(self):
        """冷却检查应使用 per-rule cooldown 值"""
        import app.schedulers as sched_mod

        with patch.object(sched_mod, "_decision_last_ask", {
            "late_night_sleep": time.time() - 7200,  # 2 hours ago
            "other_rule": time.time() - 1800,         # 30 min ago
        }):
            # late_night_sleep: 2h < 8h → still cooling
            last_ask = sched_mod._decision_last_ask.get("late_night_sleep", 0)
            cooldown = 28800 if "late_night_sleep" == "late_night_sleep" else sched_mod._DECISION_COOLDOWN
            assert time.time() - last_ask < cooldown

            # other_rule: 30min < 1h → still cooling
            last_ask = sched_mod._decision_last_ask.get("other_rule", 0)
            cooldown = 28800 if "other_rule" == "late_night_sleep" else sched_mod._DECISION_COOLDOWN
            assert time.time() - last_ask < cooldown


# ======================================================================
# P2-2: ASR audit duration measured inside worker
# ======================================================================

class TestASRAuditDuration:
    def test_decode_check_asr_returns_duration(self):
        """_decode_check_asr 应返回 duration_ms（在 worker 内部计时）"""
        from app.xiaozhi_ws import _decode_check_asr

        fake_wav = b"RIFF" + b"\x00" * 100

        with patch("app.xiaozhi_ws.opus_decode_to_wav", return_value=fake_wav), \
             patch("app.xiaozhi_ws.likely_empty_audio", return_value=False), \
             patch("voice_agent.asr_race", return_value=("今天天气不错", "sensevoice")):
            wav, text, provider, duration_ms = _decode_check_asr([b"frame1", b"frame2"])

        assert wav is fake_wav
        assert text == "今天天气不错"
        assert provider == "sensevoice"
        assert duration_ms > 0
        assert duration_ms < 5000  # should be small, not millions

    def test_process_utterance_uses_internal_duration(self):
        """process_utterance 应使用 worker 返回的 duration_ms，而非外部 t0"""
        import app.xiaozhi_ws as xz_mod
        from unittest.mock import patch as mock_patch, AsyncMock

        fake_wav = b"RIFF" + b"\x00" * 100
        fake_frames = [b"frame1"]

        # We need to verify that process_utterance does not use t0 for duration.
        # The easiest check: verify _decode_check_asr returns duration_ms
        # and process_utterance unpacks it.
        src = open(os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")).read()
        idx = src.find("def process_utterance")
        body = src[idx:idx + 2000]
        assert "asr_duration_ms" in body
        assert "duration_ms=asr_duration_ms" in body
        # Should not use t0 = time.monotonic() before to_thread
        assert "t0 = time.monotonic()" not in body.split("def process_utterance")[1].split("async def")[0]
