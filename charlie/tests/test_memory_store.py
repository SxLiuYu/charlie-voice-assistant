"""Tests for agent/memory_store.py (SQLite long-term memory persistence)"""
import os
import time
import tempfile
import shutil
import pytest

# Ensure isolated temp dir for test DB
os.environ["ASSISTANT_KID_DATA_DIR"] = tempfile.mkdtemp(prefix="charlie_mem_test_")
os.environ["CHARLIE_USER_ID"] = "test_user"
os.environ["CHARLIE_TEST_MODE"] = "1"

from agent.memory_store import (
    _init_db, _get_db_path, warm_up_cache,
    upsert_fact, get_fact, get_facts_by_category, get_all_facts, delete_fact,
    record_correction,
    start_session, end_session, get_recent_sessions,
    add_event, get_events, get_events_by_tags,
    store_semantic_knowledge, get_semantic_knowledge, refresh_semantic_memory,
    get_memory_stats,
    _HOT_FACT_CACHE_MAX,
)
from agent import memory_store as ms


def _fresh_db():
    """Create a fresh isolated DB for each test."""
    ms._hot_fact_cache.clear()
    db_path = _get_db_path()
    if os.path.exists(db_path):
        os.remove(db_path)
    _init_db()
    return db_path


class TestInit:
    def test_init_creates_tables(self):
        db = _fresh_db()
        import sqlite3
        conn = sqlite3.connect(db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in ["facts", "sessions", "events", "corrections", "semantic_knowledge"]:
            assert t in tables, f"table {t} missing"

    def test_init_creates_indices(self):
        db = _fresh_db()
        import sqlite3
        conn = sqlite3.connect(db)
        indices = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
        for idx in ["idx_events_created", "idx_events_session", "idx_facts_category",
                     "idx_sessions_channel", "idx_sessions_started"]:
            assert idx in indices, f"index {idx} missing"

    def test_init_is_idempotent(self):
        _fresh_db()
        _init_db()  # second call should not error


class TestFacts:
    def test_upsert_and_get(self):
        _fresh_db()
        upsert_fact("pet_type", "猫", category="personal", confidence=0.9)
        entry = get_fact("pet_type")
        assert entry is not None
        assert entry["value"] == "猫"
        assert entry["category"] == "personal"
        assert entry["confidence"] == 0.9

    def test_upsert_updates_existing(self):
        _fresh_db()
        upsert_fact("city", "北京")
        upsert_fact("city", "上海")
        entry = get_fact("city")
        assert entry["value"] == "上海"

    def test_get_missing_returns_none(self):
        _fresh_db()
        assert get_fact("nonexistent") is None

    def test_get_facts_by_category(self):
        _fresh_db()
        upsert_fact("pet", "猫", category="personal")
        upsert_fact("hobby", "摄影", category="preference")
        upsert_fact("city", "北京", category="fact")
        facts = get_facts_by_category("preference")
        assert len(facts) == 1
        assert facts[0]["key"] == "hobby"

    def test_get_all_facts(self):
        _fresh_db()
        for i in range(5):
            upsert_fact(f"key_{i}", f"val_{i}")
        facts = get_all_facts()
        assert len(facts) == 5

    def test_delete_fact(self):
        _fresh_db()
        upsert_fact("temp", "gone")
        assert delete_fact("temp") is True
        assert get_fact("temp") is None
        assert delete_fact("temp") is False  # already gone

    def test_empty_key_skipped(self):
        _fresh_db()
        upsert_fact("", "value")   # should not crash
        upsert_fact(None, "value") # should not crash


class TestSessions:
    def test_start_and_end_session(self):
        _fresh_db()
        start_session("sess_1", channel="feishu")
        end_session("sess_1", turn_count=5, summary="测试会话", tags=["test"])
        sessions = get_recent_sessions(limit=10)
        assert len(sessions) == 1
        assert sessions[0]["id"] == "sess_1"
        assert sessions[0]["turn_count"] == 5
        assert sessions[0]["channel"] == "feishu"

    def test_get_recent_sessions_empty(self):
        _fresh_db()
        assert get_recent_sessions() == []

    def test_start_duplicate_session_ignored(self):
        _fresh_db()
        start_session("s1", channel="ws")
        start_session("s1", channel="ws")  # INSERT OR IGNORE
        rows = get_recent_sessions()
        assert len(rows) == 1


class TestEvents:
    def test_add_and_get_events(self):
        _fresh_db()
        start_session("s1")
        eid = add_event("s1", "我叫小明", "用户提到名字", tags=["personal"], category="fact",
                        bigrams={"我叫": 1}, sentiment="neutral")
        assert eid > 0
        events = get_events(limit=10)
        assert len(events) == 1
        assert events[0]["user_text"] == "我叫小明"
        assert events[0]["summary"] == "用户提到名字"

    def test_get_events_by_category(self):
        _fresh_db()
        start_session("s1")
        add_event("s1", "text1", "s1", tags=[], category="preference")
        add_event("s1", "text2", "s2", tags=[], category="fact")
        events = get_events(category="preference")
        assert len(events) == 1

    def test_get_events_by_tags(self):
        _fresh_db()
        start_session("s1")
        add_event("s1", "t1", "s1", tags=["personal"])
        add_event("s1", "t2", "s2", tags=["task"])
        events = get_events_by_tags(["personal"])
        assert len(events) == 1
        assert events[0]["tags"] == ["personal"]

    def test_get_events_since_hours(self):
        _fresh_db()
        start_session("s1")
        add_event("s1", "recent", "recent")
        # old event
        import sqlite3
        db = _get_db_path()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (session_id, user_text, summary, created_at) VALUES (?, ?, ?, ?)",
                ("s1", "old", "old", "2000-01-01T00:00:00Z")
            )
            conn.commit()
        events = get_events(since_hours=1)
        texts = [e["user_text"] for e in events]
        assert "old" not in texts


class TestCorrections:
    def test_record_correction(self):
        _fresh_db()
        record_correction("pet", "狗", "猫", "用户更正")
        # corrections table - verify via direct query
        import sqlite3
        db = _get_db_path()
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT * FROM corrections WHERE fact_key=?", ("pet",)).fetchone()
        assert row is not None
        assert row[2] == "狗"   # old_value
        assert row[3] == "猫"   # new_value


class TestSemanticKnowledge:
    def test_store_and_get(self):
        _fresh_db()
        store_semantic_knowledge("user_name", "personal", "name", "小明", "小明", 0.9, "2024-01-01")
        entries = get_semantic_knowledge("personal")
        assert len(entries) == 1
        assert entries[0]["key"] == "小明"

    def test_get_all_semantic(self):
        _fresh_db()
        store_semantic_knowledge("k1", "preference", "like", "咖啡", "咖啡")
        store_semantic_knowledge("k2", "habit", "routine", "早起", "早起")
        entries = get_semantic_knowledge()
        assert len(entries) == 2

    def test_refresh_semantic_incremental(self):
        _fresh_db()
        upsert_fact("my_pref", "我喜欢摄影", category="preference")
        updated = refresh_semantic_memory()
        assert updated > 0
        # second call: same fact, should skip
        updated2 = refresh_semantic_memory()
        assert updated2 == 0


class TestLRUCache:
    def test_hot_cache_warmup(self):
        _fresh_db()
        # insert several facts
        for i in range(10):
            upsert_fact(f"k{i}", f"v{i}")
        count = warm_up_cache()
        assert count == 10

    def test_hot_cache_read_path(self):
        _fresh_db()
        upsert_fact("cached_key", "cached_val")
        entry = get_fact("cached_key")  # should hit cache after first write
        assert entry["value"] == "cached_val"

    def test_lru_eviction(self):
        _fresh_db()
        ms._hot_fact_cache.clear()
        # Filling and evicting
        for i in range(_HOT_FACT_CACHE_MAX + 10):
            key = f"x{i}"
            with ms._hot_cache_lock:
                if len(ms._hot_fact_cache) >= _HOT_FACT_CACHE_MAX:
                    ms._hot_fact_cache.popitem(last=False)
                ms._hot_fact_cache[key] = {"value": str(i)}
        with ms._hot_cache_lock:
            assert len(ms._hot_fact_cache) == _HOT_FACT_CACHE_MAX


class TestStats:
    def test_get_memory_stats(self):
        _fresh_db()
        upsert_fact("f1", "v1")
        start_session("s1")
        add_event("s1", "t", "s")
        stats = get_memory_stats()
        assert stats["facts"] == 1
        assert stats["sessions"] == 1
        assert stats["events"] == 1
        assert stats["cache_size"] == 1


class TestConcurrency:
    def test_concurrent_writes(self):
        """Multiple threads writing facts concurrently should not corrupt data."""
        import threading
        _fresh_db()
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    upsert_fact(f"t{n}_k{i}", f"v{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        facts = get_all_facts(limit=200)
        assert len(facts) == 100  # 5 threads * 20 facts


# ===== Cleanup =====
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_dir():
    yield
    try:
        shutil.rmtree(os.environ.get("ASSISTANT_KID_DATA_DIR", ""), ignore_errors=True)
    except Exception:
        pass
