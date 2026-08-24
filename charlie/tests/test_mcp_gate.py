"""
T6 — MCP 分层 + mcp_gate key 缺失跳过测试

Seam: app.mcp_gate 公共接口（resolve_mcp_profile / filter_optional_mcp）
"""
import os
from unittest.mock import patch

import pytest

from app import mcp_gate


class TestMcpGate:
    def test_core_profile_returns_12(self, monkeypatch):
        """MCP_PROFILE=core 返回核心 MCP（含 amap-maps/filesystem 别名 + magic-preferences + magic-memory 等）"""
        monkeypatch.setenv("MCP_PROFILE", "core")
        result = mcp_gate.resolve_mcp_profile()
        assert len(result) >= 12  # 至少 12 个核心
        assert "magic-info" in result
        assert "magic-reminder" in result
        assert "magic-preferences" in result
        assert "magic-memory" in result
        assert "magic-feishu" not in result  # 可选，不在 core

    def test_all_profile_returns_core_plus_optional(self, monkeypatch):
        """MCP_PROFILE=all 返回核心 + 可选（key 缺失过滤后）"""
        monkeypatch.setenv("MCP_PROFILE", "all")
        result = mcp_gate.resolve_mcp_profile()
        assert len(result) >= 12  # 至少 12 个核心
        # 核心全部都在
        for k in mcp_gate.CORE_MCP_KEYS:
            assert k in result

    def test_all_profile_filters_missing_feishu(self, monkeypatch):
        """MCP_PROFILE=all + 缺 FEISHU_APP_ID 时 magic-feishu 被过滤"""
        monkeypatch.setenv("MCP_PROFILE", "all")
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        result = mcp_gate.resolve_mcp_profile()
        assert "magic-feishu" not in result

    def test_all_profile_keeps_feishu_when_configured(self, monkeypatch):
        """MCP_PROFILE=all + 配了 FEISHU_APP_ID/SECRET 时 magic-feishu 保留"""
        monkeypatch.setenv("MCP_PROFILE", "all")
        monkeypatch.setenv("FEISHU_APP_ID", "cli_test123")
        monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
        result = mcp_gate.resolve_mcp_profile()
        assert "magic-feishu" in result

    def test_custom_profile_reads_env(self, monkeypatch):
        """MCP_PROFILE=custom 读 MCP_SERVERS 列表"""
        monkeypatch.setenv("MCP_PROFILE", "custom")
        monkeypatch.setenv("MCP_SERVERS", "magic-info,magic-reminder")
        result = mcp_gate.resolve_mcp_profile()
        assert "magic-info" in result
        assert "magic-reminder" in result

    def test_custom_empty_falls_back_to_core(self, monkeypatch):
        """MCP_PROFILE=custom + MCP_SERVERS 空 → 回退 core（含 magic-preferences + magic-memory）"""
        monkeypatch.setenv("MCP_PROFILE", "custom")
        monkeypatch.delenv("MCP_SERVERS", raising=False)
        result = mcp_gate.resolve_mcp_profile()
        assert len(result) >= 12  # 至少 12 个核心

    def test_all_profile_filters_missing_tuya(self, monkeypatch):
        """MCP_PROFILE=all + 缺 TUYA_CLIENT_ID 时 ac-control 被过滤"""
        monkeypatch.setenv("MCP_PROFILE", "all")
        monkeypatch.delenv("TUYA_CLIENT_ID", raising=False)
        monkeypatch.delenv("TUYA_ACCESS_KEY", raising=False)
        result = mcp_gate.resolve_mcp_profile()
        assert "ac-control" not in result

    def test_placeholder_value_treated_as_missing(self, monkeypatch):
        """占位符值（'你的...'）视为未配置"""
        monkeypatch.setenv("MCP_PROFILE", "all")
        monkeypatch.setenv("FEISHU_APP_ID", "你的飞书ID")
        monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
        result = mcp_gate.resolve_mcp_profile()
        assert "magic-feishu" not in result
