"""Round 9 blindspots behavior tests.

Covers zero/very-low coverage modules:
1. app.mqtt_push - MQTT push channel
2. agent.state - User state machine
3. app.brain_health - Brain warmup/health
4. app.environment - Air quality / earthquake
5. app.finance - Stock / forex
6. app.sports - Sports events
"""
from __future__ import annotations

import datetime
import sys
import os
import time
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# 1. app.mqtt_push
# ======================================================================

class TestMqttPush:
    """publish_wake / is_available 行为测试"""

    def test_publish_wake_success(self):
        """get_server 返回 MagicMock 时 push_notification 被调一次且返回 True"""
        from app.mqtt_push import publish_wake
        mock_server = MagicMock()
        with patch('app.mqtt_server.get_server', return_value=mock_server):
            result = publish_wake("dev1", "test_reason")
        assert result is True
        mock_server.push_notification.assert_called_once_with("Charlie: test_reason")

    def test_publish_wake_no_server(self):
        """get_server 返回 None 时 publish_wake 返回 False"""
        from app.mqtt_push import publish_wake
        with patch('app.mqtt_server.get_server', return_value=None):
            result = publish_wake("dev1", "test")
        assert result is False

    def test_is_available_no_server(self):
        """无 server 时 is_available 返回 False"""
        from app.mqtt_push import is_available
        with patch('app.mqtt_server.get_server', return_value=None):
            result = is_available()
        assert result is False

    def test_is_available_with_server(self):
        """有 server 时 is_available 返回 True"""
        from app.mqtt_push import is_available
        with patch('app.mqtt_server.get_server', return_value=MagicMock()):
            result = is_available()
        assert result is True


# ======================================================================
# 2. agent.state
# ======================================================================

class TestAgentState:
    """用户状态机情境推理"""

    def _reset_state(self):
        import agent.state as state_mod
        state_mod._user_state.update({
            "state": "unknown",
            "confidence": 0.0,
            "last_update": 0.0,
            "last_location": None,
            "last_voice_activity": 0.0,
            "last_screen_active": 0.0,
            "home_location": None,
        })

    def test_presence_at_home_deep_night_voice_idle(self):
        """presence 判定在家 + 深夜 + voice_idle>3600 → home_sleeping"""
        import agent.state as state_mod
        self._reset_state()
        state_mod._user_state["home_location"] = (39.9, 116.4)
        state_mod._user_state["last_location"] = (39.9, 116.4)

        mock_dt = MagicMock()
        mock_dt.now.return_value = MagicMock(hour=2)

        mock_presence = MagicMock()
        mock_presence.detect_devices.return_value = {"at_home": True}
        mock_presence.get_presence_confidence.return_value = 0.9

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            state = state_mod.update_user_state()

        assert state["state"] == "home_sleeping"
        assert state["confidence"] == 0.7

    def test_voice_activity_makes_home_awake(self):
        """presence 在家 + 深夜 + voice_idle<1800 → home_awake"""
        import agent.state as state_mod
        self._reset_state()
        state_mod._user_state["home_location"] = (39.9, 116.4)
        state_mod._user_state["last_location"] = (39.9, 116.4)
        state_mod._user_state["last_voice_activity"] = time.time() - 100

        mock_dt = MagicMock()
        mock_dt.now.return_value = MagicMock(hour=2)

        mock_presence = MagicMock()
        mock_presence.detect_devices.return_value = {"at_home": True}
        mock_presence.get_presence_confidence.return_value = 0.9

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            state = state_mod.update_user_state()

        assert state["state"] == "home_awake"
        assert state["confidence"] == 0.5

    def test_presence_unavailable_falls_back_to_location(self):
        """presence 异常 → 降级到 location 判定不崩溃"""
        import agent.state as state_mod
        self._reset_state()
        state_mod._user_state["home_location"] = (39.9, 116.4)
        state_mod._user_state["last_location"] = (39.9, 116.4)
        # voice_idle very large (default last_voice_activity=0)
        state_mod._user_state["last_voice_activity"] = 0

        mock_dt = MagicMock()
        mock_dt.now.return_value = MagicMock(hour=2)

        mock_presence = MagicMock()
        mock_presence.detect_devices.side_effect = RuntimeError("presence down")

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            state = state_mod.update_user_state()

        # Falls back to location (at_home=True), deep night + huge voice idle
        assert state["state"] == "home_sleeping"
        assert state["confidence"] == 0.7

    def test_state_transitions_reasonable(self):
        """home_awake → home_resting → home_sleeping 连续转移合理"""
        import agent.state as state_mod
        self._reset_state()
        state_mod._user_state["home_location"] = (39.9, 116.4)

        mock_dt = MagicMock()
        mock_dt.now.return_value = MagicMock(hour=2)

        mock_presence = MagicMock()
        mock_presence.detect_devices.return_value = {"at_home": True}
        mock_presence.get_presence_confidence.return_value = 0.9

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            # Stage 1: voice just happened → home_awake
            state_mod._user_state["last_voice_activity"] = time.time() - 100
            s1 = state_mod.update_user_state()
        assert s1["state"] == "home_awake"

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            # Stage 2: idle 2000s → home_resting
            state_mod._user_state["last_voice_activity"] = time.time() - 2000
            s2 = state_mod.update_user_state()
        assert s2["state"] == "home_resting"

        with patch.object(state_mod.datetime, 'datetime', mock_dt), \
             patch.dict(sys.modules, {"presence": mock_presence}):
            # Stage 3: idle 4000s → home_sleeping
            state_mod._user_state["last_voice_activity"] = time.time() - 4000
            s3 = state_mod.update_user_state()
        assert s3["state"] == "home_sleeping"


# ======================================================================
# 3. app.brain_health
# ======================================================================

class TestBrainHealth:
    """大脑预热与健康状态"""

    def test_brain_is_warm_when_brains_exist(self):
        """_brain_is_warm 在 brains 非空时返回 True"""
        from app.brain_health import _brain_is_warm
        with patch('voice_agent._brains', ["brain1"]):
            assert _brain_is_warm() is True

    def test_brain_is_warm_when_empty(self):
        """_brain_is_warm 在 brains 为空时返回 False"""
        from app.brain_health import _brain_is_warm
        with patch('voice_agent._brains', []):
            assert _brain_is_warm() is False

    def test_get_brain_health_exception_returns_fallback(self):
        """brain_status 抛异常时 _get_brain_health 返回 ready=False 的容错结构"""
        from app.brain_health import _get_brain_health
        with patch('voice_agent.brain_status', side_effect=RuntimeError("brain down")):
            result = _get_brain_health()
        assert result["ready"] is False
        assert "error" in result

    def test_warmup_brain_exceptions_dont_leak(self):
        """后台预热各步骤异常时，主流程不抛异常"""
        import threading
        from app.brain_health import _warmup_brain
        created = []
        real_thread_cls = threading.Thread

        def fake_thread(*args, **kwargs):
            t = real_thread_cls(*args, **kwargs)
            created.append(t)
            return t

        with patch('app.brain_health.threading.Thread', side_effect=fake_thread), \
             patch('app.brain_health.asyncio.new_event_loop', side_effect=RuntimeError("warmup test")), \
             patch('app.brain_health.asyncio.set_event_loop'):
            _warmup_brain()
            for t in created:
                t.join(timeout=5)


# ======================================================================
# 4. app.environment
# ======================================================================

class TestEnvironment:
    """空气质量 / 地震预警"""

    def test_get_air_quality_levels(self):
        """AQI=75 → 良, UV=3 → 中等"""
        from app.environment import get_air_quality
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "current": {
                "pm2_5": 35,
                "pm10": 50,
                "us_aqi": 75,
                "uv_index": 3,
            }
        }
        with patch('requests.get', return_value=mock_response):
            result = get_air_quality("北京")
        assert result["aqi"] == 75
        assert result["aqi_level"] == "良"
        assert result["uv_level"] == "中等"

    def test_get_earthquake_parsing(self):
        """地震数据 mag/位置/海啸解析正确"""
        from app.environment import get_earthquake
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "features": [
                {
                    "properties": {
                        "mag": 6.5,
                        "place": "10km S of City",
                        "time": 1700000000000,
                        "tsunami": 1,
                    },
                    "geometry": {"coordinates": [120.5, 30.5, 10]},
                }
            ]
        }
        with patch('requests.get', return_value=mock_response):
            result = get_earthquake(4.5)
        assert len(result) == 1
        assert result[0]["magnitude"] == 6.5
        assert result[0]["place"] == "10km S of City"
        assert result[0]["tsunami"] is True
        assert result[0]["lat"] == 30.5
        assert result[0]["lon"] == 120.5

    def test_get_air_quality_network_error(self):
        """网络异常返回友好错误文本，不抛异常"""
        from app.environment import get_air_quality
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_air_quality("北京")
        assert result["aqi_level"] == "未知"
        assert result["city"] == "北京"

    def test_get_earthquake_network_error(self):
        """网络异常返回空列表，不抛异常"""
        from app.environment import get_earthquake
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_earthquake()
        assert result == []


# ======================================================================
# 5. app.finance
# ======================================================================

class TestFinance:
    """股票 / 外汇行情解析"""

    def test_parse_sina_quote_individual_stock(self):
        """完整个股格式解析正确"""
        from app.finance import _parse_sina_quote
        text = (
            'var hq_str_sh600519="贵州茅台,500.000,500.000,500.000,500.000,500.000,'
            '2024/1/1,500.000,500,100,100,100,100,100,100,100,100,100,100,100,'
            '100,100,100,100,100,100,100,100,100,100,100,100";'
        )
        results = _parse_sina_quote(text)
        assert len(results) == 1
        assert results[0]["name"] == "贵州茅台"
        assert results[0]["price"] == 500.0
        assert results[0]["prev_close"] == 500.0
        assert results[0]["high"] == 500.0
        assert results[0]["low"] == 500.0

    def test_parse_sina_quote_brief_index(self):
        """s_ 前缀简要指数格式解析正确"""
        from app.finance import _parse_sina_quote
        text = 'var hq_str_s_sh000001="上证指数,3000.000,10.000,0.33,1000000,10000000000";'
        results = _parse_sina_quote(text)
        assert len(results) == 1
        assert results[0]["name"] == "上证指数"
        assert results[0]["price"] == 3000.0
        assert results[0]["change"] == 10.0
        assert results[0]["change_pct"] == 0.33

    def test_get_stock_network_error(self):
        """网络异常返回 None"""
        from app.finance import get_stock
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_stock("上证")
        assert result is None

    def test_get_stock_text_network_error(self):
        """网络异常时返回友好错误文本"""
        from app.finance import get_stock_text
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_stock_text("上证")
        assert "未找到股票" in result

    def test_get_forex_parsing(self):
        """外汇汇率解析正确"""
        from app.finance import get_forex
        mock_response = MagicMock()
        mock_response.text = (
            'var hq_str_fx_susdcny="美元人民币,7.2500,7.2600,7.2550,7.2700,7.2400'
            ',2024/1/1,12:00:00";'
        )
        with patch('requests.get', return_value=mock_response):
            result = get_forex("USDCNY")
        assert result is not None
        assert result["pair"] == "USDCNY"
        assert result["bid"] == 7.25
        assert result["ask"] == 7.26
        assert result["high"] == 7.27
        assert result["low"] == 7.24

    def test_get_stock_batch_network_error(self):
        """批量查询网络异常返回友好文本"""
        from app.finance import get_stock_batch
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_stock_batch(["上证", "深证"])
        assert result == "查询失败"


# ======================================================================
# 6. app.sports
# ======================================================================

class TestSports:
    """体育赛事查询"""

    def test_get_events_today_parsing(self):
        """TheSportsDB 响应解析：比分/时间/场地正确"""
        from app.sports import get_events_today
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "events": [
                {
                    "strSport": "Soccer",
                    "strEvent": "Team A vs Team B",
                    "strHomeTeam": "Team A",
                    "strAwayTeam": "Team B",
                    "strTime": "20:00:00",
                    "strStatus": "Not Started",
                    "intHomeScore": "2",
                    "intAwayScore": "1",
                    "strVenue": "Stadium X",
                }
            ]
        }
        with patch('requests.get', return_value=mock_response):
            result = get_events_today("英超")
        assert len(result) == 1
        assert result[0]["home"] == "Team A"
        assert result[0]["away"] == "Team B"
        assert result[0]["home_score"] == "2"
        assert result[0]["away_score"] == "1"
        assert result[0]["venue"] == "Stadium X"

    def test_get_events_text_formatting(self):
        """赛事列表格式化：包含比分和时间"""
        from app.sports import get_events_text
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "events": [
                {
                    "strSport": "Soccer",
                    "strEvent": "Team A vs Team B",
                    "strHomeTeam": "Team A",
                    "strAwayTeam": "Team B",
                    "strTime": "20:00:00",
                    "strStatus": "Not Started",
                    "intHomeScore": "2",
                    "intAwayScore": "1",
                    "strVenue": "Stadium X",
                }
            ]
        }
        with patch('requests.get', return_value=mock_response):
            result = get_events_text("英超")
        assert "Team A vs Team B" in result
        assert "2-1" in result
        assert "20:00" in result

    def test_get_events_today_empty(self):
        """events 为 None 时返回空列表"""
        from app.sports import get_events_today
        mock_response = MagicMock()
        mock_response.json.return_value = {"events": None}
        with patch('requests.get', return_value=mock_response):
            result = get_events_today("英超")
        assert result == []

    def test_get_events_text_no_events(self):
        """无赛事时返回友好提示"""
        from app.sports import get_events_text
        mock_response = MagicMock()
        mock_response.json.return_value = {"events": []}
        with patch('requests.get', return_value=mock_response):
            result = get_events_text("英超")
        assert "没有赛事" in result

    def test_get_events_today_network_error(self):
        """网络异常返回空列表，不抛异常"""
        from app.sports import get_events_today
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_events_today("英超")
        assert result == []

    def test_get_all_sports_today_network_error(self):
        """全量查询网络异常返回友好提示"""
        from app.sports import get_all_sports_today
        with patch('requests.get', side_effect=requests.exceptions.Timeout("timeout")):
            result = get_all_sports_today()
        assert result == "赛事查询失败"
