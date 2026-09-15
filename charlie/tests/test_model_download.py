"""
T8 — SenseVoice 模型下载 API 测试

Seam: HTTP API (POST /api/setup/download-model + GET /api/setup/download-status)
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


class TestModelDownload:
    def test_download_status_returns_structure(self, client):
        """GET /api/setup/download-status 返回 {downloading, error, model_exists}"""
        r = client.get("/api/setup/download-status")
        assert r.status_code == 200
        data = r.json()
        assert "downloading" in data
        assert "error" in data
        assert "model_exists" in data
        # 类型断言：error 在无错误时为 None，有错误时为 str
        assert isinstance(data["downloading"], bool)
        assert data["error"] is None or isinstance(data["error"], str)
        assert isinstance(data["model_exists"], bool)

    def test_download_model_returns_started_when_not_downloading(self, client):
        """POST /api/setup/download-model 在无下载进行时返回 started（mock 模型不存在）"""
        with patch.object(voice_server, "_check_model_exists", return_value=False), \
             patch.object(voice_server._model_download, "start", return_value=True) as mock_start:
            r = client.post("/api/setup/download-model")
        assert r.status_code == 200
        data = r.json()
        assert data["started"] is True
        mock_start.assert_called_once()

    def test_download_model_skips_when_exists(self, client):
        """模型已存在时 POST 返回 started=False"""
        with patch.object(voice_server, "_check_model_exists", return_value=True), \
             patch.object(voice_server._model_download, "is_active", return_value=False):
            r = client.post("/api/setup/download-model")
        assert r.status_code == 200
        data = r.json()
        assert data["started"] is False
        assert data["model_exists"] is True

    def test_download_model_active_returns_in_progress(self, client):
        """mock is_active()=True 时 POST 返回 started=False 且 message 含"进行中"."""
        with patch.object(voice_server, "_check_model_exists", return_value=False), \
             patch.object(voice_server._model_download, "is_active", return_value=True):
            r = client.post("/api/setup/download-model")
        assert r.status_code == 200
        data = r.json()
        assert data["started"] is False
        assert "进行中" in data.get("message", "")
