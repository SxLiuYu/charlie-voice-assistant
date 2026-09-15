"""Round 14: 语音链路 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestVADSilenceLimit:
    def test_vad_flag_not_internal_var(self):
        """VAD 端点检测应依赖 VAD_ENABLED 标志，而非 _silero_vad 内部变量。"""
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        # 找到 silence_limit 赋值行
        assert "VAD_ENABLED and _load_silero_vad()" in src, \
            "silence_limit 应使用 VAD_ENABLED and _load_silero_vad() 而非 _silero_vad is not None"
        # 确保旧的脆弱写法不再存在
        assert "SILENCE_FRAMES_VAD if _silero_vad is not None" not in src, \
            "不应再使用 _silero_vad is not None 作为 VAD 判定条件"

    def test_vad_silence_frames_default(self):
        """VAD 启用时端点检测应 6 帧 = 0.36s，远快于 RMS 1.2s。"""
        from app.xiaozhi_ws import SILENCE_FRAMES_VAD, SILENCE_FRAMES
        assert SILENCE_FRAMES_VAD <= 8, f"VAD 静音帧应 ≤8（0.48s），实际 {SILENCE_FRAMES_VAD}"
        assert SILENCE_FRAMES_VAD < SILENCE_FRAMES, \
            "VAD 静音帧应少于 RMS 回退帧"


class TestWakeBeepNoThreadDispatch:
    def test_beep_no_to_thread(self):
        """唤醒 beep 是缓存命中，不应使用 asyncio.to_thread 派发。"""
        from app import xiaozhi_ws
        src = inspect.getsource(xiaozhi_ws)
        # 确保 wake beep 判定不再用 to_thread
        assert "to_thread(_synth_wake_beep" not in src, \
            "_synth_wake_beep 是缓存命中，不应 asyncio.to_thread 派发"


class TestFirstSentenceTimeout:
    def test_timeout_not_too_long(self):
        """首句超时应 ≤10s，避免系统卡死时长沉默。"""
        from app.xiaozhi_ws import _FIRST_SENTENCE_TIMEOUT
        assert _FIRST_SENTENCE_TIMEOUT <= 10.0, \
            f"首句超时应 ≤10s，实际 {_FIRST_SENTENCE_TIMEOUT}s"
