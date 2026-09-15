"""Round 12: LLM lite 通道精简 system msg + 飞书 msg_id NameError 修复回归测试."""
import json
import os
import re
import sys
import time
import logging
import types
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
    payload = json.dumps({"choices": [{"delta": {"content": content}}]})
    return f"data: {payload}\r\n".encode("utf-8")


def _make_done_chunk() -> bytes:
    return b"data: [DONE]\r\n"


def _build_fake_response(chunks: list[str]) -> FakeResponse:
    lines = [_make_chunk(c) for c in chunks]
    lines.append(_make_done_chunk())
    return FakeResponse(lines)


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _patch_lite_deps(monkeypatch):
    """Patch _chat_lite_stream external dependencies (system msg returns a fixed string)."""
    import agent.llm as _llm_mod

    monkeypatch.setattr(_llm_mod, "active_chat_endpoint", lambda: ("http://test", "key", "model"))
    monkeypatch.setattr(_llm_mod, "_get_history", lambda sid: [])
    # Patch BOTH variants so tests calling _chat_lite_stream always get a fixed system msg
    monkeypatch.setattr(_llm_mod, "_build_system_msg", lambda mcp, session_id="default": "FULL SYSTEM PROMPT")
    monkeypatch.setattr(_llm_mod, "_build_lite_system_msg", lambda mcp, session_id="default": "LITE SYSTEM PROMPT")
    monkeypatch.setattr(_llm_mod, "_build_wm_anaphor_prompt", lambda: None)
    monkeypatch.setattr(_llm_mod, "_SENTENCE_END", re.compile(r"[。！？\n]"))
    monkeypatch.setattr(_llm_mod, "_clean_for_tts", lambda s: s)
    monkeypatch.setattr(_llm_mod, "_is_filler_word", lambda s: False)
    monkeypatch.setattr(_llm_mod, "_append_history", lambda *a, **kw: None)
    monkeypatch.setattr(_llm_mod, "_cache_set", lambda *a, **kw: None)
    monkeypatch.setattr(_llm_mod, "_remember_conversation_async", lambda *a, **kw: None)


def _install_fake_lark_oapi():
    """Install minimal fake lark_oapi so feishu_ws can be imported."""
    fake = types.ModuleType("lark_oapi")
    fake_ws = types.ModuleType("lark_oapi.ws")
    fake_ws_client = types.ModuleType("lark_oapi.ws.client")
    fake_api = types.ModuleType("lark_oapi.api")
    fake_im = types.ModuleType("lark_oapi.api.im")
    fake_im_v1 = types.ModuleType("lark_oapi.api.im.v1")

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def start(self): pass

    class FakeEventHandler:
        @classmethod
        def builder(cls, *a, **kw): return cls()
        def register_p2_im_message_receive_v1(self, handler): return self
        def build(self): return self

    class FakeLogLevel:
        INFO = "INFO"

    fake_ws_client.Client = FakeClient
    fake_ws.client = fake_ws_client
    fake_ws.client.Client = FakeClient
    fake.EventDispatcherHandler = FakeEventHandler
    fake.LogLevel = FakeLogLevel
    fake_api.im = fake_im
    fake_im.v1 = fake_im_v1
    fake_im_v1.P2ImMessageReceiveV1 = type("P2ImMessageReceiveV1", (), {})

    sys.modules["lark_oapi"] = fake
    sys.modules["lark_oapi.ws"] = fake_ws
    sys.modules["lark_oapi.ws.client"] = fake_ws_client
    sys.modules["lark_oapi.api"] = fake_api
    sys.modules["lark_oapi.api.im"] = fake_im
    sys.modules["lark_oapi.api.im.v1"] = fake_im_v1


@pytest.fixture(autouse=True)
def _fresh_feishu_state(monkeypatch):
    """Reset feishu_ws module-level state between tests."""
    monkeypatch.setenv("FEISHU_APP_ID", "test_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test_secret")
    _install_fake_lark_oapi()
    for mod_name in list(sys.modules):
        if mod_name == "feishu_ws" or mod_name.startswith("feishu_ws."):
            del sys.modules[mod_name]
    import feishu_token
    with feishu_token._lock:
        feishu_token._cache["token"] = ""
        feishu_token._cache["at"] = 0.0
    import feishu_ws
    feishu_ws._started = False
    yield
    feishu_ws._started = False


# ======================================================================
# R12-1: Lite system msg length and content
# ======================================================================

class TestLiteSystemMsg:
    """_build_lite_system_msg 应显著短于完整版且不含工具文档。"""

    def test_lite_system_msg_under_400_chars(self):
        """Lite system msg 长度 < 400 字符。"""
        from agent.system_msg import _build_lite_system_msg, _build_system_msg

        lite = _build_lite_system_msg("none")
        assert len(lite) < 400, f"Lite system msg 过长: {len(lite)} 字符"

    def test_lite_system_msg_shorter_than_full(self):
        """Lite system msg 应比完整版明显更短（短至少 500 字符）。"""
        from agent.system_msg import _build_lite_system_msg, _build_system_msg

        lite = _build_lite_system_msg("none")
        full = _build_system_msg("none")
        assert len(lite) < len(full) - 500, (
            f"Lite 不够短: lite={len(lite)}, full={len(full)}"
        )

    def test_lite_system_msg_no_mcp_tool_doc(self):
        """Lite system msg 不应包含 MCP 工具文档特征词。"""
        from agent.system_msg import _build_lite_system_msg

        lite = _build_lite_system_msg("none")
        # 不应出现具体工具名
        assert "get_detailed_weather" not in lite
        assert "play_music" not in lite
        assert "add_reminder" not in lite
        assert "search_docs" not in lite
        # 不应出现 "可用工具" 段落
        assert "可用工具" not in lite
        # 不应出现 MCP 工具集中常见的描述词（如"调用工具"）
        assert "调用工具获取真实数据" not in lite

    def test_lite_system_msg_contains_core_rules(self):
        """Lite system msg 应保留核心回复规则（直接说答案/禁止占用语）。"""
        from agent.system_msg import _build_lite_system_msg

        lite = _build_lite_system_msg("none")
        assert "直接说答案" in lite or "第一句就是答案" in lite
        # 默认角色 lite 版本已精简，检查通用规则关键词
        assert "禁止输出占用语" in lite or "答完即止" in lite


# ======================================================================
# R12-2: _chat_lite_stream uses lite system msg in payload
# ======================================================================

class TestChatLiteStreamUsesLiteSystemMsg:
    """_chat_lite_stream 的 HTTP payload 应携带 lite 版 system msg。"""

    @patch("agent.llm._session")
    def test_payload_contains_lite_system_msg(self, mock_session):
        """HTTP POST payload 的 system 内容来自 _build_lite_system_msg，而非完整版。"""
        from agent.llm import _chat_lite_stream

        mock_session.post.return_value = _build_fake_response(["你好。"])

        results = list(_chat_lite_stream("hi"))
        assert len(results) >= 1

        call_kwargs = mock_session.post.call_args.kwargs
        payload = call_kwargs["json"]
        messages = payload["messages"]

        # 应存在 system 消息且内容为 lite 版（即 "LITE SYSTEM PROMPT"）
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert system_msgs, "payload 中应有 system 消息"
        system_contents = [m["content"] for m in system_msgs]
        assert "LITE SYSTEM PROMPT" in system_contents, (
            f"payload system 内容应来自 _build_lite_system_msg，实际: {system_contents}"
        )
        # 不应包含完整版标记
        assert not any("FULL SYSTEM PROMPT" in c for c in system_contents)


# ======================================================================
# R12-3: Timeout constants new values
# ======================================================================

class TestLiteTimeoutConstants:
    """超时参数（按 2026-08-22 实测数据调优后的当前值）：
    first-content 8.0s（Agnes 尾部抖动最高 7.9s，8s 几乎不误杀）；
    total 默认 12s（LITE_TOTAL_TIMEOUT 可覆盖）。"""

    def test_first_content_timeout_is_8(self):
        import agent.llm as _llm_mod
        assert _llm_mod._LITE_FIRST_CONTENT_TIMEOUT == 8.0

    def test_total_timeout_default_is_12(self):
        import agent.llm as _llm_mod
        assert _llm_mod._LITE_TOTAL_TIMEOUT == 12


# ======================================================================
# R12-4: feishu msg_id — warning log 含 msg_id 且不抛 NameError
# ======================================================================

class TestFeishuMsgIdLogging:
    """_process_message_sync 异常路径应正确打出含 msg_id 的 warning，不再 NameError。"""

    def test_exception_path_logs_msg_id_no_nameerror(self, caplog):
        """构造 brain 抛异常 + _send_text_reply 首次调用即抛异常 → warning 日志含 msg_id。"""
        import feishu_ws
        import agent.llm as _llm_mod

        test_msg_id = "test-msg-id-abc123"
        test_text = "用户消息内容"
        test_sender = "ou_test_user"
        test_chat = "oc_test_chat"

        # brain_stream_sentences 在 _process_message_sync 内部惰性 import，
        # 必须在 reload feishu_ws 之前 patch agent.llm.brain_stream_sentences
        with patch.object(_llm_mod, "brain_stream_sentences", side_effect=RuntimeError("brain down")):
            # reload 使 feishu_ws 内部 from agent.llm import brain_stream_sentences
            # 拿到已被 patch 的对象
            import importlib
            importlib.reload(feishu_ws)

            with caplog.at_level(logging.WARNING, logger="magic"):
                # _send_text_reply 首次调用即抛异常 → 触发外层 except → 打 warning
                def fake_send(chat_id, text):
                    raise RuntimeError("send boom")

                with patch.object(feishu_ws, "_send_text_reply", side_effect=fake_send):
                    feishu_ws._process_message_sync(test_text, test_sender, test_chat, test_msg_id)

        # 应出现含 msg_id 的 warning 日志，不应出现 NameError
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        msg_id_warnings = [r for r in warning_records if test_msg_id in r.getMessage()]
        assert msg_id_warnings, (
            f"warning 日志应含 msg_id={test_msg_id!r}，实际 WARNING 日志: "
            + str([r.getMessage() for r in warning_records])
        )

        # 确认无 NameError（日志中不应出现 NameError 字样）
        all_messages = [r.getMessage() for r in caplog.records]
        assert not any("NameError" in m for m in all_messages), (
            f"不应出现 NameError，实际日志: {all_messages}"
        )
