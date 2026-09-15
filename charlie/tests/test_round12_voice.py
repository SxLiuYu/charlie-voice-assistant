"""Round 12 voice-interaction fixes: flush-on-streaming-end, merged ASR pipeline,
ASR race pool reuse, wake echo cooldown, weather TTL cache, Silero VAD prewarm.
"""
import asyncio
import os
import sys
import time
import threading
import types
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Fix 1: flush_xiaozhi_pending (extracted from register_xiaozhi_client)
# ======================================================================

class TestFlushXiaozhiPending:
    """flush_xiaozhi_pending 补发 pending 且标记 reminder 完成"""

    def test_flush_sends_pending_and_completes_reminder(self):
        """streaming 结束触发 flush：补发条目 + complete_reminder_delivery"""
        from app.state import (
            flush_xiaozhi_pending, _xiaozhi_pending, _xiaozhi_lock,
            _xiaozhi_clients, _do_flush_xiaozhi_pending,
        )
        from app.xiaozhi_ws import _set_xiaozhi_streaming

        _set_xiaozhi_streaming(False)
        complete_calls = []

        def fake_complete(rid):
            complete_calls.append(rid)

        with _xiaozhi_lock:
            _xiaozhi_pending.clear()
            _xiaozhi_pending.append({
                "text": "提醒内容",
                "mp3": b"fake-mp3",
                "ts": time.time(),
                "reminder_id": 42,
            })

        real_loop = asyncio.new_event_loop()
        try:
            fake_ws = AsyncMock()
            fake_ws.send_text = AsyncMock()
            fake_ws.send_bytes = AsyncMock()
            fake_ws.client_state = 1  # CONNECTED

            client_id = "flush-test-client"
            with _xiaozhi_lock:
                _xiaozhi_clients[client_id] = {"ws": fake_ws, "loop": real_loop}

            with patch("app.reminders.complete_reminder_delivery",
                       side_effect=fake_complete):
                with patch("app.xiaozhi_codec.mp3_to_opus_packets",
                           return_value=[b"pkt1", b"pkt2"]):
                    # Directly run the flush coroutine on real_loop
                    sent = real_loop.run_until_complete(
                        _do_flush_xiaozhi_pending(
                            list(_xiaozhi_pending), fake_ws, real_loop))

            assert sent == 1, f"expected 1 item flushed, got {sent}"
            assert fake_ws.send_text.call_count >= 3  # start + sentence_start + stop
            assert 42 in complete_calls, f"reminder 42 not completed: {complete_calls}"
        finally:
            with _xiaozhi_lock:
                _xiaozhi_clients.pop(client_id, None)
                _xiaozhi_pending.clear()
            real_loop.close()

    def test_flush_empty_queue_returns_zero(self):
        """空队列 flush 返回 0，不抛异常"""
        from app.state import _do_flush_xiaozhi_pending

        fake_ws = AsyncMock()
        loop = asyncio.new_event_loop()
        try:
            sent = loop.run_until_complete(
                _do_flush_xiaozhi_pending([], fake_ws, loop))
        finally:
            loop.close()
        assert sent == 0


# ======================================================================
# Fix 2: merged ASR pipeline (_decode_check_asr)
# ======================================================================

class TestDecodeCheckASR:
    """_decode_check_asr 合并 decode+silence-check+ASR"""

    def test_empty_audio_returns_none_early(self):
        """空/静音音频 → (None, "", "", 0.0)，不上报 ASR"""
        from app.xiaozhi_ws import _decode_check_asr

        with patch("app.xiaozhi_ws.opus_decode_to_wav", return_value=b""), \
             patch("app.xiaozhi_ws.likely_empty_audio", return_value=True), \
             patch("voice_agent.asr_race") as mock_race:
            wav, text, provider, duration_ms = _decode_check_asr([])
        assert wav is None
        assert text == ""
        assert provider == ""
        assert duration_ms == 0.0
        mock_race.assert_not_called()

    def test_normal_audio_returns_wav_text_provider(self):
        """正常音频 → (wav, text, provider, duration_ms)"""
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


# ======================================================================
# Fix 2b: asr_race ThreadPoolExecutor reuse
# ======================================================================

class TestASRRacePoolReuse:
    """asr_race 复用模块级线程池，不每次新建"""

    def _reset_race_pool(self):
        import agent.asr_tts as asr_mod
        pool = asr_mod._RACE_POOL
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        asr_mod._RACE_POOL = None

    def test_two_calls_reuse_same_pool(self):
        """连续两次 asr_race 调用，_RACE_POOL 只初始化一次"""
        import agent.asr_tts as asr_mod

        self._reset_race_pool()
        try:
            with patch.dict(os.environ, {"STEPFUN_KEY": "k", "ASR_RACE": "1"}):
                with patch.object(asr_mod, "_asr_sense_voice", return_value="本地"), \
                     patch.object(asr_mod, "_asr_stepfun", return_value="云端"):
                    pool_id_1 = id(asr_mod._get_race_pool())
                    asr_mod.asr_race(b"audio", "wav")
                    pool_id_2 = id(asr_mod._get_race_pool())
                    asr_mod.asr_race(b"audio", "wav")
                    pool_id_3 = id(asr_mod._get_race_pool())

            assert pool_id_1 == pool_id_2 == pool_id_3, \
                f"pool not reused: {pool_id_1} vs {pool_id_2} vs {pool_id_3}"
        finally:
            self._reset_race_pool()


# ======================================================================
# Fix 3: wake echo cooldown constant
# ======================================================================

class TestWakeEchoCooldown:
    def test_cooldown_is_025(self):
        """WAKE_ECHO_COOLDOWN == 0.25（beep 3×60ms + 余量，避免丢首字）"""
        from app.xiaozhi_ws import WAKE_ECHO_COOLDOWN
        assert WAKE_ECHO_COOLDOWN == 0.25


# ======================================================================
# Fix 4: weather TTL cache
# ======================================================================

class TestWeatherTTLCache:
    """get_weather 的 TTL 缓存：同 city 不重复请求；过期后重新请求"""

    def _reset_weather_cache(self):
        import app.weather as wm
        with wm._weather_cache_lock:
            wm._weather_cache.clear()

    def test_two_calls_same_city_hits_cache(self):
        """同一城市两次 get_weather，只打一次网络请求"""
        self._reset_weather_cache()
        try:
            fake_result = {
                "city": "北京", "day_weather": "晴", "night_weather": "晴",
                "day_temp": 25, "night_temp": 18, "weather_text": "晴",
                "source": "open-meteo",
            }
            with patch("app.weather._amap_get", return_value=None), \
                 patch("app.weather._open_meteo_get", return_value=fake_result) as mock_om:
                from app.weather import get_weather
                r1 = get_weather("北京")
                r2 = get_weather("北京")

            assert r1["source"] == "open-meteo"
            assert r2["source"] == "open-meteo"
            assert mock_om.call_count == 1, (
                f"expected 1 Open-Meteo call for same city, got {mock_om.call_count}"
            )
        finally:
            self._reset_weather_cache()

    def test_ttl_expiry_triggers_new_request(self):
        """TTL 过期后同一城市重新发起请求（首次过期→刷新→二次命中新缓存）"""
        import app.weather as wm
        self._reset_weather_cache()
        try:
            fake_result = {
                "city": "上海", "day_weather": "雨", "night_weather": "雨",
                "day_temp": 20, "night_temp": 15, "weather_text": "雨",
                "source": "open-meteo",
            }
            # 预填一个过期缓存
            past = time.time() - 601  # 超过 600s TTL
            with wm._weather_cache_lock:
                wm._weather_cache["上海"] = (fake_result, past)

            with patch("app.weather._amap_get", return_value=None), \
                 patch("app.weather._open_meteo_get",
                       return_value=fake_result) as mock_om:
                from app.weather import get_weather
                # 第一次：缓存过期 → 发请求，写入新缓存
                get_weather("上海")
                # 第二次：新缓存有效 → 命中，不发请求
                get_weather("上海")

            # 共 1 次网络请求（首次过期触发刷新，二次走缓存）
            assert mock_om.call_count == 1, (
                f"expected 1 call (expired→refill then cache hit), got {mock_om.call_count}"
            )
        finally:
            self._reset_weather_cache()

    def test_different_cities_independent_cache(self):
        """不同城市各有独立缓存"""
        self._reset_weather_cache()
        try:
            beijing = {
                "city": "北京", "day_weather": "晴", "night_weather": "晴",
                "day_temp": 25, "night_temp": 18, "weather_text": "晴",
                "source": "open-meteo",
            }
            shanghai = {
                "city": "上海", "day_weather": "雨", "night_weather": "雨",
                "day_temp": 20, "night_temp": 15, "weather_text": "雨",
                "source": "open-meteo",
            }
            with patch("app.weather._amap_get", return_value=None), \
                 patch("app.weather._open_meteo_get",
                       side_effect=[beijing, shanghai]) as mock_om:
                from app.weather import get_weather
                r1 = get_weather("北京")
                r2 = get_weather("上海")
                r3 = get_weather("北京")  # cache hit

            assert r1["day_weather"] == "晴"
            assert r2["day_weather"] == "雨"
            assert mock_om.call_count == 2, (
                f"expected 2 distinct calls for 2 cities, got {mock_om.call_count}"
            )
        finally:
            self._reset_weather_cache()


# ======================================================================
# Side: Silero VAD prewarm
# ======================================================================

class TestSileroVADPrewarm:
    """prewarm 线程中调用 _load_silero_vad"""

    def test_prewarm_calls_load_silero_vad(self):
        """_prewarm_hints 内调用了 _load_silero_vad（源码结构断言）"""
        import app.xiaozhi_ws as xw
        import inspect

        src = inspect.getsource(xw.register_xiaozhi_routes)
        assert "_load_silero_vad()" in src, "_load_silero_vad not called in prewarm"

    def test_load_silero_vad_is_callable(self):
        """_load_silero_vad 可导入且调用不抛异常"""
        from app.xiaozhi_ws import _load_silero_vad
        # 调用一次；VAD 可能未安装，函数应优雅降级而非抛异常
        result = _load_silero_vad()
        # result 可能是 None (未安装/禁用) 或模型实例
        assert result is None or True  # 只验证不 crash
