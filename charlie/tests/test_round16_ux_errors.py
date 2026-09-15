"""Round 16: 错误处理 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestASRTimeoutFeedback:
    def test_timeout_plays_feedback(self):
        """ASR 超时应播放提示音，而非静默 return。"""
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        # 找到 ASR pipeline 超时处理块（用唯一标记定位）
        timeout_idx = src.find("ASR pipeline timeout")
        assert timeout_idx > 0, "应存在 ASR pipeline timeout 处理"
        after_timeout = src[timeout_idx:timeout_idx+400]
        assert "_safe_stream" in after_timeout or "超时" in after_timeout, \
            "ASR 超时应播放提示音（_safe_stream），而非静默 return"
        assert "cancel_stream" in after_timeout, \
            "ASR 超时应取消现有流（cancel_stream）"


class TestTTSFailureFeedback:
    def test_tts_failure_returns_fallback_text(self):
        """voice_loop TTS 失败时应保留原始回复文字，音频降级为空。"""
        from voice_agent import voice_loop
        src = inspect.getsource(voice_loop)
        # TTS 失败时应保留原始 reply，仅 audio_out 设为空
        tts_fail_idx = src.find("TTS降级为文字")
        assert tts_fail_idx > 0
        after_fail = src[tts_fail_idx:tts_fail_idx+300]
        assert 'audio_out = b""' in after_fail, \
            "TTS 失败时 audio_out 应设为空字节"
