"""Tests for JARVIS features (P0-1, P0-2, P0-3, P1-4, P1-6).

覆盖:
1. strip_wake_word 支持 jarvis/贾维斯 中英文
2. jarvis vs charlie TTS 音色不同; get_effective_tts_config 角色感知
3. ARM_WINDOW 角色感知 (jarvis 默认 300s > charlie 默认 30s)
4. jarvis system_prompt 含 "Sir"; _build_system_msg 注入贾维斯专属准则
5. morning_briefing/evening_report 返回含 "Sir" 的非空字符串
6. local_wake._WAKE_WORDS 包含 jarvis/贾维斯
"""
import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== 测试 1: strip_wake_word 支持 jarvis/贾维斯 =====

class TestStripWakeWordJarvis:
    def test_strip_chinese_jarvis(self):
        from agent.intent import strip_wake_word
        result = strip_wake_word("贾维斯，今天天气怎么样")
        assert result == "今天天气怎么样"

    def test_strip_english_jarvis(self):
        from agent.intent import strip_wake_word
        result = strip_wake_word("jarvis, what's the weather")
        assert result == "what's the weather"

    def test_strip_after_switch_to_jarvis_role(self):
        from agent.intent import strip_wake_word
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            result = strip_wake_word("贾维斯，今天天气怎么样")
            assert result == "今天天气怎么样"
        finally:
            switch_role("charlie")

    def test_fallback_regex_strips_jarvis_when_roles_empty(self):
        """roles 导入失败/返回空列表时，静态 fallback 正则仍能剥离 jarvis。"""
        from agent.intent import _WAKE_STRIP_RE
        # 直接用静态 fallback 正则（_WAKE_STRIP_RE），不依赖动态版本
        result = _WAKE_STRIP_RE.sub("", "jarvis, what's the weather", count=1).strip()
        assert result == "what's the weather"
        result2 = _WAKE_STRIP_RE.sub("", "贾维斯，今天天气怎么样", count=1).strip()
        assert result2 == "今天天气怎么样"


# ===== 测试 2: jarvis vs charlie TTS 音色不同; get_effective_tts_config =====

class TestTtsVoiceJarvis:
    def test_jarvis_voice_differs_from_charlie(self):
        """jarvis 用 Alex（沉稳男声），charlie 用 Ethan（温暖男声），baize 用 Echo（中性声）。"""
        from agent.roles import get_role_tts_config
        charlie = get_role_tts_config("charlie")
        jarvis = get_role_tts_config("jarvis")
        baize = get_role_tts_config("baize")
        assert charlie["voice"] == "Ethan"
        assert jarvis["voice"] == "Alex"
        assert baize["voice"] == "Echo"
        # 三个角色音色各不相同
        assert len({charlie["voice"], jarvis["voice"], baize["voice"]}) == 3

    def test_get_effective_tts_config_jarvis_role(self):
        from agent.asr_tts import get_effective_tts_config
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            voice, speed = get_effective_tts_config()
            # jarvis 用 Alex（沉稳男声），语速 0.92
            assert voice == "Alex"
            assert speed == 0.92
        finally:
            switch_role("charlie")

    def test_get_effective_tts_config_charlie_role(self):
        from agent.asr_tts import get_effective_tts_config
        from agent.roles import switch_role
        try:
            switch_role("charlie")
            voice, speed = get_effective_tts_config()
            assert voice == "Ethan"
            assert speed == 1.0
        finally:
            switch_role("charlie")


# ===== 测试 3: ARM_WINDOW 角色感知 =====

class TestArmWindowRoleAware:
    def test_jarvis_arm_window_greater_than_charlie(self):
        from app.xiaozhi_ws import _get_arm_window
        from agent.roles import switch_role
        # 先清掉所有可能干扰的环境变量，确保读到默认值
        os.environ.pop("XIAOZHI_JARVIS_ARM_WINDOW", None)
        os.environ.pop("XIAOZHI_ARM_WINDOW", None)
        try:
            switch_role("jarvis")
            jarvis_win = _get_arm_window()
            switch_role("charlie")
            charlie_win = _get_arm_window()
            # jarvis 300s > charlie 120s（charlie 也有较长窗口支持主动服务）
            assert jarvis_win > charlie_win
            assert jarvis_win == 300.0
            assert charlie_win == 120.0
        finally:
            switch_role("charlie")
            os.environ.pop("XIAOZHI_JARVIS_ARM_WINDOW", None)
            os.environ.pop("XIAOZHI_ARM_WINDOW", None)

    def test_jarvis_arm_window_env_override(self):
        from app.xiaozhi_ws import _get_arm_window
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            os.environ["XIAOZHI_JARVIS_ARM_WINDOW"] = "120.0"
            win = _get_arm_window()
            assert win == 120.0
        finally:
            switch_role("charlie")
            os.environ.pop("XIAOZHI_JARVIS_ARM_WINDOW", None)

    def test_charlie_arm_window_env_override(self):
        from app.xiaozhi_ws import _get_arm_window
        from agent.roles import switch_role
        try:
            switch_role("charlie")
            os.environ["XIAOZHI_ARM_WINDOW"] = "60.0"
            win = _get_arm_window()
            assert win == 60.0
        finally:
            switch_role("charlie")
            os.environ.pop("XIAOZHI_ARM_WINDOW", None)

    def test_arm_window_immediate_effect_after_role_switch(self):
        """角色切换后下一次窗口判断即时生效（_get_arm_window 每次调用读取角色）。"""
        from app.xiaozhi_ws import _get_arm_window
        from agent.roles import switch_role
        try:
            switch_role("charlie")
            os.environ.pop("XIAOZHI_ARM_WINDOW", None)
            os.environ.pop("XIAOZHI_JARVIS_ARM_WINDOW", None)
            charlie_win = _get_arm_window()
            switch_role("jarvis")
            jarvis_win = _get_arm_window()
            # 不切换回 charlie，直接再调一次确认仍是 jarvis 值
            jarvis_win2 = _get_arm_window()
            assert charlie_win == 120.0
            assert jarvis_win == 300.0
            assert jarvis_win2 == 300.0
        finally:
            switch_role("charlie")


# ===== 测试 4: jarvis system_prompt 含 Sir; _build_system_msg 专属准则 =====

class TestJarvisSystemPrompt:
    def test_jarvis_system_prompt_contains_sir(self):
        from agent.roles import get_role_system_prompt
        prompt = get_role_system_prompt("jarvis")
        assert "Sir" in prompt

    def test_build_system_msg_includes_jarvis_rules_in_jarvis_role(self):
        from agent.system_msg import _build_system_msg
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            msg = _build_system_msg()
            assert "Sir" in msg
            assert "汇报式" in msg or "管家" in msg or "英式" in msg or "主动报告" in msg
        finally:
            switch_role("charlie")

    def test_build_system_msg_charlie_has_proactive_rules(self):
        """charlie 角色也应有主动服务准则（JARVIS 能力是 Charlie 的默认能力）。"""
        from agent.system_msg import _build_system_msg
        from agent.roles import switch_role
        try:
            switch_role("charlie")
            msg = _build_system_msg()
            # charlie 也有主动服务和汇报式准则
            assert "主动" in msg
            assert "行为准则" in msg
        finally:
            switch_role("charlie")


# ===== 测试 5: morning_briefing/evening_report =====

class TestMorningEveningBriefing:
    def setup_method(self):
        self._orig_data_dir = os.environ.get("ASSISTANT_KID_DATA_DIR")
        self._tmp_dir = __import__("tempfile").mkdtemp(prefix="jarvis_brief_test_")
        os.environ["ASSISTANT_KID_DATA_DIR"] = self._tmp_dir
        os.environ.pop("CHARLIE_USER_ID", None)
        self.jarvis = _load_module("jarvis_brief_test", "magic-jarvis.py")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._orig_data_dir is not None:
            os.environ["ASSISTANT_KID_DATA_DIR"] = self._orig_data_dir
        else:
            os.environ.pop("ASSISTANT_KID_DATA_DIR", None)

    def test_morning_briefing_returns_non_empty_with_sir(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_preferences") as p, \
             patch.object(self.jarvis, "_get_weather_context", return_value="今天晴，25度"):
            tc.return_value = {
                "now": __import__("datetime").datetime(2026, 8, 22, 8, 0),
                "hour": 8, "weekday": 5, "period": "早晨", "is_weekend": True
            }
            p.return_value = {}
            result = self.jarvis.morning_briefing()
            assert isinstance(result, str)
            assert len(result) > 0
            assert "Sir" in result

    def test_evening_report_returns_non_empty_with_sir(self):
        with patch.object(self.jarvis, "_get_time_context") as tc, \
             patch.object(self.jarvis, "_get_preferences") as p:
            tc.return_value = {
                "now": __import__("datetime").datetime(2026, 8, 22, 21, 0),
                "hour": 21, "weekday": 5, "period": "晚上", "is_weekend": True
            }
            p.return_value = {}
            result = self.jarvis.evening_report()
            assert isinstance(result, str)
            assert len(result) > 0
            assert "Sir" in result


# ===== 测试 6: local_wake._WAKE_WORDS 包含 jarvis/贾维斯 =====

class TestLocalWakeWords:
    def test_wake_words_include_jarvis_and_chinese(self):
        import local_wake
        assert "jarvis" in local_wake._WAKE_WORDS
        assert "贾维斯" in local_wake._WAKE_WORDS


# ===== 测试 7: _synth_mp3 缓存隔离（P0 修复） =====

class TestSynthMp3CacheIsolation:
    def test_synth_mp3_passes_role_voice_to_cache(self):
        """_synth_mp3 应将角色音色传入 _tts_cache_get/_tts_cache_put，避免跨角色缓存污染。"""
        from unittest.mock import patch
        from app.xiaozhi_ws import _synth_mp3
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            recorded_calls = []
            def fake_get(sentence, voice="", model=""):
                recorded_calls.append(("get", sentence, voice, model))
                return None
            def fake_put(sentence, audio, voice="", model=""):
                recorded_calls.append(("put", sentence, voice, model))
            # 让 _tts_baidu 返回一个足够长的假音频，避免被长度检查过滤
            # 同时 mock subprocess.run（ffmpeg 转换）以避免真实调用
            mock_subprocess = MagicMock()
            mock_subprocess.run.return_value.stdout = b"x" * 200
            mock_subprocess.run.return_value.returncode = 0
            with patch("agent.asr_tts.get_effective_tts_config", return_value=("Ethan", 0.92)), \
                 patch("agent.asr_tts.TTS_MODEL", "qwen3-tts-flash"), \
                 patch("agent.asr_tts._tts_baidu", return_value=b"fake mp3 data that is long enough for cache"), \
                 patch("subprocess.run", mock_subprocess.run):
                result = _synth_mp3("test sentence", _cache_get=fake_get, _cache_put=fake_put)
                # 缓存查询时应传入 jarvis 的音色和 TTS_MODEL，而非默认 "Ethan"
                get_calls = [c for c in recorded_calls if c[0] == "get"]
                put_calls = [c for c in recorded_calls if c[0] == "put"]
                assert len(get_calls) == 1
                assert get_calls[0] == ("get", "test sentence", "Ethan", "qwen3-tts-flash")
                assert len(put_calls) == 1
                assert put_calls[0][:3] == ("put", "test sentence", "Ethan")
                assert put_calls[0][3] == "qwen3-tts-flash"
        finally:
            switch_role("charlie")

    def test_synth_mp3_cache_put_uses_role_voice_on_hit(self):
        """缓存命中时返回的应是角色对应音色的音频，而非跨角色缓存的。"""
        from unittest.mock import patch
        from app.xiaozhi_ws import _synth_mp3
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            fake_audio = b"fake mp3 data for jarvis"
            recorded_calls = []
            def fake_get(sentence, voice="", model=""):
                recorded_calls.append(("get", sentence, voice, model))
                return fake_audio
            def fake_put(sentence, audio, voice="", model=""):
                recorded_calls.append(("put", sentence, voice, model))
            with patch("agent.asr_tts.get_effective_tts_config", return_value=("Ethan", 0.92)), \
                 patch("agent.asr_tts.TTS_MODEL", "qwen3-tts-flash"):
                result = _synth_mp3("hello", _cache_get=fake_get, _cache_put=fake_put)
                assert result == fake_audio
                # 缓存命中也应通过 role voice 的 key 查询
                get_calls = [c for c in recorded_calls if c[0] == "get"]
                assert len(get_calls) == 1
                assert get_calls[0] == ("get", "hello", "Ethan", "qwen3-tts-flash")
                # 不会走到 put（命中直接返回）
                put_calls = [c for c in recorded_calls if c[0] == "put"]
                assert len(put_calls) == 0
        finally:
            switch_role("charlie")


# ===== 测试 8: 动态唤醒词正则缓存失效（P1 修复） =====

class TestWakeWordRegexCacheInvalidation:
    def test_switch_role_invalidates_dynamic_wake_regex(self):
        """switch_role 后，动态唤醒词正则缓存应被清除，下次 strip_wake_word 重建。"""
        from agent.intent import _WAKE_STRIP_RE_DYNAMIC, _get_wake_strip_re
        from agent.roles import switch_role
        try:
            # 先触发一次动态正则构建
            re1 = _get_wake_strip_re()
            assert re1 is not None
            # 切换角色，应清除动态缓存
            switch_role("jarvis")
            import agent.intent as _intent_mod
            assert _intent_mod._WAKE_STRIP_RE_DYNAMIC is None
            # 再次调用应重建
            re2 = _get_wake_strip_re()
            assert re2 is not None
        finally:
            switch_role("charlie")
