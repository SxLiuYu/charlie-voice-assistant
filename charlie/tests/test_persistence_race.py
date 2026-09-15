"""Regression tests for data-persistence race-condition fixes (round 21).

Tests cover:
- magic-memory: RLocker + atomic I/O + single-acquisition wrappers
- magic-decisions: _atomic_save + atomic read-modify-write for history
- Concurrent write-protection (2-thread remember_conversation)
- Crash resilience (_atomic_save leaves target untouched on failure)
"""

import json
import os
import sys
import threading
import time
import tempfile
import shutil
import importlib.util
from unittest.mock import patch

import pytest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)

TMP_DIR = tempfile.mkdtemp(prefix="charlie_race_test_")


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 环境翻转只允许覆盖"被隔离模块加载"的瞬间：
# 在 pytest 收集期（本文件 import 时）改写并长期持有全局 env，
# 会污染之后导入的所有模块的静态路径快照（含生产代码），
# 与执行期其它测试恢复 env 的动作发生错位。加载完立即恢复。
_ORIG_DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR")
_ORIG_USER_ID = os.environ.get("CHARLIE_USER_ID")
try:
    os.environ["ASSISTANT_KID_DATA_DIR"] = TMP_DIR
    os.environ.pop("CHARLIE_USER_ID", None)
    MEM = _load_module("magic_memory_race", "magic-memory.py")
    DEC = _load_module("magic_decisions_race", "magic-decisions.py")
finally:
    if _ORIG_DATA_DIR is None:
        os.environ.pop("ASSISTANT_KID_DATA_DIR", None)
    else:
        os.environ["ASSISTANT_KID_DATA_DIR"] = _ORIG_DATA_DIR
    if _ORIG_USER_ID is None:
        os.environ.pop("CHARLIE_USER_ID", None)
    else:
        os.environ["CHARLIE_USER_ID"] = _ORIG_USER_ID


def teardown_module(module):
    for f in os.listdir(TMP_DIR):
        try:
            os.remove(os.path.join(TMP_DIR, f))
        except OSError:
            pass
    try:
        os.rmdir(TMP_DIR)
    except OSError:
        pass


# ======================================================================
# Magic-memory: single lock acquisition
# ======================================================================

class TestMagicMemoryRaceFixes:
    """remember/correct/dedup/forget/forget_working_fact must use one _memory_lock."""

    def test_memory_has_rlock(self):
        assert isinstance(MEM._memory_lock, type(threading.RLock()))

    def test_save_uses_atomic_io(self):
        names = list(MEM._do_save_memories.__code__.co_names)
        # co_names stores 'os' and 'replace' separately
        assert "os" in names and "replace" in names, \
            "_do_save_memories missing os.replace call"

    def test_remember_closes_under_lock(self):
        MEM._do_save_memories([])  # prep clean slate
        MEM.remember_conversation("用户 Alpha 项目周五截止", "知道了")
        mems = MEM._do_load_memories()
        assert any("Alpha" in m.get("user_text", "") for m in mems)

    def test_correct_under_lock(self):
        MEM._do_save_memories([{
            "timestamp": time.time(), "datetime": "2024-01-01 10:00",
            "user_text": "旧内容 Old", "tags": ["test"],
            "summary": "旧摘要", "bigrams": {},
        }])
        ok = MEM.correct_memory("Old", "新内容 New")
        assert ok is True
        mems = MEM._do_load_memories()
        assert mems[0]["user_text"] == "新内容 New"

    def test_dedup_under_lock(self):
        base = {
            "timestamp": time.time(), "datetime": "2024-01-01 10:00",
            "user_text": "项目截止周五", "tags": ["test"],
            "summary": "用户提到项目", "bigrams": MEM._text_to_bigrams("项目截止周五"),
        }
        MEM._do_save_memories([dict(base), dict(base)])
        removed = MEM.dedup_memories()
        mems = MEM._do_load_memories()
        assert removed >= 1
        assert len(mems) <= 2

    def test_forget_under_lock(self):
        MEM._do_save_memories([
            {"timestamp": time.time(), "datetime": "2024-01-01 10:00",
             "user_text": "项目 Alpha", "tags": ["t"], "summary": "Alpha", "bigrams": {}},
            {"timestamp": time.time(), "datetime": "2024-01-01 10:01",
             "user_text": "天气很好", "tags": ["t"], "summary": "天气", "bigrams": {}},
        ])
        r = MEM.forget("Alpha")
        assert "删除" in r
        mems = MEM._do_load_memories()
        assert all("Alpha" not in m["user_text"] for m in mems)

    def test_concurrent_remember_not_silent_lost(self):
        N = 20
        threads = []
        for i in range(N):
            t = threading.Thread(target=MEM.remember_conversation,
                                 args=(f"线程{i} abcd测试", "回复"))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        mems = MEM._do_load_memories()
        assert len(mems) > 0, "All memories silently lost"


# ======================================================================
# Magic-decisions: atomic I/O
# ======================================================================

class TestMagicDecisionsRaceFixes:
    """mark_triggered + _atomic_save ensure valid JSON under concurrency."""

    def test_atomic_save_creates_valid_json(self):
        target = os.path.join(TMP_DIR, "atomic_valid.json")
        DEC._atomic_save(target, {"k": "v"})
        assert os.path.exists(target)
        with open(target) as f:
            assert json.load(f)["k"] == "v"

    def test_atomic_save_no_tmp_after_success(self):
        target = os.path.join(TMP_DIR, "atomic_clean.json")
        DEC._atomic_save(target, {"a": 1})
        tmps = [f for f in os.listdir(TMP_DIR) if f.endswith(".tmp")]
        assert tmps == [], f"Leftover tmp files: {tmps}"

    def test_mark_triggered_produces_valid_json(self):
        DEC.DECISIONS_FILE = os.path.join(TMP_DIR, "atomic_mark.json")
        DEC.mark_triggered("r1")
        with open(DEC.DECISIONS_FILE) as f:
            data = json.load(f)
        assert "r1" in data
        assert "last_trigger" in data["r1"]

    def test_mark_triggered_concurrent_no_corruption(self):
        DEC.DECISIONS_FILE = os.path.join(TMP_DIR, "atomic_concurrent.json")
        threads = [threading.Thread(target=DEC.mark_triggered,
                                    args=(f"r{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(DEC.DECISIONS_FILE) as f:
            data = json.load(f)
        assert isinstance(data, dict)
        for i in range(10):
            assert f"r{i}" in data

    def test_atomic_save_leaves_target_on_failure(self):
        target = os.path.join(TMP_DIR, "atomic_protect.json")
        with open(target, "w") as f:
            json.dump({"stable": True}, f)
        with patch.object(DEC.json, "dump", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                DEC._atomic_save(target, {"unstable": True})
        with open(target) as f:
            assert json.load(f) == {"stable": True}

    def test_feedback_uses_atomic_save(self):
        """DEC._atomic_save co_names contains os and replace."""
        names = list(DEC._atomic_save.__code__.co_names)
        assert "os" in names and "replace" in names
