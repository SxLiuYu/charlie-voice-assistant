"""测试空调控制 — 关机误触发防护"""
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def mock_tuya():
    """Mock TuyaCloudAPI and inject required env vars for every test."""
    env = {
        "TUYA_IR_DEVICE_ID": "ir-123",
        "TUYA_AC_DEVICE_ID": "ac-123",
        "TUYA_CLIENT_ID": "cid",
        "TUYA_ACCESS_KEY": "sk-xxx",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("tuya_api.TuyaCloudAPI") as MockAPI:
            instance = MockAPI.return_value
            instance.ac_status.return_value = {
                "power": 0, "mode": 0, "temp": 26, "wind": 1,
            }
            instance.ac_scenes_command.return_value = {}
            yield instance


class TestACPowerOffGuard:
    """关机判断必须有明确指令，疑问句不触发（防误触发）"""

    def test_explicit_close_triggers_off(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("关闭空调")
        assert "关闭" in result or "已关" in result
        mock_tuya.ac_scenes_command.assert_called_with("ir-123", "ac-123", power=0)

    def test_help_close_triggers_off(self, mock_tuya):
        from agent.device_control import direct_ac_control
        direct_ac_control("帮我关空调")
        mock_tuya.ac_scenes_command.assert_called_with("ir-123", "ac-123", power=0)

    def test_question_does_not_trigger(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("你是不是把客厅空调给关了")
        mock_tuya.ac_scenes_command.assert_not_called()

    def test_confirm_does_not_trigger(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("空调你刚才肯定关了")
        mock_tuya.ac_scenes_command.assert_not_called()

    def test_ask_status_does_not_trigger(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("空调关了吗")
        mock_tuya.ac_scenes_command.assert_not_called()

    def test_help_close_still_works(self, mock_tuya):
        """'帮我关空调' 应触发关机"""
        from agent.device_control import direct_ac_control
        direct_ac_control("帮我关空调")
        mock_tuya.ac_scenes_command.assert_called_with("ir-123", "ac-123", power=0)


class TestACPowerOn:
    """开机判断"""

    def test_open_ac_triggers_on(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("打开空调")
        assert "打开" in result or "已开" in result
        mock_tuya.ac_scenes_command.assert_called_with(
            "ir-123", "ac-123", power=1, mode=None, temp=26, wind=None,
        )

    def test_open_with_temp(self, mock_tuya):
        from agent.device_control import direct_ac_control
        direct_ac_control("打开空调制冷26度")
        mock_tuya.ac_scenes_command.assert_called_with(
            "ir-123", "ac-123", power=1, mode=0, temp=26, wind=None,
        )

    def test_open_returns_message(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("打开空调")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_empty_string_when_no_match(self, mock_tuya):
        from agent.device_control import direct_ac_control
        result = direct_ac_control("今天天气怎么样")
        assert result == ""
        mock_tuya.ac_scenes_command.assert_not_called()

    def test_returns_empty_when_env_missing(self):
        """没有设备 ID 环境变量时直接返回空串，不抛异常"""
        from agent.device_control import direct_ac_control
        with patch.dict(os.environ,
                        {"TUYA_IR_DEVICE_ID": "", "TUYA_AC_DEVICE_ID": ""},
                        clear=False):
            result = direct_ac_control("打开空调")
            assert result == ""
