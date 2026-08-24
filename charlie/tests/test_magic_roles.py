"""Tests for magic-roles MCP tool and voice-triggered role switching."""
import os
import sys
import pytest
import importlib.util

# Test setup: load modules from parent dir (same as test_core.py)
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMagicRolesMCP:
    def test_switch_role_tool_exists(self):
        """magic-roles MCP should expose switch_role tool"""
        mod = _load_module("magic_roles_test", "magic-roles.py")
        assert hasattr(mod, 'switch_role')
        assert hasattr(mod, 'list_roles')
        assert hasattr(mod, 'get_role_info')

    def test_switch_role_to_jarvis(self):
        mod = _load_module("magic_roles_jarvis", "magic-roles.py")
        msg = mod.switch_role("jarvis")
        assert "J.A.R.V.I.S." in msg
        # Verify it's in the list
        roles_str = mod.list_roles()
        assert "jarvis" in roles_str
        # Reset to charlie
        mod.switch_role("charlie")

    def test_switch_role_to_baize(self):
        mod = _load_module("magic_roles_baize", "magic-roles.py")
        msg = mod.switch_role("baize")
        assert "白泽" in msg
        mod.switch_role("charlie")

    def test_switch_role_unknown_fails(self):
        mod = _load_module("magic_roles_unknown", "magic-roles.py")
        msg = mod.switch_role("unknown")
        assert "未知角色" in msg

    def test_list_roles_shows_current(self):
        mod = _load_module("magic_roles_list", "magic-roles.py")
        mod.switch_role("jarvis")
        roles_str = mod.list_roles()
        assert "jarvis" in roles_str
        assert "J.A.R.V.I.S." in roles_str
        mod.switch_role("charlie")

    def test_get_role_info(self):
        mod = _load_module("magic_roles_info", "magic-roles.py")
        info = mod.get_role_info("jarvis")
        assert "J.A.R.V.I.S." in info
        assert "TTS 音色" in info

    def test_mcp_meta_registration(self):
        """magic-roles.py should have correct __mcp_meta__ for auto-discovery"""
        mod = _load_module("magic_roles_meta", "magic-roles.py")
        assert hasattr(mod, '__mcp_meta__')
        meta = mod.__mcp_meta__
        assert meta["name"] == "magic-roles"
        assert meta["tier"] == "core"
        assert "label" in meta


class TestVoiceTriggeredRoleSwitching:
    """Test that role switching works through the voice pipeline"""

    def test_system_msg_includes_role_switch_instruction(self):
        """System prompt should tell LLM it can switch roles"""
        from agent.system_msg import _build_system_msg
        from agent.roles import switch_role
        switch_role("charlie")
        msg = _build_system_msg()
        assert "switch_role" in msg or "角色切换" in msg

    def test_role_persists_after_switch(self):
        """Role should persist in preferences"""
        from agent.roles import switch_role
        from agent.preferences import get_preference
        switch_role("jarvis")
        assert get_preference("current_role") == "jarvis"
        switch_role("charlie")
        assert get_preference("current_role") == "charlie"
