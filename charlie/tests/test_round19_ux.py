"""Round 19: barge-in 风暴提示 + MQTT TTS 兜底 + AC 超时优化 回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestBargeInStormNotification:
    """barge-in 风暴 goodbye 前应发送提示文字，避免用户困惑。"""

    def test_stt_notification_before_goodbye(self):
        """风暴判定后、goodbye 前应发送 stt 文字提示。"""
        ws_path = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
        src = open(ws_path).read()
        # 定位风暴守卫代码块
        storm_idx = src.find("判定背景音干扰")
        assert storm_idx > 0, "应存在 barge-in 风暴守卫逻辑"
        # 风暴块到 goodbye 之间应有 stt 文字提示
        goodbye_idx = src.find('{"type": "goodbye"}', storm_idx)
        assert goodbye_idx > 0
        storm_block = src[storm_idx:goodbye_idx]
        assert '"stt"' in storm_block, \
            "goodbye 前应发送 stt 文字提示，让用户知道为何退出"
        assert "背景音" in storm_block, \
            "提示文案应包含'背景音'，解释退出原因"

    def test_goodbye_still_sent_after_notification(self):
        """提示文字发送后仍应发送 goodbye 并关闭连接。"""
        ws_path = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
        src = open(ws_path).read()
        storm_idx = src.find("判定背景音干扰")
        goodbye_idx = src.find('{"type": "goodbye"}', storm_idx)
        assert goodbye_idx > 0, "风暴后仍应发送 goodbye"
        # goodbye 后应有 ws.close()
        close_idx = src.find("ws.close()", goodbye_idx)
        assert close_idx > 0 and close_idx - goodbye_idx < 200, \
            "goodbye 后应关闭 WebSocket"


class TestMqttTtsFallbackText:
    """MQTT 兜底 TTS 失败时应下发文字提示，避免设备完全沉默。"""

    def test_fallback_text_on_tts_none(self):
        """_push_text_tts 在 mp3 为 None 时应下发 stt 文字。"""
        mqtt_path = os.path.join(PROJECT_DIR, "app", "mqtt_server.py")
        src = open(mqtt_path).read()
        func_idx = src.find("def _push_text_tts")
        assert func_idx > 0
        # 截取函数体（到下一个 def 或缩进回退）
        func_body = src[func_idx:func_idx + 600]
        # mp3 为 None 时应调用 _publish_stt 下发文字
        assert "_publish_stt" in func_body, \
            "_push_text_tts 在 mp3 为 None 时应调用 _publish_stt 下发文字提示"
        assert "语音合成暂时不可用" in func_body, \
            "兜底文案应包含'语音合成暂时不可用'"

    def test_exception_also_publishes_text(self):
        """_push_text_tts 异常分支也应下发文字提示。"""
        mqtt_path = os.path.join(PROJECT_DIR, "app", "mqtt_server.py")
        src = open(mqtt_path).read()
        func_idx = src.find("def _push_text_tts")
        func_body = src[func_idx:func_idx + 800]
        # except 分支也应有 _publish_stt
        except_idx = func_body.find("except Exception as e:")
        assert except_idx > 0
        except_block = func_body[except_idx:]
        assert "_publish_stt" in except_block, \
            "异常分支也应下发 _publish_stt 文字提示"


class TestAcTimeoutOptimization:
    """AC 控制 API 超时应从 15s 降至 8s，减少用户等待。"""

    def test_default_timeout_le_8(self):
        """_run_ac_with_timeout 默认超时不应超过 8 秒。"""
        dc_path = os.path.join(PROJECT_DIR, "agent", "device_control.py")
        src = open(dc_path).read()
        func_idx = src.find("def _run_ac_with_timeout")
        assert func_idx > 0
        # 提取函数签名行
        sig_end = src.find("\n", func_idx)
        sig = src[func_idx:sig_end]
        assert "timeout" in sig
        # 默认值不应超过 8.0
        assert "15.0" not in sig, \
            "AC 控制默认超时应从 15s 降低，避免用户长时间等待"
        assert "8.0" in sig or "8" in sig, \
            "AC 控制默认超时应降至 8s"

    def test_timeout_message_friendly(self):
        """超时提示文案应对用户友好。"""
        dc_path = os.path.join(PROJECT_DIR, "agent", "device_control.py")
        src = open(dc_path).read()
        func_idx = src.find("def _run_ac_with_timeout")
        func_body = src[func_idx:func_idx + 400]
        assert "超时" in func_body, \
            "超时文案应包含'超时'说明"
        assert "稍后" in func_body, \
            "超时文案应建议用户稍后重试"

    def test_all_call_sites_use_le_8s(self):
        """所有 _run_ac_with_timeout 调用点的 timeout 不应超过 8 秒。"""
        dc_path = os.path.join(PROJECT_DIR, "agent", "device_control.py")
        src = open(dc_path).read()
        # 查找所有 _run_ac_with_timeout 调用（排除 def 行）
        import re
        calls = re.findall(r'_run_ac_with_timeout\([^)]*timeout=(\d+\.?\d*)', src)
        assert len(calls) >= 2, \
            f"应存在至少 2 个 _run_ac_with_timeout 调用（关机+开机），实际 {len(calls)}"
        for t in calls:
            assert float(t) <= 8.0, \
                f"调用点 timeout={t} 超过 8 秒，用户等待时间过长"
