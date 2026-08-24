"""测试 ASR/TTS 优先级链遍历

注意：asr_tts._TTS_PROVIDERS / _ASR_PROVIDERS 在模块导入时即被填充为真实的
函数引用。patch.object(asr_tts, '_tts_baidu', ...) 只能替换模块属性，无法更新
字典中的引用，因此所有 provider 函数 mock 必须直接 patch.dict 到 _TTS_PROVIDERS /
_ASR_PROVIDERS，保证 tts()/asr() 遍历时取到的是 mock。
"""
import os
import pytest
from unittest.mock import patch, MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# 规范音色映射层（resolve_voice）
# ─────────────────────────────────────────────────────────────────────────────

class TestStepfunVoiceMapping:
    """验证 resolve_voice 在各 provider TTS 函数中的接入"""

    def test_tts_stepfun_uses_resolved_voice(self):
        """_tts_stepfun 应将规范音色 Ethan 解析为 cixingnansheng"""
        from agent import asr_tts
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"audio"
        with patch("agent.asr_tts.get_effective_tts_config", return_value=("Ethan", 1.0)), \
             patch("voice_agent._session.post", return_value=mock_resp) as mock_post:
            result = asr_tts._tts_stepfun("你好")
            payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
            assert payload["voice"] == "cixingnansheng"

    def test_tts_baidu_uses_resolved_per(self):
        """_tts_baidu 应将规范音色 Ethan 解析为 per=3"""
        from agent import asr_tts
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "audio/mp3"}
        mock_resp.content = b"audio"
        mock_resp.json.return_value = {}
        with patch("agent.asr_tts.get_effective_tts_config", return_value=("Ethan", 1.0)), \
             patch("agent.asr_tts._baidu_get_token", return_value="fake-token"), \
             patch("voice_agent._session.post", return_value=mock_resp) as mock_post:
            result = asr_tts._tts_baidu("你好")
            call_kwargs = mock_post.call_args.kwargs
            data = call_kwargs.get("data", {})
            assert data.get("per") == 3


# ─────────────────────────────────────────────────────────────────────────────
# ASR
# ─────────────────────────────────────────────────────────────────────────────

class TestASRPriority:
    def test_asr_returns_first_successful(self):
        from agent import asr_tts
        fake = MagicMock(return_value="你好")
        with patch.dict(asr_tts._ASR_PROVIDERS, {"sensevoice": fake}):
            result = asr_tts.asr(b"audio", "mp3")
            assert result == "你好"
            fake.assert_called_once_with(b"audio", "mp3")

    def test_asr_falls_through_on_exception(self):
        from agent import asr_tts
        fake_sv = MagicMock(side_effect=RuntimeError("fail"))
        fake_bd = MagicMock(return_value="你好")
        with patch.dict(asr_tts._ASR_PROVIDERS, {
            "sensevoice": fake_sv,
            "baidu": fake_bd,
        }):
            result = asr_tts.asr(b"audio", "mp3")
            assert result == "你好"
            fake_bd.assert_called_once_with(b"audio", "mp3")

    def test_asr_skips_stepfun_when_no_key(self):
        from agent import asr_tts
        # Make every earlier provider raise so execution reaches stepfun
        fake_sv  = MagicMock(side_effect=RuntimeError("fail"))
        fake_bd  = MagicMock(side_effect=RuntimeError("fail"))
        fake_sf  = MagicMock(side_effect=AssertionError("should not be called"))
        fake_vk  = MagicMock(return_value="")
        with patch.dict(os.environ, {"STEPFUN_KEY": ""}), \
             patch.dict(asr_tts._ASR_PROVIDERS, {
                 "sensevoice": fake_sv,
                 "baidu": fake_bd,
                 "stepfun": fake_sf,
                 "vosk":     fake_vk,
             }):
            result = asr_tts.asr(b"audio", "mp3")
            fake_sf.assert_not_called()

    def test_asr_skips_unknown_provider(self):
        from agent import asr_tts
        fake_bd = MagicMock(return_value="你好")
        with patch.dict(asr_tts._ASR_PROVIDERS, {"baidu": fake_bd}):
            asr_tts.ASR_PRIORITY = ["unknown_provider", "baidu"]
            try:
                result = asr_tts.asr(b"audio", "mp3")
                assert result == "你好"
            finally:
                asr_tts.ASR_PRIORITY = [
                    p.strip() for p in
                    os.getenv("ASR_PRIORITY", "sensevoice,baidu,stepfun,vosk").split(",")
                    if p.strip()
                ]


# ─────────────────────────────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────────────────────────────

class TestTTSPriority:
    def test_tts_returns_first_successful(self):
        from agent import asr_tts
        fake = MagicMock(return_value=b"mp3audio")
        with patch.dict(asr_tts._TTS_PROVIDERS, {"baidu": fake}):
            result = asr_tts.tts("你好")
            assert result == b"mp3audio"

    def test_tts_falls_through_on_exception(self):
        from agent import asr_tts
        fake_bd = MagicMock(side_effect=RuntimeError("baidu down"))
        fake_sf = MagicMock(side_effect=RuntimeError("stepfun down"))
        fake_fn = MagicMock(return_value=b"mp3audio")
        with patch.dict(asr_tts._TTS_PROVIDERS, {
            "baidu": fake_bd,
            "stepfun": fake_sf,
            "finna": fake_fn,
        }):
            result = asr_tts.tts("你好")
            assert result == b"mp3audio"
            fake_fn.assert_called_once_with("你好")

    def test_tts_skips_stepfun_when_no_key(self):
        from agent import asr_tts
        from agent.asr_tts import TTSUnavailableError
        # Every earlier provider must raise so we reach stepfun in the loop
        fake_bd  = MagicMock(side_effect=RuntimeError("baidu fail"))
        fake_fn  = MagicMock(side_effect=RuntimeError("finna fail"))
        fake_sf  = MagicMock(side_effect=AssertionError("should not be called"))
        fake_vk  = MagicMock(return_value=b"vosk-mp3")
        with patch.dict(os.environ, {"STEPFUN_KEY": ""}), \
             patch.dict(asr_tts._TTS_PROVIDERS, {
                 "baidu": fake_bd,
                 "finna": fake_fn,
                 "stepfun": fake_sf,
             }):
            # vosk is not a TTS provider; append it just to have a final fallback
            asr_tts._TTS_PROVIDERS["vosk"] = fake_vk
            try:
                result = asr_tts.tts("你好")
                # Should return from vosk, stepfun never called
                assert fake_sf.call_count == 0
            except TTSUnavailableError:
                # Even on full failure, stepfun must not be called
                assert fake_sf.call_count == 0
            finally:
                asr_tts._TTS_PROVIDERS.pop("vosk", None)

    def test_tts_unavailable_error_stops_fallthrough(self):
        from agent import asr_tts
        from agent.asr_tts import TTSUnavailableError
        fake_bd = MagicMock(side_effect=TTSUnavailableError("cooldown"))
        fake_sf = MagicMock(side_effect=AssertionError("should not be called"))
        fake_fn = MagicMock(side_effect=AssertionError("should not be called"))
        with patch.dict(asr_tts._TTS_PROVIDERS, {
            "baidu": fake_bd,
            "stepfun": fake_sf,
            "finna": fake_fn,
        }):
            with pytest.raises(TTSUnavailableError):
                asr_tts.tts("你好")
            fake_sf.assert_not_called()
            fake_fn.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Provider 注册表结构验证
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderRegistry:
    def test_asr_providers_has_four(self):
        from agent.asr_tts import _ASR_PROVIDERS
        assert set(_ASR_PROVIDERS.keys()) == {"sensevoice", "baidu", "stepfun", "vosk"}

    def test_tts_providers_has_three(self):
        from agent.asr_tts import _TTS_PROVIDERS
        assert set(_TTS_PROVIDERS.keys()) == {"baidu", "finna", "stepfun"}


# ─────────────────────────────────────────────────────────────────────────────
# ASR 文本纠错（来自 gitee assistant-x-openclaw 的「文本纠错兜底」思路）
# ─────────────────────────────────────────────────────────────────────────────

class TestASRCorrection:
    def test_correction_disabled_by_default(self):
        """未显式开启时，correct_asr_text 应原样返回"""
        import importlib
        import agent.asr_correction as mod
        with patch.dict(os.environ, {"ASR_CORRECTION_ENABLED": "0"}):
            importlib.reload(mod)
            assert mod.correct_asr_text("四点了") == "四点了"
        # 恢复默认开启
        importlib.reload(mod)

    def test_filler_words_stripped(self):
        import agent.asr_correction as mod
        assert mod.correct_asr_text("嗯，帮我开空调") == "帮我开空调"
        assert mod.correct_asr_text("啊，现在几点了") == "现在几点了"

    def test_homophone_correction(self):
        import agent.asr_correction as mod
        assert mod.correct_asr_text("在开空调") == "再开空调"

    def test_wake_word_normalization(self):
        import agent.asr_correction as mod
        assert mod.correct_asr_text("查里，几点了") == "charlie，几点了"
        assert mod.correct_asr_text("小智，开灯") == "charlie，开灯"

    def test_file_based_corrections(self, tmp_path):
        """data/text_corrections.txt 规则应生效"""
        import agent.asr_correction as mod
        # 写入临时纠错文件
        corrections = tmp_path / "text_corrections.txt"
        corrections.write_text("四点了 : 十点了\n关机 : 关掉\n", encoding="utf-8")
        # 临时替换规则文件路径
        orig_file = mod._CORRECTIONS_FILE
        mod._CORRECTIONS_FILE = str(corrections)
        mod._file_rules_mtime = 0.0
        try:
            assert mod.correct_asr_text("四点了") == "十点了"
            assert mod.correct_asr_text("关机") == "关掉"
        finally:
            mod._CORRECTIONS_FILE = orig_file
            mod._file_rules_mtime = 0.0
            mod._load_file_rules()

    def test_empty_input_returns_original(self):
        import agent.asr_correction as mod
        assert mod.correct_asr_text("") == ""
        assert mod.correct_asr_text("   ") == "   "

    def test_asr_integration_calls_correction(self):
        """asr() 成功返回后应调用 correct_asr_text 做后处理"""
        from agent import asr_tts
        fake = MagicMock(return_value="四点了")
        with patch.dict(asr_tts._ASR_PROVIDERS, {"sensevoice": fake}), \
             patch("agent.asr_correction.correct_asr_text", side_effect=lambda t, **kw: t.replace("四", "十")) as mock_correct:
            result = asr_tts.asr(b"audio", "mp3", session_id="test_session")
            assert result == "十点了"
            mock_correct.assert_called_once_with("四点了", session_id="test_session")

