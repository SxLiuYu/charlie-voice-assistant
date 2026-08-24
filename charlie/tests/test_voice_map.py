"""测试规范音色映射层 resolve_voice"""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestVoiceMap:
    def test_resolve_voice_finna_ethan(self):
        from agent.asr_tts import resolve_voice
        assert resolve_voice("Ethan", "finna") == "Ethan"

    def test_resolve_voice_baidu_ethan(self):
        from agent.asr_tts import resolve_voice
        assert resolve_voice("Ethan", "baidu") == 3

    def test_resolve_voice_stepfun_ethan(self):
        from agent.asr_tts import resolve_voice
        assert resolve_voice("Ethan", "stepfun") == "cixingnansheng"

    def test_resolve_voice_unknown_canonical_fallback(self):
        """未知规范音色应回退到 Ethan 映射"""
        from agent.asr_tts import resolve_voice
        assert resolve_voice("UnknownVoice", "baidu") == 3

    def test_resolve_voice_unknown_provider_fallback(self):
        """未知 provider 应回退到 finna 映射（Ethan）"""
        from agent.asr_tts import resolve_voice
        assert resolve_voice("Ethan", "unknown") == "Ethan"
