"""
T4 — setup 路由 + mcp-status API 测试

Seam: HTTP API (GET /api/setup/mcp-status, GET /api/setup, POST /api/setup)
"""
import os
import re
from unittest.mock import patch

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


class TestSetupMcpStatus:
    def test_mcp_status_returns_groups(self, client):
        """GET /api/setup/mcp-status 返回分组结构"""
        r = client.get("/api/setup/mcp-status")
        assert r.status_code == 200
        data = r.json()
        assert "groups" in data
        assert isinstance(data["groups"], list)
        assert len(data["groups"]) > 0

    def test_mcp_status_group_has_entries(self, client):
        """每个分组含 entries 列表"""
        data = client.get("/api/setup/mcp-status").json()
        for g in data["groups"]:
            assert "key" in g
            assert "label" in g
            assert "entries" in g
            assert isinstance(g["entries"], list)

    def test_mcp_status_entry_fields(self, client):
        """每个 entry 含 name/configured/required/demo_supported/description"""
        data = client.get("/api/setup/mcp-status").json()
        for g in data["groups"]:
            for e in g["entries"]:
                assert "name" in e
                assert "configured" in e
                assert "required" in e
                assert "description" in e

    def test_mcp_status_has_demo_mode(self, client):
        """返回含 demo_mode 字段"""
        data = client.get("/api/setup/mcp-status").json()
        assert "demo_mode" in data
        assert "llm_available" in data


class TestSetupGet:
    def test_get_setup_returns_env_values(self, client):
        """GET /api/setup 返回当前 .env 值 + demo_mode 标记"""
        r = client.get("/api/setup")
        assert r.status_code == 200
        data = r.json()
        assert "__demo_mode" in data
        assert "__llm_available" in data
        assert "__missing_required" in data

    def test_get_setup_masks_sensitive_keys(self, client, tmp_path, monkeypatch):
        """以 _KEY/_SECRET/_TOKEN/_PASSWORD 结尾且非空的字段应被掩码，末4位保留"""
        from app.routes import manage as manage_mod
        import app.routes.manage as manage_pkg

        env_file = tmp_path / ".env"
        env_file.write_text(
            "SHORT_KEY=12345678\n"
            "LONG_KEY=abcdefghijklmnop\n"
            "EMPTY_KEY=\n"
            "NORMAL_VAR=hello\n"
            "APP_ID=world\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(manage_pkg, "_ENV_FILE", str(env_file))

        r = client.get("/api/setup")
        assert r.status_code == 200
        data = r.json()

        # 长度<=8 全掩码
        assert data["SHORT_KEY"] == "******"
        # 长度>8 保留末4位
        assert data["LONG_KEY"] == "****mnop"
        # 空值保持空
        assert data["EMPTY_KEY"] == ""
        # 非敏感键原样返回
        assert data["NORMAL_VAR"] == "hello"
        assert data["APP_ID"] == "world"

    def test_get_setup_empty_value_not_masked(self, client, tmp_path, monkeypatch):
        """空值敏感键应保持空字符串，不变成 ******"""
        from app.routes import manage as manage_mod
        import app.routes.manage as manage_pkg

        env_file = tmp_path / ".env"
        env_file.write_text("SECRET_TOKEN=\n", encoding="utf-8")
        monkeypatch.setattr(manage_pkg, "_ENV_FILE", str(env_file))

        r = client.get("/api/setup")
        assert r.status_code == 200
        data = r.json()
        assert data["SECRET_TOKEN"] == ""

    def test_post_setup_ignores_masked_value(self, client, tmp_path, monkeypatch):
        """POST 时若前端回传 **** 开头的掩码值，不应写入 .env"""
        from app.routes import manage as manage_mod
        import app.routes.manage as manage_pkg

        env_file = tmp_path / ".env"
        env_file.write_text("REAL_KEY=original\n", encoding="utf-8")
        monkeypatch.setattr(manage_pkg, "_ENV_FILE", str(env_file))

        r = client.post("/api/setup", json={"REAL_KEY": "****abcd"})
        assert r.status_code == 200
        assert r.json().get("ok") is True

        content = env_file.read_text(encoding="utf-8")
        assert "REAL_KEY=original" in content
        assert "REAL_KEY=****abcd" not in content

    def test_post_setup_normal_value_written(self, client, tmp_path, monkeypatch):
        """POST 正常值仍可正常写入 .env"""
        from app.routes import manage as manage_mod
        import app.routes.manage as manage_pkg

        env_file = tmp_path / ".env"
        env_file.write_text("STEPFUN_KEY=old\n", encoding="utf-8")
        monkeypatch.setattr(manage_pkg, "_ENV_FILE", str(env_file))

        r = client.post("/api/setup", json={"STEPFUN_KEY": "new-secret"})
        assert r.status_code == 200
        assert r.json().get("ok") is True

        content = env_file.read_text(encoding="utf-8")
        assert "STEPFUN_KEY=new-secret" in content
