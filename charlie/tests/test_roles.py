"""Tests for multi-role / persona switching system (gitee assistant-x-openclaw pattern)."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestRoleDefinitions:
    def test_get_all_roles_returns_three_builtin(self):
        from agent.roles import get_all_roles
        roles = get_all_roles()
        assert "charlie" in roles
        assert "jarvis" in roles
        assert "baize" in roles

    def test_get_role_returns_config(self):
        from agent.roles import get_role
        role = get_role("jarvis")
        assert role is not None
        assert role["name"] == "J.A.R.V.I.S."
        assert "tts_voice" in role
        assert "wake_words" in role

    def test_get_role_unknown_returns_none(self):
        from agent.roles import get_role
        assert get_role("unknown_role") is None


class TestRoleSwitching:
    def test_switch_role_success(self):
        from agent.roles import switch_role, get_current_role
        # 默认是 charlie
        assert get_current_role() == "charlie"
        ok, msg = switch_role("jarvis")
        assert ok is True
        assert "J.A.R.V.I.S." in msg
        assert get_current_role() == "jarvis"
        # 切回
        switch_role("charlie")
        assert get_current_role() == "charlie"

    def test_switch_role_unknown_fails(self):
        from agent.roles import switch_role
        ok, msg = switch_role("unknown_role")
        assert ok is False
        assert "未知角色" in msg

    def test_switch_role_persists_to_preferences(self):
        from agent.roles import switch_role
        from agent.preferences import get_preference
        switch_role("baize")
        assert get_preference("current_role") == "baize"
        # 清理
        switch_role("charlie")


class TestRoleConfig:
    def test_get_role_system_prompt(self):
        from agent.roles import get_role_system_prompt
        prompt = get_role_system_prompt("jarvis")
        assert "J.A.R.V.I.S." in prompt or "Sir" in prompt or "Boss" in prompt

    def test_get_role_tts_config(self):
        from agent.roles import get_role_tts_config
        config = get_role_tts_config("charlie")
        assert "voice" in config
        assert "speed" in config
        assert isinstance(config["speed"], (int, float))

    def test_get_role_wake_words(self):
        from agent.roles import get_role_wake_words
        words = get_role_wake_words("jarvis")
        assert "jarvis" in words
        assert "贾维斯" in words


class TestSystemMsgRoleInjection:
    def test_build_system_msg_includes_role_prompt(self):
        from agent.system_msg import _build_system_msg
        from agent.roles import switch_role
        switch_role("jarvis")
        msg = _build_system_msg()
        assert "J.A.R.V.I.S." in msg or "Sir" in msg or "Boss" in msg
        # 清理
        switch_role("charlie")

    def test_build_system_msg_default_when_no_role(self):
        from agent.system_msg import _build_system_msg
        from agent.roles import switch_role
        switch_role("charlie")
        msg = _build_system_msg()
        assert "Charlie" in msg


class TestVoicePersistence:
    def test_set_role_tts_voice_persists_to_preferences(self):
        """P1-B: set_role_tts_voice 应同时写入 preferences.json"""
        from agent.roles import set_role_tts_voice
        from agent.preferences import get_preference
        set_role_tts_voice("charlie", "Cherry")
        assert get_preference("tts_voice:charlie") == "Cherry"
        # 清理
        set_role_tts_voice("charlie", "Ethan")
        assert get_preference("tts_voice:charlie") == "Ethan"

    def test_invalid_persisted_voice_does_not_override_on_load(self):
        """P1-B: 非法音色值在 _load_persisted_voice_overrides 中不应覆盖内置默认值"""
        from agent.roles import _load_persisted_voice_overrides, _ROLES
        from agent.preferences import set_preference
        # 先写入非法值
        set_preference("tts_voice:charlie", "INVALID_VOICE_XYZ")
        # 加载持久化覆盖
        _load_persisted_voice_overrides()
        # 应仍为内置默认值 Ethan，不被非法值覆盖
        assert _ROLES["charlie"]["tts_voice"] == "Ethan"
        # 清理
        set_preference("tts_voice:charlie", "Ethan")
        _load_persisted_voice_overrides()


class TestRoleVoiceDistinction:
    """三种角色应有不同的 TTS 音色（P0-1 修复）"""

    def test_charlie_uses_ethan(self):
        from agent.roles import get_role_tts_config
        assert get_role_tts_config("charlie")["voice"] == "Ethan"

    def test_jarvis_uses_alex(self):
        from agent.roles import get_role_tts_config
        assert get_role_tts_config("jarvis")["voice"] == "Alex"

    def test_baize_uses_echo(self):
        from agent.roles import get_role_tts_config
        assert get_role_tts_config("baize")["voice"] == "Echo"

    def test_all_roles_have_distinct_voices(self):
        from agent.roles import get_role_tts_config
        voices = {get_role_tts_config(r)["voice"] for r in ("charlie", "jarvis", "baize")}
        assert len(voices) == 3, f"三个角色应有3种不同音色，实际: {voices}"

    def test_role_voices_are_in_valid_set(self):
        from agent.roles import _VALID_TTS_VOICES
        for role_id in ("charlie", "jarvis", "baize"):
            from agent.roles import get_role_tts_config
            voice = get_role_tts_config(role_id)["voice"]
            assert voice in _VALID_TTS_VOICES, f"{role_id} 音色 {voice} 不在合法集合中"
