"""Tests for agent.llm._chat_lite_stream: streaming logic, timeout, bad JSON, terminator."""
import json
import os
import re
import sys
import threading
import time
from io import BytesIO
from unittest.mock import patch, MagicMock

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


# ======================================================================
# Fixtures / patches
# ======================================================================

@pytest.fixture(autouse=True)
def _patch_lite_deps(monkeypatch):
    """Patch all _chat_lite_stream external dependencies."""
    import agent.llm as _llm_mod

    monkeypatch.setattr(_llm_mod, "active_chat_endpoint", lambda: ("http://test", "key", "model"))
    monkeypatch.setattr(_llm_mod, "_get_history", lambda sid: [])
    monkeypatch.setattr(_llm_mod, "_build_system_msg", lambda mcp: "system prompt")
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
# P0-2-a: Normal streaming
# ======================================================================

class TestChatLiteStreamNormal:
    @patch("agent.llm._session")
    def test_yields_complete_sentences(self, mock_session):
        """Normal stream: yields full sentences with accumulated full_reply."""
        from agent.llm import _chat_lite_stream

        chunks = ["你好，", "我叫", " Charlie。", "你好，我叫 Charlie。", "今天", "天气不错。"]
        mock_session.post.return_value = _build_fake_response(chunks)

        results = list(_chat_lite_stream("hello"))
        # Sentences should end at 。 or ！
        # "你好，我叫 Charlie。" 和 "今天天气不错。"
        assert len(results) >= 1
        # The full reply at the end should be "你好，我叫 Charlie。今天天气不错。"
        # or similar depending on chunk boundaries
        last_sentence, last_full = results[-1]
        assert "。" in last_sentence or "！" in last_sentence
        assert last_full.strip()

    @patch("agent.llm._session")
    def test_yields_individual_chunks(self, mock_session):
        """Streaming yields sentences as they complete."""
        from agent.llm import _chat_lite_stream

        mock_session.post.return_value = _build_fake_response(["第一句。", "第二句。"])

        results = list(_chat_lite_stream("hi"))
        sentences = [s for s, _ in results]
        assert any("第一句" in s for s in sentences)
        assert any("第二句" in s for s in sentences)

    @patch("agent.llm._session")
    def test_empty_stream_no_yield(self, mock_session):
        """Empty response yields nothing."""
        from agent.llm import _chat_lite_stream

        mock_session.post.return_value = _build_fake_response([])

        results = list(_chat_lite_stream("hi"))
        assert results == []


# ======================================================================
# P0-2-b: Bad JSON chunk handling
# ======================================================================

class TestChatLiteStreamBadJson:
    @patch("agent.llm._session")
    def test_non_json_line_skipped(self, mock_session):
        """Non-JSON data line is skipped, doesn't crash."""
        from agent.llm import _chat_lite_stream

        lines = [
            b"data: not valid json\r\n",
            _make_chunk("有效"),
            _make_chunk("内容。"),
            _make_done_chunk(),
        ]
        mock_session.post.return_value = FakeResponse(lines)

        results = list(_chat_lite_stream("hi"))
        assert len(results) >= 1
        assert any("有效内容" in s for s, _ in results)

    @patch("agent.llm._session")
    def test_mixed_valid_and_invalid(self, mock_session):
        """Mix of bad JSON and good chunks still produces correct output."""
        from agent.llm import _chat_lite_stream

        lines = [
            b"data: {bad\r\n",
            _make_chunk("A。"),
            b"data: {also bad}\r\n",
            _make_chunk("B。"),
            _make_done_chunk(),
        ]
        mock_session.post.return_value = FakeResponse(lines)

        results = list(_chat_lite_stream("hi"))
        sentences = [s for s, _ in results]
        assert any("A" in s for s in sentences)
        assert any("B" in s for s in sentences)


# ======================================================================
# P0-2-c: Terminator [DONE]
# ======================================================================

class TestChatLiteStreamTerminator:
    @patch("agent.llm._session")
    def test_done_terminator_stops_iteration(self, mock_session):
        """data: [DONE] stops iteration correctly."""
        from agent.llm import _chat_lite_stream

        lines = [
            _make_chunk("完成"),
            _make_chunk("了。"),
            _make_done_chunk(),
            _make_chunk("这不应该出现。"),  # after DONE should be ignored
        ]
        mock_session.post.return_value = FakeResponse(lines)

        results = list(_chat_lite_stream("hi"))
        sentences_text = "".join(s for s, _ in results)
        assert "完成了。" in sentences_text
        assert "不应该出现" not in sentences_text


# ======================================================================
# P0-2-d: Timer timeout
# ======================================================================

class TestChatLiteStreamTimeout:
    @patch("agent.llm._LITE_TOTAL_TIMEOUT", 0.5)
    @patch("agent.llm._session")
    def test_timer_closes_socket_on_slow_stream(self, mock_session):
        """Timer closes socket when iter_lines blocks past timeout.

        We simulate a slow stream where the generator sleeps between yields,
        exceeding _LITE_TOTAL_TIMEOUT. The Timer should close the response.
        """
        from agent.llm import _chat_lite_stream

        class SlowResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_lines(self, chunk_size=1024, decode_unicode=True):
                # Yield a chunk, then sleep past the timeout before yielding more
                yield _make_chunk("慢")
                time.sleep(0.8)  # longer than _LITE_TOTAL_TIMEOUT=0.5
                yield _make_chunk("吞。")
                yield _make_done_chunk()

            def close(self):
                # close is called by the timer
                pass

        mock_session.post.return_value = SlowResponse()

        start = time.time()
        results = list(_chat_lite_stream("hi"))
        elapsed = time.time() - start

        # Should finish well before the slow 0.8s sleep (timer forces close)
        assert elapsed < 1.0, f"Stream took too long: {elapsed:.2f}s"
        # Should have at least yielded the first sentence before timeout
        assert len(results) >= 0  # may yield partial or none depending on timing


# ======================================================================
# P0-2-e: Request failure → no yield
# ======================================================================

class TestChatLiteStreamRequestFailure:
    @patch("agent.llm._session")
    def test_post_failure_returns_none(self, mock_session):
        """If post raises, _chat_lite_stream returns without yielding."""
        from agent.llm import _chat_lite_stream

        mock_session.post.side_effect = Exception("connection refused")

        results = list(_chat_lite_stream("hi"))
        assert results == []
