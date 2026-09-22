"""StepFun 多 Key 轮询路由（agent/stepfun_keys.py）。

2026-09-19 决策：STEPFUN_KEY 支持逗号分隔多把，TTS/ASR/LLM/realtime 共用
一个本地轮询路由器；429 短冷却、401/403 长隔离、全冷却兜底不阻塞。
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from agent import stepfun_keys as sk  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_router():
    sk._reset_for_tests()
    yield
    sk._reset_for_tests()


class TestKeyParsing:
    def test_single_key_unchanged(self):
        """单把 key（老写法）解析为长度 1 的列表，行为不变。"""
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-only"}):
            assert sk.stepfun_keys() == ["sk-only"]
            assert sk.has_keys() is True

    def test_multi_key_comma_separated(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a, sk-b,sk-c"}):
            assert sk.stepfun_keys() == ["sk-a", "sk-b", "sk-c"]

    def test_empty_and_dedup(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,,sk-a,  ,sk-b"}):
            assert sk.stepfun_keys() == ["sk-a", "sk-b"]

    def test_missing_key(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": ""}):
            assert sk.stepfun_keys() == []
            assert sk.has_keys() is False
            assert sk.next_key() == ""


class TestRoundRobin:
    def test_round_robin_order(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b,sk-c"}):
            got = [sk.next_key() for _ in range(6)]
            assert got == ["sk-a", "sk-b", "sk-c", "sk-a", "sk-b", "sk-c"]

    def test_cooled_key_skipped(self):
        """429 冷却中的 key 被轮询跳过；全冷却兜底不阻塞；过期后恢复轮询。"""
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b"}):
            assert sk.next_key() == "sk-a"
            sk.mark_rate_limited("sk-b")
            assert sk.next_key() == "sk-a"  # b 冷却 → 跳回 a
            sk.mark_rate_limited("sk-a")
            assert sk.next_key() in ("sk-a", "sk-b")  # 全冷却 → 兜底不阻塞
            # 冷却过期后恢复轮询
            with patch.object(sk.time, "monotonic", return_value=time.monotonic() + 61):
                assert sk.next_key() == "sk-a"

    def test_auth_failed_long_cooldown(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b"}), \
             patch.object(sk, "_auth_cooldown_seconds", return_value=1800.0):
            sk.mark_auth_failed("sk-a")
            # a 被长隔离，后续全走 b
            assert sk.next_key() == "sk-b"
            assert sk.next_key() == "sk-b"

    def test_mark_from_status_mapping(self):
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b"}):
            sk.mark_from_status("sk-a", 429)
            assert sk.keys_status()[0]["cooling"] is True
            sk._reset_for_tests()
            sk.mark_from_status("sk-a", 401)
            assert sk.keys_status()[0]["cooling"] is True
            sk._reset_for_tests()
            sk.mark_from_status("sk-a", 500)  # 其他状态不打标
            assert sk.keys_status()[0]["cooling"] is False

    def test_status_never_leaks_key(self):
        """keys_status 只给尾 4 位，不回传明文。"""
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-abcdef123456"}):
            st = sk.keys_status()
            assert st[0]["key_tail"] == "3456"
            assert "sk-abcdef123456" not in str(st)


class TestWiring:
    def test_tts_stepfun_uses_next_key_and_marks_429(self):
        """_tts_stepfun 从路由器取 key；429 响应把该 key 打标冷却。"""
        from agent import asr_tts
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b"}):
            resp = MagicMock(status_code=429, text=b"rate limited")
            with patch.object(asr_tts, "_stepfun_keys") as mock_router, \
                 patch("voice_agent._session") as mock_session:
                mock_router.has_keys.return_value = True
                mock_router.next_key.return_value = "sk-a"
                mock_session.post.return_value = MagicMock(status_code=429, text="rate limited")
                with pytest.raises(RuntimeError, match="429"):
                    asr_tts._tts_stepfun("测试")
                mock_router.mark_from_status.assert_called_once_with("sk-a", 429)
                hdr = mock_session.post.call_args.kwargs["headers"]["Authorization"]
                assert hdr == "Bearer sk-a"

    def test_tts_stepfun_no_key_raises(self):
        from agent import asr_tts
        with patch.dict(os.environ, {"STEPFUN_KEY": ""}):
            with pytest.raises(RuntimeError, match="未配置"):
                asr_tts._tts_stepfun("测试")

    def test_llm_provider_cfg_rotates(self):
        """LLM 配置解析 stepfun key 时走轮询路由；其他 provider 不受影响。"""
        from app import llm_config
        with patch.dict(os.environ, {"STEPFUN_KEY": "sk-a,sk-b"}):
            k1 = llm_config._get_provider_cfg("stepfun")["key"]
            k2 = llm_config._get_provider_cfg("stepfun")["key"]
            assert {k1, k2} == {"sk-a", "sk-b"}

    def test_verify_stepfun_key_uses_first_only(self):
        """管理页校验多 key 配置时只打第一把，不把整串当 key 发出去。"""
        import app.routes.manage as manage
        with patch.object(manage, "_verify_openai_compat_key", return_value=(True, "ok")) as mock_v:
            ok, msg = manage._verify_stepfun_key("sk-a,sk-b,sk-c")
        assert ok is True
        assert mock_v.call_args.args[0] == "sk-a"

    def test_realtime_is_configured_honors_router(self):
        """realtime 开关判断走路由器（多把 key 视为已配置）。"""
        from agent import realtime_tts
        with patch.dict(os.environ, {"STEPAUDIO3_RT_ENABLED": "1",
                                     "STEPFUN_KEY": "sk-a,sk-b"}):
            assert realtime_tts.is_configured() is True
        with patch.dict(os.environ, {"STEPAUDIO3_RT_ENABLED": "1",
                                     "STEPFUN_KEY": ""}):
            assert realtime_tts.is_configured() is False
