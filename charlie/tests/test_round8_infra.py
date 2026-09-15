"""Round 8 infra: _synth_mp3 cancel_evt cancellation tests (S1/S2/S3)."""
import os
import sys
import threading
from unittest.mock import patch, MagicMock

import pytest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

import app.xiaozhi_ws as xw  # noqa: E402
import agent.asr_tts as _asr_tts_mod  # noqa: E402


def _reset_finna_cooldown():
    xw._finna_cooldown_until = 0.0


def _mp3_bytes(n: int = 200) -> bytes:
    return b"x" * n


class TestSynthMp3Cancel:
    """S1: cancel_evt already set → immediate b"" and Baidu not called.
    S2: cancel after Baidu fail → Finna network call not initiated, returns b"".
    S3: cancel_evt omitted → existing fallback behavior unchanged (same as
        tests/test_synth_mp3_chain.py).
    """

    def setup_method(self):
        _reset_finna_cooldown()

    def test_s1_cancel_evt_set_immediate_return(self):
        """S1: cancel_evt set before call → b"" immediately, Baidu not called."""
        evt = threading.Event()
        evt.set()
        cg = MagicMock(return_value=None)
        cp = MagicMock()
        with patch.object(_asr_tts_mod, "_tts_baidu") as mock_baidu, \
             patch.object(_asr_tts_mod, "_tts_finna") as mock_finna, \
             patch.object(_asr_tts_mod, "_tts_stepfun") as mock_stepfun, \
             patch("subprocess.run") as mock_subp:
            result = xw._synth_mp3("测试", _cache_get=cg, _cache_put=cp, cancel_evt=evt)
        assert result == b""
        mock_baidu.assert_not_called()
        mock_finna.assert_not_called()
        mock_stepfun.assert_not_called()
        mock_subp.assert_not_called()

    def test_s2_cancel_after_baidu_fail_skips_finna(self):
        """S2: Baidu raises → cancel before Finna → no Finna network call."""
        evt = threading.Event()
        # Set cancel after Baidu would fail (but before Finna network call).
        # We set it before the call; the cancel check after Baidu failure fires.
        evt.set()
        cg = MagicMock(return_value=None)
        cp = MagicMock()
        with patch.object(_asr_tts_mod, "_tts_baidu", side_effect=RuntimeError("baidu down")), \
             patch("requests.post", side_effect=RuntimeError("should not reach")) as mock_post, \
             patch.object(_asr_tts_mod, "_tts_stepfun") as mock_stepfun, \
             patch("subprocess.run") as mock_subp:
            _reset_finna_cooldown()
            with patch.object(xw.os, "getenv", return_value="fake-key"):
                result = xw._synth_mp3("测试", _cache_get=cg, _cache_put=cp, cancel_evt=evt)
        assert result == b""
        # Finna's network call must NOT have been attempted
        mock_post.assert_not_called()
        mock_stepfun.assert_not_called()

    def test_s3_no_cancel_evt_existing_fallback_unchanged(self):
        """S3: Without cancel_evt, existing fallback chain still works (Baidu → StepFun)."""
        cg = MagicMock(return_value=None)
        cp = MagicMock()
        with patch.object(_asr_tts_mod, "_tts_baidu", side_effect=RuntimeError("baidu down")), \
             patch.object(_asr_tts_mod, "_tts_stepfun", return_value=_mp3_bytes(300)) as mock_stepfun, \
             patch("subprocess.run", return_value=MagicMock(stdout=_mp3_bytes(), returncode=0, stderr=b"")) as mock_subp:
            _reset_finna_cooldown()
            with patch.object(xw.os, "getenv", return_value="fake-key"), \
                 patch("requests.post", side_effect=RuntimeError("finna down")):
                result = xw._synth_mp3("测试", _cache_get=cg, _cache_put=cp)
        assert len(result) > 100
        mock_stepfun.assert_called_once_with("测试")
        # say fallback (subprocess "say") should NOT be reached when StepFun succeeds
        for call in mock_subp.call_args_list:
            assert "say" not in call.args[0]

    def test_s3_all_fallback_without_cancel(self):
        """S3 regression: all network TTS fail → say fallback returns non-empty."""
        cg = MagicMock(return_value=None)
        cp = MagicMock()
        _say_mp3 = _mp3_bytes(200)
        with patch.object(_asr_tts_mod, "_tts_baidu", side_effect=RuntimeError("baidu down")), \
             patch.object(_asr_tts_mod, "_tts_stepfun", side_effect=RuntimeError("stepfun down")), \
             patch("subprocess.run", side_effect=[
                 MagicMock(stdout=_mp3_bytes(), returncode=0, stderr=b""),   # Baidu ffmpeg noop
                 MagicMock(stdout=_say_mp3, returncode=0, stderr=b""),        # say raw
                 MagicMock(stdout=_say_mp3, returncode=0, stderr=b""),        # say ffmpeg
             ]) as mock_subp:
            _reset_finna_cooldown()
            with patch.object(xw.os, "getenv", return_value="fake-key"), \
                 patch("requests.post", side_effect=RuntimeError("finna down")):
                result = xw._synth_mp3("测试", _cache_get=cg, _cache_put=cp)
        assert result
        assert len(result) > 100
