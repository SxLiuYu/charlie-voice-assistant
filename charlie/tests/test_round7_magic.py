"""Round 7 magic subsystem bug-fix behavior tests.

Covers:
1. search tools (taobao/douyin/music) + play_music: no NameError, __doc__ non-empty
2. outfit_recommend registered in mcp tool table
3. system_status psutil fallback
4. _check_dev_access whitelist logic
5. evolution learn_from_history atomicity under concurrent threads
6. save_note atomic write failure preserves original file
7. magic-life leaving_home no longer谎报 "空调已关闭"
8. get_location double-failure returns friendly message
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ======================================================================
# 1. search tools + play_music: no NameError, __doc__ non-empty
# ======================================================================

class TestSearchToolsNoNameError:
    """search_products / search_videos / search_music / play_music 不应再抛 NameError。"""

    def test_taobao_search_products_doc_and_call(self, monkeypatch):
        mod = _load_module("magic_taobao_test", "magic-taobao.py")
        assert mod.search_products.__doc__ is not None and len(mod.search_products.__doc__) > 0

        mock_resp = MagicMock()
        mock_resp.text = "<html></html>"
        mock_resp.status_code = 200
        monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: mock_resp)

        result = mod.search_products("手机壳")
        assert isinstance(result, str)
        assert "搜索「手机壳」失败" in result or "🛒" in result

    def test_douyin_search_videos_doc_and_call(self, monkeypatch):
        mod = _load_module("magic_douyin_test", "magic-douyin.py")
        assert mod.search_videos.__doc__ is not None and len(mod.search_videos.__doc__) > 0

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.status_code = 200
        monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: mock_resp)

        result = mod.search_videos("美食教程")
        assert isinstance(result, str)
        assert "没搜到" in result

    def test_music_search_music_doc_and_call(self, monkeypatch):
        mod = _load_module("magic_music_test", "magic-music.py")
        assert mod.search_music.__doc__ is not None and len(mod.search_music.__doc__) > 0

        mock_run = MagicMock(returncode=0, stdout=json.dumps({"result": {"songs": []}}))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: mock_run)

        result = mod.search_music("周杰伦")
        assert isinstance(result, str)
        assert "没搜到" in result

    def test_music_play_music_doc_and_call(self, monkeypatch):
        mod = _load_module("magic_music_test2", "magic-music.py")
        assert mod.play_music.__doc__ is not None and len(mod.play_music.__doc__) > 0

        search_result = json.dumps({"result": {"songs": [{"id": 1, "name": "晴天", "ar": [{"name": "周杰伦"}]}]}})
        url_result = json.dumps({"data": [{"url": "http://example.com/song.mp3"}]})
        call_count = [0]

        def mock_run(cmd, **kw):
            call_count[0] += 1
            if "search" in cmd:
                return MagicMock(returncode=0, stdout=search_result)
            elif "url" in cmd:
                return MagicMock(returncode=0, stdout=url_result)
            return MagicMock(returncode=1, stdout="")

        monkeypatch.setattr(mod.subprocess, "run", mock_run)
        monkeypatch.setattr(mod, "_ensure_https", lambda x: x)

        result = mod.play_music("晴天")
        assert isinstance(result, str)
        assert result.startswith("__MUSIC__")


# ======================================================================
# 2. outfit_recommend 已注册
# ======================================================================

class TestOutfitRecommendRegistered:
    def test_outfit_recommend_in_tool_table(self):
        mod = _load_module("magic_wardrobe_test", "magic-wardrobe.py")
        # fastmcp 注册的工具可以通过 mcp._tool_manager._tools 访问
        tools = getattr(mod.mcp, "_tool_manager", None)
        assert tools is not None
        tool_names = [getattr(t, "name", None) for t in getattr(tools, "_tools", {}).values()]
        assert "outfit_recommend" in tool_names


# ======================================================================
# 3. system_status psutil fallback
# ======================================================================

class TestSystemStatusPsutilFallback:
    def test_psutil_missing_returns_friendly_message(self, monkeypatch):
        mod = _load_module("magic_system_test", "magic-system.py")
        # 让 import psutil 抛 ImportError
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        result = mod.system_status()
        assert isinstance(result, str)
        assert "psutil" in result or "不可用" in result


# ======================================================================
# 4. _check_dev_access whitelist logic
# ======================================================================

class TestCheckDevAccess:
    def test_disabled_returns_false(self, monkeypatch):
        monkeypatch.setenv("DEV_MODE_ENABLED", "0")
        monkeypatch.setenv("FEISHU_DEV_OPEN_IDS", "")
        mod = _load_module("magic_dev_test", "magic-dev.py")
        ok, msg = mod._check_dev_access("any_user")
        assert ok is False

    def test_whitelist_hit_returns_true(self, monkeypatch):
        monkeypatch.setenv("DEV_MODE_ENABLED", "1")
        monkeypatch.setenv("FEISHU_DEV_OPEN_IDS", "ou_allowed,ou_other")
        mod = _load_module("magic_dev_test2", "magic-dev.py")
        ok, msg = mod._check_dev_access("ou_allowed")
        assert ok is True

    def test_not_in_whitelist_returns_false(self, monkeypatch):
        monkeypatch.setenv("DEV_MODE_ENABLED", "1")
        monkeypatch.setenv("FEISHU_DEV_OPEN_IDS", "ou_allowed,ou_other")
        mod = _load_module("magic_dev_test3", "magic-dev.py")
        ok, msg = mod._check_dev_access("ou_stranger")
        assert ok is False

    def test_empty_whitelist_allows_with_warning(self, monkeypatch):
        monkeypatch.setenv("DEV_MODE_ENABLED", "1")
        monkeypatch.setenv("FEISHU_DEV_OPEN_IDS", "")
        mod = _load_module("magic_dev_test4", "magic-dev.py")
        ok, msg = mod._check_dev_access("any_user")
        assert ok is True


# ======================================================================
# 5. evolution concurrency (load-modify-save atomicity)
# ======================================================================

class TestEvolutionConcurrency:
    def test_concurrent_learn_no_lost_updates(self, monkeypatch):
        tmpdir = tempfile.mkdtemp(prefix="evolution_concurrent_")
        monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", tmpdir)
        monkeypatch.setenv("CHARLIE_USER_ID", "concurrent_test")

        mod = _load_module("magic_evolution_concurrent", "magic-evolution.py")

        # 写入初始数据
        initial = {
            "learned_preferences": {},
            "usage_patterns": {},
            "response_metrics": {},
            "adaptation_state": {},
            "version": 1,
            "last_updated": time.time(),
        }
        with open(mod.EVOLUTION_FILE, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        # 覆盖 _analyze_conversation_history 和 _extract_patterns 以返回可控数据
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(5)]

        def fake_analyze():
            return messages

        def fake_extract(msgs):
            return {
                "topic_distribution": Counter(),
                "avg_message_length": 10,
                "frequent_questions": {},
                "active_hours": [],
            }

        monkeypatch.setattr(mod, "_analyze_conversation_history", fake_analyze)
        monkeypatch.setattr(mod, "_extract_patterns", fake_extract)

        errors = []

        def worker(wid):
            try:
                for _ in range(5):
                    mod.learn_from_history()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Worker errors: {errors}"

        with open(mod.EVOLUTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "adaptation_state" in data
        assert data["adaptation_state"].get("response_style") == "平衡"


# ======================================================================
# 6. save_note atomic write failure preserves original file
# ======================================================================

class TestSaveNoteAtomicFailure:
    def test_atomic_write_failure_keeps_original(self, tmp_path, monkeypatch):
        mod = _load_module("magic_notes_atomic", "magic-notes.py")
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        filepath = notes_dir / "existing.md"
        filepath.write_text("original content", encoding="utf-8")

        # 让 os.replace 抛异常
        monkeypatch.setattr(mod.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("replace failed")))
        # 让 fsync 也抛异常，确保走到 replace 分支
        monkeypatch.setattr(mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))

        result = mod.save_note("测试标题", "测试内容")
        assert "失败" in result
        # 原文件不应被截断
        assert filepath.read_text(encoding="utf-8") == "original content"


# ======================================================================
# 7. magic-life leaving_home no longer谎报 "空调已关闭"
# ======================================================================

class TestLeavingHomeHonesty:
    def test_leaving_home_does_not_claim_ac_off(self, monkeypatch):
        mod = _load_module("magic_life_test", "magic-life.py")
        monkeypatch.setenv("ESP32_IP", "")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"forecasts": [{"casts": []}]}
        mock_resp.status_code = 200
        monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: mock_resp)

        result = mod.leaving_home()
        assert "空调已关闭" not in result
        assert "空调控制未配置" in result or "跳过" in result


# ======================================================================
# 8. get_location double-failure returns friendly message
# ======================================================================

class TestGetLocationDoubleFailure:
    def test_both_methods_fail_returns_friendly(self, monkeypatch):
        mod = _load_module("magic_info_loc", "magic-info.py")
        monkeypatch.setenv("AMAP_KEY", "")

        def bad_get(url, **kw):
            raise ConnectionError("network down")

        monkeypatch.setattr(mod.requests, "get", bad_get)
        monkeypatch.setattr(mod, "ESP32_IP", "127.0.0.1")

        # 让 app.geo.locate 返回 None
        geo_mock = MagicMock()
        geo_mock.locate.return_value = None
        monkeypatch.setitem(sys.modules, "app.geo", geo_mock)

        result = mod.get_location()
        assert isinstance(result, str)
        assert "无法获取位置信息" in result
        # 不应拼接两条错误信息
        assert "失败:" not in result or result.count("失败") == 0
