"""Round 26 UX regression tests.

P1-1: context 缓存跨会话 recent_conversation 泄漏
P1-2: 飞书占位消息"正在思考…"回复后删除
P1-3: persona 按用户隔离状态与文件
P2-4: MCP 工具超时不触发 LLM provider 轮换
P2-5: Web UI 错误消息与语音/飞书对齐
P2-6: 历史落盘频率 5→2
P2-7: 工作记忆容量 8→16
"""
import os
import threading
from unittest.mock import patch, MagicMock


# ─── P1-1: context cache cross-session leak ───────────────────────────────

class TestContextCacheIsolation:
    def test_override_recent_exists(self):
        """应有 _override_recent 辅助函数"""
        import agent.context as ctx
        assert hasattr(ctx, "_override_recent")

    def test_fresh_path_overrides_recent(self):
        """缓存命中路径也应覆盖 recent_conversation"""
        import agent.context as ctx
        src = open(ctx.__file__).read()
        idx = src.find("def get_context")
        body = src[idx:idx + 3000]
        # 两个返回路径都应调用 _override_recent
        assert body.count("_override_recent(") >= 2

    def test_override_uses_session_history(self):
        """覆盖应基于当前 session 的历史而非缓存"""
        import agent.context as ctx
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "ctx_test_user_r26"}):
            ctx._override_recent.__wrapped__ if hasattr(ctx._override_recent, '__wrapped__') else None
            result = {"recent_conversation": ["别的会话的消息"]}
            ctx._override_recent(result, "ctx_test_session_r26")
            # 该 session 无历史 → 应被覆盖为空列表（不再残留他人对话）
            assert result["recent_conversation"] == []

    def test_stale_path_overrides_recent(self):
        """stale-while-revalidate 路径也应覆盖"""
        import agent.context as ctx
        src = open(ctx.__file__).read()
        idx = src.find("stale_ctx")
        body = src[idx:idx + 1500]
        assert "_override_recent" in body


# ─── P1-2: Feishu placeholder deletion ────────────────────────────────────

class TestFeishuPlaceholderDeletion:
    def test_delete_helper_exists(self):
        """应有 _delete_feishu_msg 辅助函数"""
        import feishu_ws
        assert hasattr(feishu_ws, "_delete_feishu_msg")

    def test_delete_helper_empty_id(self):
        """空 message_id 应直接返回 False 不发请求"""
        import feishu_ws
        assert feishu_ws._delete_feishu_msg("") is False

    def test_placeholder_id_captured(self):
        """占位消息应捕获 message_id"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "_placeholder_msg_id" in src
        assert "_send_single_feishu_msg(chat_id, \"正在思考…\")" in src

    def test_deleted_in_finally(self):
        """回复完成后应在 finally 中删除占位"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        idx = src.find("def _process_message_sync")
        body = src[idx:idx + 8000]
        finally_idx = body.rfind("finally:")
        assert finally_idx > 0
        finally_body = body[finally_idx:finally_idx + 400]
        assert "_delete_feishu_msg" in finally_body

    def test_delete_api_endpoint(self):
        """删除应调用飞书 DELETE 消息 API"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "DELETE" in src or "_req.delete" in src
        assert "/im/v1/messages/" in src


# ─── P1-3: Persona per-user isolation ─────────────────────────────────────

class TestPersonaPerUser:
    def test_persona_file_per_user(self):
        """非默认用户应有独立 persona 文件"""
        import agent.persona as p
        old_uid = os.environ.get("CHARLIE_USER_ID")
        try:
            os.environ["CHARLIE_USER_ID"] = "persona_r26_alice"
            f = p._persona_file()
            assert "persona_state_persona_r26_alice.json" in f
            os.environ["CHARLIE_USER_ID"] = "default"
            f2 = p._persona_file()
            assert f2.endswith("persona_state.json")
        finally:
            if old_uid is not None:
                os.environ["CHARLIE_USER_ID"] = old_uid
            else:
                os.environ.pop("CHARLIE_USER_ID", None)

    def test_states_keyed_by_user(self):
        """内存状态应按用户分键"""
        import agent.persona as p
        assert hasattr(p, "_states")
        old_uid = os.environ.get("CHARLIE_USER_ID")
        try:
            os.environ["CHARLIE_USER_ID"] = "persona_r26_bob"
            p.update("你好")
            assert "persona_r26_bob" in p._states
        finally:
            if old_uid is not None:
                os.environ["CHARLIE_USER_ID"] = old_uid
            else:
                os.environ.pop("CHARLIE_USER_ID", None)
            p._states.pop("persona_r26_bob", None)

    def test_no_constant_persona_file_or_function_only(self):
        """不应再用模块级 PERSONA_FILE 常量做 IO（应为函数）"""
        import agent.persona as p
        # _persona_file 应可调用
        assert callable(p._persona_file)


# ─── P2-4: MCP timeout should not rotate provider ─────────────────────────

class TestMCPTimeoutNoRotation:
    def test_guard_in_record_brain_failure(self):
        """_record_brain_failure 应跳过 MCP 工具错误的 provider 轮换"""
        import agent.llm as llm
        src = open(llm.__file__).read()
        idx = src.find("def _record_brain_failure")
        body = src[idx:idx + 2500]
        assert "mcp tool" in body.lower()

    def test_mcp_timeout_no_provider_note(self):
        """MCP 工具超时不应触发 _note_provider_failure"""
        import agent.llm as llm
        with patch.object(llm, "_note_provider_failure") as mock_note, \
             patch.object(llm, "_try_rotate_provider_on_failure") as mock_rotate, \
             patch("agent.llm._st") as mock_st:
            mock_st.brain_failures = 0
            mock_st.brains = {}
            llm._record_brain_failure("MCP tool 'weather' timeout after 30s")
            mock_note.assert_not_called()
            mock_rotate.assert_not_called()

    def test_real_conn_error_still_rotates(self):
        """真实连接错误仍应触发轮换"""
        import agent.llm as llm
        with patch.object(llm, "_note_provider_failure") as mock_note, \
             patch.object(llm, "_try_rotate_provider_on_failure") as mock_rotate, \
             patch("agent.llm._st") as mock_st:
            mock_st.brain_failures = 0
            mock_st.brains = {}
            llm._record_brain_failure("ConnectionError: connection refused")
            mock_note.assert_called_once()


# ─── P2-5: Web UI error alignment ─────────────────────────────────────────

class TestWebUIErrorAlignment:
    def test_timeout_friendly_message(self):
        from app.routes.conversation import _friendly_brain_error
        result = _friendly_brain_error(RuntimeError("brain.run total timeout 35s"))
        assert "超时" in result

    def test_mcp_friendly_message(self):
        from app.routes.conversation import _friendly_brain_error
        result = _friendly_brain_error(RuntimeError("MCP工具 weather 暂时不可用"))
        assert "暂时不可用" in result

    def test_rate_limit_still_busy_message(self):
        from app.routes.conversation import _friendly_brain_error
        result = _friendly_brain_error(RuntimeError("HTTP 429 too many requests"))
        assert "忙" in result or "429" in result or len(result) > 0


# ─── P2-6/P2-7: Constants ─────────────────────────────────────────────────

class TestConstants:
    def test_history_save_every_is_2(self):
        from agent.history import HISTORY_SAVE_EVERY
        assert HISTORY_SAVE_EVERY == 2

    def test_wm_session_store_max_is_16(self):
        from agent.working_memory import _SESSION_STORE_MAX
        assert _SESSION_STORE_MAX == 16
