"""Tests for the TTS synth_mp3 fallback chain + social fast path + _LITE_MAX_TOKENS guard."""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)

import app.xiaozhi_ws as xw  # noqa: E402
import agent.llm as _llm_mod  # noqa: E402
import voice_agent  # noqa: E402
import agent.asr_tts as _asr_tts_mod  # noqa: E402


# ===== helpers =====

def _reset_finna_cooldown():
    xw._finna_cooldown_until = 0.0


def _mp3_bytes(n: int = 200) -> bytes:
    return b"x" * n


def _mock_subprocess_run(stdout: bytes = b"x" * 200, returncode: int = 0):
    """Mock subprocess.run that simulates ffmpeg converting to mp3."""
    mock_r = MagicMock()
    mock_r.stdout = stdout
    mock_r.returncode = returncode
    mock_r.stderr = b""
    return mock_r


# ===== Test class =====

class TestSynthMp3Chain:
    """当前分支顺序: Baidu → Finna → StepFun → say（macOS 离线兜底）"""

    def setup_method(self):
        _reset_finna_cooldown()

    def test_baidu_success_skips_rest(self):
        """Baidu 成功时，Finna / StepFun / say 都不应被调用。"""
        _cg = MagicMock(return_value=None)
        _cp = MagicMock()
        with patch.object(_asr_tts_mod, "_tts_baidu", return_value=_mp3_bytes(300)) as mock_baidu, \
             patch.object(_asr_tts_mod, "_tts_finna") as mock_finna, \
             patch.object(_asr_tts_mod, "_tts_stepfun") as mock_stepfun, \
             patch("subprocess.run", return_value=_mock_subprocess_run()) as mock_subp:
            result = xw._synth_mp3("测试", _cache_get=_cg, _cache_put=_cp)
            assert len(result) > 100
            mock_baidu.assert_called_once_with("测试")
            mock_finna.assert_not_called()
            mock_stepfun.assert_not_called()
            # say is invoked via subprocess.run, check it was not "say"
            for call in mock_subp.call_args_list:
                assert "say" not in call.args[0]

    def test_baidu_and_finna_fail_stepfun_takes_over(self):
        """Baidu + Finna 都失败时，_tts_stepfun 接管且 say 不调用。"""
        _cg = MagicMock(return_value=None)
        _cp = MagicMock()
        with patch.object(_asr_tts_mod, "_tts_baidu", side_effect=RuntimeError("baidu down")) as mock_baidu, \
             patch.object(_asr_tts_mod, "_tts_stepfun", return_value=_mp3_bytes(300)) as mock_stepfun, \
             patch("subprocess.run", return_value=_mock_subprocess_run()) as mock_subp:
            _reset_finna_cooldown()
            # Make finna_ok True and force requests.post to raise so Finna fails
            with patch.object(xw.os, "getenv", return_value="fake-key"), \
                 patch("requests.post", side_effect=RuntimeError("finna down")):
                result = xw._synth_mp3("测试", _cache_get=_cg, _cache_put=_cp)
            assert len(result) > 100
            mock_baidu.assert_called_once_with("测试")
            mock_stepfun.assert_called_once_with("测试")
            for call in mock_subp.call_args_list:
                assert "say" not in call.args[0]

    def test_all_network_fail_say_fallback(self):
        """全部网络 TTS 失败时，say 兜底返回非空 bytes。"""
        _cg = MagicMock(return_value=None)
        _cp = MagicMock()
        _say_mp3 = _mp3_bytes(200)
        with patch.object(_asr_tts_mod, "_tts_baidu", side_effect=RuntimeError("baidu down")), \
             patch.object(_asr_tts_mod, "_tts_stepfun", side_effect=RuntimeError("stepfun down")), \
             patch("subprocess.run", side_effect=[
                 # Baidu branch ffmpeg call
                 _mock_subprocess_run(),
                 # say raw output (first call in say branch)
                 MagicMock(stdout=_say_mp3, returncode=0, stderr=b""),
                 # say ffmpeg conversion
                 _mock_subprocess_run(stdout=_say_mp3),
             ]) as mock_subp:
            _reset_finna_cooldown()
            with patch.object(xw.os, "getenv", return_value="fake-key"), \
                 patch("requests.post", side_effect=RuntimeError("finna down")):
                result = xw._synth_mp3("测试", _cache_get=_cg, _cache_put=_cp)
            assert result
            assert len(result) > 100


class TestLiteMaxTokens:
    """回归守护：_LITE_MAX_TOKENS 必须保持 200。"""

    def test_lite_max_tokens_is_200(self):
        assert _llm_mod._LITE_MAX_TOKENS == 200


class TestSocialReplyHandler:
    """社交礼貌语快路径"""

    def test_thanks_returns_bukeqi(self):
        assert voice_agent._social_reply_handler("谢谢") == "不客气。"

    def test_goodbye_returns_zai_jian(self):
        assert voice_agent._social_reply_handler("再见") == "再见，随时叫我。"

    def test_compound_sentence_falls_through(self):
        """P0 回归：含真实意图的复合句必须回退（返回 None），不得被固定回复吞掉。"""
        assert voice_agent._social_reply_handler("谢谢，今天天气怎么样") is None
        assert voice_agent._social_reply_handler("谢谢提醒我开会") is None
        assert voice_agent._social_reply_handler("多谢啦顺便问一下明天冷不冷") is None

    def test_polite_suffix_still_hits(self):
        """礼貌后缀变体仍命中。"""
        assert voice_agent._social_reply_handler("谢谢啦！") == "不客气。"
        assert voice_agent._social_reply_handler("好的，拜拜。") == "再见，随时叫我。"
