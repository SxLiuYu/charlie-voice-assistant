"""
T — manage 路由 memory 端点测试

Seam: GET /api/memory（搜索）/ DELETE /api/memory（删除记忆）
"""
import os
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import voice_server


@pytest.fixture(scope="module")
def client():
    os.environ["SKIP_BACKGROUND"] = "1"
    os.environ.setdefault("GLM_KEY", "test")
    os.environ.setdefault("TTS_KEY", "test")
    os.environ.setdefault("ASR_KEY", "test")
    os.environ.setdefault("AMAP_KEY", "test")
    yield TestClient(voice_server.app)


class TestMemoryDelete:
    def test_delete_memory_returns_ok(self, client):
        """DELETE /api/memory?query=xx 返回 ok:true"""
        fake_mem = MagicMock()
        fake_mem.forget.return_value = "已删除1条记忆。"
        with patch("app.load_magic_module", return_value=fake_mem):
            r = client.delete("/api/memory?query=项目")
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True

    def test_delete_memory_missing_query_returns_400(self, client):
        """DELETE /api/memory 缺少 query 时返回 400"""
        r = client.delete("/api/memory")
        assert r.status_code == 400

    def test_delete_memory_module_not_loaded_returns_500(self, client):
        """记忆模块未加载时返回 500"""
        with patch("app.load_magic_module", return_value=None):
            r = client.delete("/api/memory?query=项目")
        assert r.status_code == 500


class TestTTSVoice:
    def test_set_tts_voice_updates_asr_tts(self, client):
        """POST /api/tts/voice 应修改 agent.asr_tts.TTS_VOICE，否则 TTS 实际不会切换"""
        import agent.asr_tts
        original = agent.asr_tts.TTS_VOICE
        try:
            r = client.post("/api/tts/voice", json={"voice_id": "Stella"})
            assert r.status_code == 200
            data = r.json()
            assert data["ok"] is True
            assert data["voice"] == "Stella"
            assert agent.asr_tts.TTS_VOICE == "Stella"
        finally:
            agent.asr_tts.TTS_VOICE = original

    def test_list_tts_voices_returns_current_from_asr_tts(self, client):
        """GET /api/tts/voices 的 current 应与 get_effective_tts_config()（实际合成音色）一致。

        角色音色优先于全局 TTS_VOICE（且持久化到 preferences.json），
        UI 展示必须与实际合成用的音色一致，否则重启后显示与实际分叉。
        """
        import agent.asr_tts
        from agent.roles import get_current_role, set_role_tts_voice, _ROLES
        role_id = get_current_role()
        original_role_voice = _ROLES.get(role_id, {}).get("tts_voice")
        try:
            set_role_tts_voice(role_id, "Alex")
            r = client.get("/api/tts/voices")
            assert r.status_code == 200
            data = r.json()
            assert data["current"] == "Alex"
            assert any(v["id"] == "Alex" and v["current"] for v in data["voices"])
        finally:
            if original_role_voice is not None:
                set_role_tts_voice(role_id, original_role_voice)

    def test_set_tts_voice_updates_role_and_effective_config(self, client):
        """切换音色后，get_effective_tts_config() 应返回新音色（不仅是 asr_tts.TTS_VOICE）"""
        import agent.asr_tts
        from agent.roles import get_current_role, get_role_tts_config, set_role_tts_voice
        original_tts = agent.asr_tts.TTS_VOICE
        original_role_voice = None
        role_id = get_current_role()
        role = get_role_tts_config(role_id)
        original_role_voice = role["voice"]
        try:
            r = client.post("/api/tts/voice", json={"voice_id": "Vega"})
            assert r.status_code == 200
            # 验证 asr_tts.TTS_VOICE 已更新
            assert agent.asr_tts.TTS_VOICE == "Vega"
            # 验证当前角色音色已更新（角色硬编码覆盖的根因修复）
            role_cfg = get_role_tts_config(role_id)
            assert role_cfg["voice"] == "Vega"
            # 验证 get_effective_tts_config() 返回新音色
            voice, speed = agent.asr_tts.get_effective_tts_config()
            assert voice == "Vega"
        finally:
            agent.asr_tts.TTS_VOICE = original_tts
            if original_role_voice is not None:
                set_role_tts_voice(role_id, original_role_voice)

    def test_tts_cache_uses_voice_in_key(self, client):
        """_tts_cleaned_to_mp3 缓存 key 应包含 voice，避免双层缓存"""
        import agent.asr_tts
        from unittest.mock import patch
        original = agent.asr_tts.TTS_VOICE
        try:
            agent.asr_tts.TTS_VOICE = "Ethan"
            fake_audio = b"fake-audio-bytes-for-testing-" * 5  # > 100 bytes
            with patch.object(agent.asr_tts, "_tts_cache_get") as mock_get:
                mock_get.return_value = None
                with patch.object(agent.asr_tts, "tts", return_value=fake_audio):
                    with patch.object(agent.asr_tts, "_tts_cache_put"):
                        # 让 get_effective_tts_config 返回角色音色（与 TTS_VOICE 不同）
                        with patch.object(agent.asr_tts, "get_effective_tts_config", return_value=("Stella", 1.0)):
                            agent.asr_tts._tts_cleaned_to_mp3("hello world")
                            # 断言 _tts_cache_get 收到了 voice 参数
                            assert mock_get.called
                            args = mock_get.call_args
                            # 第二个位置参数应为 voice="Stella"
                            assert args[0][1] == "Stella"
        finally:
            agent.asr_tts.TTS_VOICE = original


class TestMemorySearch:
    def test_get_memory_with_query_returns_results(self, client):
        """GET /api/memory?query=xx 调用 recall_hybrid 并返回结果"""
        fake_mem = MagicMock()
        fake_mem.recall_hybrid.return_value = [
            {"datetime": "2026-01-01 10:00", "summary": "用户提到项目截止日期"},
        ]
        fake_mem.get_memory_summary.return_value = {
            "total": 1, "tags": {}, "recent": []
        }
        with patch("app.load_magic_module", return_value=fake_mem):
            r = client.get("/api/memory?query=项目")
        assert r.status_code == 200
        data = r.json()
        assert "query" in data
        assert data["query"] == "项目"
        assert "recent" in data

    def test_get_memory_without_query_returns_summary(self, client):
        """GET /api/memory 无 query 时返回记忆摘要"""
        fake_mem = MagicMock()
        fake_mem.get_memory_summary.return_value = {
            "total": 5, "tags": {"deadline": 2}, "recent": []
        }
        with patch("app.load_magic_module", return_value=fake_mem):
            r = client.get("/api/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5
        assert "tags" in data
