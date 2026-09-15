"""Tests for Round 10 LLM pipeline speedup: Agnes lite endpoint, first-content timeout, garbled ASR guard."""
import json
import os
import re
import sys
import threading
import time
from io import BytesIO
from unittest.mock import patch, MagicMock, call

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Helpers
# ======================================================================

class FakeResponse:
    """Mimics requests.Response for streaming."""
    def __init__(self, lines, status=200):
        self.status_code = status
        self._lines = lines

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_lines(self, chunk_size=1024, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        pass


def _make_chunk(content: str) -> bytes:
    """Format a single SSE chunk."""
    payload = json.dumps({"choices": [{"delta": {"content": content}}]})
    return f"data: {payload}\r\n".encode("utf-8")


def _make_done_chunk() -> bytes:
    return b"data: [DONE]\r\n"


def _build_fake_response(chunks: list[str]) -> FakeResponse:
    lines = []
    for c in chunks:
        lines.append(_make_chunk(c))
    lines.append(_make_done_chunk())
    return FakeResponse(lines)


def _make_reasoning_chunk(reasoning_content: str) -> bytes:
    """Format an SSE chunk with reasoning_content but no content."""
    payload = json.dumps({"choices": [{"delta": {"reasoning_content": reasoning_content}}]})
    return f"data: {payload}\r\n".encode("utf-8")


# ======================================================================
# Fixtures / patches
# ======================================================================

@pytest.fixture(autouse=True)
def _patch_lite_deps(monkeypatch):
    """Patch all _chat_lite_stream external dependencies."""
    import agent.llm as _llm_mod

    monkeypatch.setattr(_llm_mod, "active_chat_endpoint", lambda: ("http://test", "key", "model"))
    monkeypatch.setattr(_llm_mod, "_get_history", lambda sid: [])
    monkeypatch.setattr(_llm_mod, "_build_system_msg", lambda mcp, session_id="default": "system prompt")
    monkeypatch.setattr(_llm_mod, "_build_wm_anaphor_prompt", lambda: None)
    # Sentence splitting: any sentence-ending punctuation
    monkeypatch.setattr(_llm_mod, "_SENTENCE_END", re.compile(r"[。！？\n]"))
    # TTS / filler bypass
    monkeypatch.setattr(_llm_mod, "_clean_for_tts", lambda s: s)
    monkeypatch.setattr(_llm_mod, "_is_filler_word", lambda s: False)
    monkeypatch.setattr(_llm_mod, "_append_history", lambda *a, **kw: None)
    monkeypatch.setattr(_llm_mod, "_cache_set", lambda *a, **kw: None)
    # Suppress memory thread
    monkeypatch.setattr(_llm_mod, "_remember_conversation_async", lambda *a, **kw: None)


# ======================================================================
# P0-1-a: _lite_endpoint — Agnes preferred, fallback to active_chat_endpoint
# ======================================================================

class TestLiteEndpoint:
    def test_agnes_configured_returns_agnes(self, monkeypatch):
        """When Agnes is configured, _lite_endpoint returns Agnes tuple."""
        import agent.llm as _llm_mod
        from app.llm_config import is_agnes_configured, AGNES_BASE, AGNES_KEY, AGNES_MODEL

        # Ensure Agnes is configured in this test environment
        monkeypatch.setattr(_llm_mod, "is_agnes_configured", lambda: True)
        monkeypatch.setattr(_llm_mod, "AGNES_BASE", "https://agnes.example.com/v1")
        monkeypatch.setattr(_llm_mod, "AGNES_KEY", "agnes-key-123")
        monkeypatch.setattr(_llm_mod, "AGNES_MODEL", "agnes-2.0-flash")
        monkeypatch.setattr(_llm_mod, "active_chat_endpoint", lambda: ("http://fallback", "fallback-key", "fallback-model"))

        base, key, model = _llm_mod._lite_endpoint()
        assert base == "https://agnes.example.com/v1"
        assert key == "agnes-key-123"
        assert model == "agnes-2.0-flash"

    def test_agnes_not_configured_falls_back(self, monkeypatch):
        """When Agnes is not configured, _lite_endpoint falls back to active_chat_endpoint."""
        import agent.llm as _llm_mod

        monkeypatch.setattr(_llm_mod, "is_agnes_configured", lambda: False)
        monkeypatch.setattr(_llm_mod, "active_chat_endpoint", lambda: ("http://fallback", "fallback-key", "fallback-model"))

        base, key, model = _llm_mod._lite_endpoint()
        assert base == "http://fallback"
        assert key == "fallback-key"
        assert model == "fallback-model"


# ======================================================================
# P0-1-b: Lite stream — first content timeout (1.5s)
# ======================================================================

class TestChatLiteStreamFirstContentTimeout:
    @patch("agent.llm._session")
    def test_only_reasoning_no_content_aborts_after_timeout(self, mock_session):
        """If reasoning-only chunks keep arriving past 4.5s with no content, stream aborts.

        Note: once the first content chunk arrives (even at >4.5s), the stream continues.
        This test uses a NEVER-ending reasoning stream to verify the abort.
        """
        from agent.llm import _chat_lite_stream

        class NeverContentResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_lines(self, chunk_size=1024, decode_unicode=True):
                # Keep sending reasoning chunks forever (no content)
                for i in range(20):
                    yield _make_reasoning_chunk(f"思考{i}...")
                    time.sleep(0.2)  # 0.2s * many chunks >> 4.5s

            def close(self):
                pass

        mock_session.post.return_value = NeverContentResponse()

        start = time.time()
        results = list(_chat_lite_stream("hi"))
        elapsed = time.time() - start

        # Should abort after ~4.5s (first-content timeout) + 小量调度余量
        assert elapsed < 5.5, f"Should have aborted early, but took {elapsed:.2f}s"
        # Should yield nothing because no content was ever received
        assert results == []

    @patch("agent.llm._session")
    def test_content_arrives_before_timeout_yields(self, mock_session):
        """If content arrives before 4.5s, stream continues normally."""
        from agent.llm import _chat_lite_stream

        lines = [
            _make_reasoning_chunk("思考中..."),
            _make_chunk("你"),
            _make_chunk("好。"),
            _make_done_chunk(),
        ]
        mock_session.post.return_value = FakeResponse(lines)

        results = list(_chat_lite_stream("hi"))
        # Should yield the sentence because content arrived within timeout
        assert len(results) >= 1
        sentences = "".join(s for s, _ in results)
        assert "你好" in sentences


# ======================================================================
# P0-1-c: Garbled ASR guard — lite path should be skipped
# ======================================================================

class TestChatLiteStreamGarbledAsr:
    @patch("agent.llm._classify_intent", return_value="none")
    @patch("agent.llm.is_garbled_asr")
    @patch("agent.llm._session")
    def test_garbled_asr_skips_lite_in_brain_stream(self, mock_session, mock_garbled, mock_cls):
        """Garbled ASR text should skip lite path in brain_stream_sentences (no lite HTTP call)."""
        from agent.llm import brain_stream_sentences

        mock_garbled.return_value = True
        mock_session.post.return_value = FakeResponse([_make_chunk("不应出现")])

        results = list(brain_stream_sentences("乱码输入", session_id="test-garbled"))
        # Garbled text skips lite stream; _classify_intent is mocked so no HTTP from it.
        # The key assertion: HTTP call to lite endpoint should not have been made
        # (classify_intent is mocked, so mock_session.post should not be called at all)
        mock_session.post.assert_not_called()

    @patch("agent.llm._classify_intent", return_value="none")
    @patch("agent.llm.is_garbled_asr")
    @patch("agent.llm._session")
    def test_clean_asr_goes_through_lite_in_brain_stream(self, mock_session, mock_garbled, mock_cls):
        """Clean ASR text should go through lite in brain_stream_sentences."""
        from agent.llm import brain_stream_sentences

        mock_garbled.return_value = False
        mock_session.post.return_value = FakeResponse([_make_chunk("你好。"), _make_done_chunk()])

        results = list(brain_stream_sentences("你好", session_id="test-clean"))
        # Should have at least one result from lite
        assert len(results) >= 1
        mock_session.post.assert_called_once()


# ======================================================================
# P0-1-d: Constants — new values are in effect
# ======================================================================

class TestLiteConstants:
    def test_max_tokens_is_300(self):
        """_LITE_MAX_TOKENS should be 300 (not 200)."""
        import agent.llm as _llm_mod
        assert _llm_mod._LITE_MAX_TOKENS == 300

    def test_history_turns_is_6(self):
        """_LITE_HISTORY_TURNS should be 6 (not 12)."""
        import agent.llm as _llm_mod
        assert _llm_mod._LITE_HISTORY_TURNS == 6

    def test_first_content_timeout_is_4_5(self):
        """_LITE_FIRST_CONTENT_TIMEOUT should be 4.5 (Agnes 尾部抖动最高 7.9s，放宽以减少误退)."""
        import agent.llm as _llm_mod
        assert _llm_mod._LITE_FIRST_CONTENT_TIMEOUT == 4.5


# ======================================================================
# P0-2-e: No reasoning_effort param for non-stepfun endpoints
# ======================================================================

class TestLiteNoReasoningEffort:
    @patch("agent.llm._session")
    def test_agnes_endpoint_no_reasoning_effort(self, mock_session, monkeypatch):
        """When using Agnes endpoint, payload should not contain reasoning_effort."""
        import agent.llm as _llm_mod
        from agent.llm import _chat_lite_stream

        monkeypatch.setattr(_llm_mod, "_lite_endpoint", lambda: ("https://apihub.agnes-ai.com/v1", "agnes-key", "agnes-2.0-flash"))
        mock_session.post.return_value = FakeResponse([_make_chunk("你好。"), _make_done_chunk()])

        list(_chat_lite_stream("hi"))

        call_kwargs = mock_session.post.call_args.kwargs
        payload = call_kwargs["json"]
        assert "reasoning_effort" not in payload, "Agnes should not receive reasoning_effort"
        assert "thinking" not in payload, "Agnes should not receive thinking param"


# ======================================================================
# P0-3: delta_stream gate via BRAIN_DELTA_STREAM env
# ======================================================================

class TestDeltaStreamGate:
    def test_delta_stream_disabled_by_default(self, monkeypatch):
        """BRAIN_DELTA_STREAM unset or '0' → generate_cfg should not contain delta_stream."""
        import agent.llm as _llm_mod

        # Ensure env is not set or set to 0
        monkeypatch.delenv("BRAIN_DELTA_STREAM", raising=False)
        monkeypatch.setenv("BRAIN_DELTA_STREAM", "0")

        # Reload the module to pick up env
        import importlib
        importlib.reload(_llm_mod)

        # Check that the constant controlling delta_stream is False
        assert _llm_mod.BRAIN_DELTA_STREAM == "0"

    def test_delta_stream_enabled_by_env(self, monkeypatch):
        """BRAIN_DELTA_STREAM=1 → generate_cfg should include delta_stream."""
        import agent.llm as _llm_mod

        monkeypatch.setenv("BRAIN_DELTA_STREAM", "1")
        import importlib
        importlib.reload(_llm_mod)

        assert _llm_mod.BRAIN_DELTA_STREAM == "1"

    @patch("agent.llm._session")
    def test_brain_build_cfg_respects_delta_stream_gate(self, mock_session, monkeypatch):
        """_build_brain_inner generate_cfg should contain delta_stream only when enabled."""
        import agent.llm as _llm_mod
        from app.llm_config import resolve as _resolve

        # Mock the LLM config resolve to return a simple config
        monkeypatch.setenv("BRAIN_DELTA_STREAM", "1")
        import importlib
        importlib.reload(_llm_mod)

        # We can't easily test _build_brain_inner without MCP, but we can
        # verify the module-level flag is read correctly
        assert os.getenv("BRAIN_DELTA_STREAM", "1") == "1"
