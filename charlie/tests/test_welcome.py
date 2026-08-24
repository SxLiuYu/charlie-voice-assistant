"""
T7 — /welcome 引导页测试

Seam: HTTP API (GET /welcome, GET /api/welcome/status)
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import voice_server


@pytest.fixture(scope="module")
def client():
    os.environ["SKIP_BACKGROUND"] = "1"
    os.environ.setdefault("AGNES_KEY", "test")
    os.environ.setdefault("TTS_KEY", "test")
    os.environ.setdefault("ASR_KEY", "test")
    os.environ.setdefault("AMAP_KEY", "test")
    yield TestClient(voice_server.app)


class TestWelcome:
    def test_welcome_page_returns_html(self, client):
        """GET /welcome 返回 HTML 页面"""
        r = client.get("/welcome")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_welcome_status_returns_structure(self, client):
        """GET /api/welcome/status 返回 {has_env, demo_mode, ollama_online, missing_required}"""
        r = client.get("/api/welcome/status")
        assert r.status_code == 200
        data = r.json()
        assert "has_env" in data
        assert "demo_mode" in data
        assert "ollama_online" in data
        assert "missing_required" in data
