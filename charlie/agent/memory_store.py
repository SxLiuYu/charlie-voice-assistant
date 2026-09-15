"""长期记忆 SQLite 持久化存储

解决 magic-memory.py 使用 JSON 文件的痛点:
  - 大量记忆时文件 scan 慢(O(n))
  - 无结构化查询(按 category / time_range / tag)
  - 并发写入时全文件锁(RLock + tmp+replace 仍有ABA风险)
  - 刷新语义记忆无增量机制(全量重算)

SQLite 优势:
  - 索引查询 O(log n)
  - 内置事务 + MVCC 并发安全
  - WAL 模式允许并发读写(多线程brain + 调度器同时访问)
  - single file, 原子操作, crash-safe

Schema:
  facts       - 用户事实/偏好 (key-value, 带时间衰减 access_count)
  events      - 对话事件 (episodic memory, 带 bigram 字段)
  sessions    - 会话元数据 (channel, turn_count, summary)
  corrections - 记忆修正记录 (可追溯修正历史)

LRU Cache:
  startup 时加载高访问量事实(hot_facts)到内存, 后续读取优先走 cache,
  cache miss 才查 SQLite, 写完 SQLite 后升级 cache entry。
  限制: _HOT_FACT_CACHE_MAX = 500 条

集成方式:
  magic-memory.py → remember_conversation() 同时写入 SQLite events + JSON file
  brain() → _build_system_msg() 先从 SQLite 查高权记忆, 回退 JSON
  增量语义记忆: 事实变更时只刷新受影响的 semantic entries
"""
import os
import sqlite3
import json
import time
import re
import threading
import logging
from collections import OrderedDict
from datetime import datetime, timezone

log = logging.getLogger("magic")

DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))


def _get_uid() -> str:
    return os.environ.get("CHARLIE_USER_ID", "default")


def _get_db_path() -> str:
    uid = _get_uid()
    name = f"memory_{uid}.db" if uid != "default" else "memory.db"
    return os.path.join(DATA_DIR, name)


# ===== 线程安全锁 =====
_db_lock = threading.RLock()

# ===== LRU 热事实缓存 =====
_HOT_FACT_CACHE_MAX = 500
_hot_fact_cache: OrderedDict[str, dict] = OrderedDict()
_hot_cache_lock = threading.Lock()


# ===== Schema & 初始化 =====

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,          -- 事实标识 "user_name", "pet_type"
    value       TEXT NOT NULL,                  -- 事实内容
    category    TEXT DEFAULT 'fact',            -- "personal", "preference", "habit", "fact"
    confidence  REAL DEFAULT 1.0,               -- 置信度 0.0~1.0
    source      TEXT DEFAULT 'extracted',       -- "extracted", "manual", "corrected"
    created_at  TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    access_count INTEGER DEFAULT 1,             -- 访问次数(用于热缓存排序)
    last_accessed TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,               -- session_id
    channel      TEXT DEFAULT '',               -- "feishu", "websocket", "xiaozhi"
    started_at   TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at     TIMESTAMP,
    turn_count   INTEGER DEFAULT 0,
    summary      TEXT DEFAULT '',
    tags         TEXT DEFAULT '[]'              -- JSON array
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    user_text   TEXT NOT NULL,
    summary     TEXT NOT NULL,
    tags        TEXT DEFAULT '[]',              -- JSON array
    category    TEXT DEFAULT 'general',
    bigrams     TEXT DEFAULT '{}',              -- JSON dict (bigram vector)
    sentiment   TEXT DEFAULT '',                -- "positive", "negative", "neutral"
    created_at  TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key    TEXT NOT NULL,                  -- 被修正的事实 key
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS semantic_knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key    TEXT NOT NULL,                  -- 关联的 fact key
    knowledge_type TEXT DEFAULT '',             -- "preference", "habit", "attribute"
    knowledge_sub TEXT DEFAULT '',              -- "like", "dislike", "routine", "fact"
    key         TEXT DEFAULT '',                -- 提取的关键词
    value       TEXT DEFAULT '',                -- 提取的语义值
    confidence  REAL DEFAULT 0.8,
    source_mem  TEXT DEFAULT '',                -- 来源记忆日期
    source_value TEXT DEFAULT '',               -- 创建时的 fact value(用于增量比较)
    created_at  TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_tags ON events(tags);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_accessed ON facts(last_accessed DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
"""


def _init_db() -> None:
    """初始化数据库(创建表 + 索引), 幂等。"""
    db_path = _get_db_path()
    try:
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        with _db_lock:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
    except Exception as e:
        log.warning(f"[memory_store] init_db 失败: {e}")


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接(每次调用新建, 使用 with 管理生命周期)。"""
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ===== 热事实缓存 LRU =====

def _cache_hot_facts() -> int:
    """启动时加载高频访问事实到内存 cache。返回缓存条目数。"""
    global _hot_fact_cache
    _hot_fact_cache.clear()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value, category, confidence, source, access_count "
                "FROM facts ORDER BY access_count DESC LIMIT ?",
                (_HOT_FACT_CACHE_MAX,)
            ).fetchall()
            for row in rows:
                _hot_fact_cache[row["key"]] = {
                    "value": row["value"],
                    "category": row["category"],
                    "confidence": row["confidence"],
                    "source": row["source"],
                    "access_count": row["access_count"],
                }
    except Exception as e:
        log.warning(f"[memory_store] 热缓存加载失败: {e}")
    return len(_hot_fact_cache)


def _warm_cache_on_read(key: str, entry: dict) -> None:
    """Update cache entry (LRU slide). Called after DB writes too, so
    always refresh the stored value."""
    with _hot_cache_lock:
        if key in _hot_fact_cache:
            _hot_fact_cache[key].update(entry)
            _hot_fact_cache.move_to_end(key)
        else:
            if len(_hot_fact_cache) >= _HOT_FACT_CACHE_MAX:
                _hot_fact_cache.popitem(last=False)
            _hot_fact_cache[key] = entry


def get_hot_fact(key: str) -> dict | None:
    """从热缓存读取事实(SQLite 不落盘)。"""
    with _hot_cache_lock:
        entry = _hot_fact_cache.get(key)
        if entry:
            _hot_fact_cache.move_to_end(key)
            return dict(entry)
    return None


# ===== Facts CRUD =====

def upsert_fact(key: str, value: str, category: str = "fact",
                confidence: float = 1.0, source: str = "extracted") -> None:
    """插入或更新事实(UPSERT)。
    同时更新 SQLite 和热缓存。
    """
    if not key:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")
    entry = {
        "value": value,
        "category": category,
        "confidence": confidence,
        "source": source,
        "access_count": 1,
    }
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO facts (key, value, category, confidence, source, access_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, category=excluded.category, "
                "confidence=excluded.confidence, source=excluded.source, "
                "updated_at=excluded.updated_at, "
                "access_count=facts.access_count + 1, "
                "last_accessed=excluded.updated_at",
                (key, value, category, confidence, source, now)
            )
            conn.commit()
    except Exception as e:
        log.warning(f"[memory_store] upsert_fact 失败: {e}")
        return
    _warm_cache_on_read(key, entry)


def get_fact(key: str) -> dict | None:
    """读取事实(优先热缓存, cache miss 查 SQLite)。"""
    entry = get_hot_fact(key)
    if entry:
        return entry
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "UPDATE facts SET access_count=access_count+1, last_accessed=? "
                "WHERE key=? RETURNING value, category, confidence, source, access_count",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ"), key)
            ).fetchone()
            if row:
                entry = {
                    "value": row["value"],
                    "category": row["category"],
                    "confidence": row["confidence"],
                    "source": row["source"],
                    "access_count": row["access_count"],
                }
                _warm_cache_on_read(key, entry)
                return entry
    except Exception as e:
        log.debug(f"[memory_store] get_fact cache miss: {e}")
    return None


def get_facts_by_category(category: str, limit: int = 100) -> list[dict]:
    """按分类批量读取事实。"""
    rows = []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value, category, confidence, source, access_count "
                "FROM facts WHERE category=? ORDER BY updated_at DESC LIMIT ?",
                (category, limit)
            ).fetchall()
    except Exception as e:
        log.debug(f"[memory_store] get_facts_by_category 失败: {e}")
    return [dict(r) for r in rows]


def get_all_facts(limit: int = 1000) -> list[dict]:
    """读取所有事实(按更新倒序)。"""
    rows = []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value, category, confidence, source, updated_at "
                "FROM facts ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
    except Exception as e:
        log.debug(f"[memory_store] get_all_facts 失败: {e}")
    return [dict(r) for r in rows]


def delete_fact(key: str) -> bool:
    """删除事实。返回是否删除成功。"""
    try:
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM facts WHERE key=?", (key,))
            conn.commit()
            if cur.rowcount > 0:
                with _hot_cache_lock:
                    _hot_fact_cache.pop(key, None)
                return True
    except Exception as e:
        log.warning(f"[memory_store] delete_fact 失败: {e}")
    return False


def record_correction(fact_key: str, old_value: str, new_value: str, reason: str = "") -> None:
    """记录事实修正历史。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO corrections (fact_key, old_value, new_value, reason) "
                "VALUES (?, ?, ?, ?)",
                (fact_key, old_value, new_value, reason[:200])
            )
            conn.commit()
    except Exception as e:
        log.debug(f"[memory_store] record_correction 失败: {e}")


# ===== Sessions CRUD =====

def start_session(session_id: str, channel: str = "") -> None:
    """记录会话开始。"""
    if not session_id:
        return
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, channel, started_at) VALUES (?, ?, ?)",
                (session_id, channel, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ"))
            )
            conn.commit()
    except Exception as e:
        log.debug(f"[memory_store] start_session 失败: {e}")


def end_session(session_id: str, turn_count: int = 0, summary: str = "", tags: list | None = None) -> None:
    """结束会话并写摘要。"""
    if not session_id:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    try:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at=?, turn_count=?, summary=?, tags=? WHERE id=?",
                (now, turn_count, summary[:500], tags_json, session_id)
            )
            conn.commit()
    except Exception as e:
        log.debug(f"[memory_store] end_session 失败: {e}")


def get_recent_sessions(limit: int = 10) -> list[dict]:
    """读取最近的会话快照。"""
    rows = []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT id, channel, started_at, ended_at, turn_count, summary "
                "FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
    except Exception as e:
        log.debug(f"[memory_store] get_recent_sessions 失败: {e}")
    return [dict(r) for r in rows]


# ===== Events CRUD =====

def add_event(session_id: str, user_text: str, summary: str,
              tags: list | None = None, category: str = "general",
              bigrams: dict | None = None, sentiment: str = "") -> int:
    """添加对话事件(episodic memory), 返回 event id。"""
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    bigrams_json = json.dumps(bigrams or {}, ensure_ascii=False)
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, user_text, summary, tags, category, bigrams, sentiment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_text[:500], summary[:200],
                 tags_json, category, bigrams_json, sentiment[:50])
            )
            conn.commit()
            return cur.lastrowid
    except Exception as e:
        log.warning(f"[memory_store] add_event 失败: {e}")
        return -1


def _parse_json_field(value, default):
    """Safely parse a JSON field from SQLite, returning default on failure."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_events(limit: int = 50, category: str = "", since_hours: float = 0) -> list[dict]:
    """读取事件列表, 按创建时间倒序。"""
    rows = []
    params = []
    where = []
    if category:
        where.append("category = ?")
        params.append(category)
    if since_hours > 0:
        cutoff = time.time() - since_hours * 3600
        where.append("created_at > ?")
        params.append(datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ"))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, session_id, user_text, summary, tags, category, sentiment, created_at "
                f"FROM events {where_sql} ORDER BY created_at DESC LIMIT ?",
                (*params, limit)
            ).fetchall()
    except Exception as e:
        log.debug(f"[memory_store] get_events 失败: {e}")
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_json_field(d.get("tags"), [])
        result.append(d)
    return result


def get_events_by_tags(tags: list[str], limit: int = 20) -> list[dict]:
    """按标签查询事件(包含任一标签即可)。"""
    rows = []
    try:
        with _get_conn() as conn:
            for tag in tags:
                rows.extend(conn.execute(
                    "SELECT id, session_id, user_text, summary, tags, category, sentiment, created_at "
                    "FROM events WHERE tags LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f'%"{tag}"%', limit)
                ).fetchall())
    except Exception as e:
        log.debug(f"[memory_store] get_events_by_tags 失败: {e}")
    # 去重(按 id) + 解析 JSON tags
    seen = set()
    unique = []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            d = dict(r)
            d["tags"] = _parse_json_field(d.get("tags"), [])
            unique.append(d)
    return unique[:limit]


# ===== Semantic Knowledge =====

def store_semantic_knowledge(fact_key: str, knowledge_type: str, knowledge_sub: str,
                              key: str, value: str, confidence: float = 0.8,
                              source_mem: str = "", source_value: str = "") -> None:
    """存储语义知识(增量, 不影响其他事实的 semantic entries)。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO semantic_knowledge "
                "(fact_key, knowledge_type, knowledge_sub, key, value, confidence, source_mem, source_value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fact_key, knowledge_type, knowledge_sub,
                 key[:50], value[:100], confidence, source_mem[:50], source_value[:200])
            )
            conn.commit()
    except Exception as e:
        log.debug(f"[memory_store] store_semantic_knowledge 失败: {e}")


def get_semantic_knowledge(knowledge_type: str = "", limit: int = 50) -> list[dict]:
    """读取语义知识。"""
    rows = []
    try:
        with _get_conn() as conn:
            if knowledge_type:
                rows = conn.execute(
                    "SELECT id, fact_key, knowledge_type, knowledge_sub, key, value, confidence "
                    "FROM semantic_knowledge WHERE knowledge_type=? ORDER BY created_at DESC LIMIT ?",
                    (knowledge_type, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, fact_key, knowledge_type, knowledge_sub, key, value, confidence "
                    "FROM semantic_knowledge ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
    except Exception as e:
        log.debug(f"[memory_store] get_semantic_knowledge 失败: {e}")
    return [dict(r) for r in rows]


def refresh_semantic_memory() -> int:
    """增量刷新语义知识: 只处理自上次刷新后有变更的事实。
    返回新增/更新的语义条目数。
    """
    updated = 0
    try:
        with _get_conn() as conn:
            # 获取需要刷新的 facts(最近更新的, 且需要 refresh 的)
            facts = conn.execute(
                "SELECT key, value FROM facts WHERE updated_at >= datetime('now', '-1 day')"
            ).fetchall()
            for fact in facts:
                key, val = fact["key"], fact["value"]
                # 检查是否已有 semantic entry with matching source_value
                existing = conn.execute(
                    "SELECT id, source_value FROM semantic_knowledge WHERE fact_key=? LIMIT 1",
                    (key,)
                ).fetchone()
                if existing and existing["source_value"] == val:
                    continue  # 值未变, 跳过
                if existing:
                    # value 变了, 删除旧 entries
                    conn.execute("DELETE FROM semantic_knowledge WHERE fact_key=?", (key,))
                # 重新提取语义知识
                knowledges = _extract_semantic_from_fact(key, val)
                for ktype, ksub, k, v in knowledges:
                    conn.execute(
                        "INSERT INTO semantic_knowledge "
                        "(fact_key, knowledge_type, knowledge_sub, key, value, source_value) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (key, ktype, ksub, k[:50], v[:100], val[:200])
                    )
                    updated += 1
            conn.commit()
    except Exception as e:
        log.warning(f"[memory_store] refresh_semantic_memory 失败: {e}")
    return updated


_PREF_FACT_PATTERNS = [
    (r"我喜欢(.+?)(?:[，。\n]|$)", "preference", "like"),
    (r"我讨厌(.+?)(?:[，。\n]|$)", "preference", "dislike"),
    (r"我习惯(.+?)(?:[，。\n]|$)", "habit", "routine"),
    (r"我通常(.+?)(?:[，。\n]|$)", "habit", "routine"),
    (r"我偏好(.+?)(?:[，。\n]|$)", "preference", "like"),
    (r"我的(.+?)是(.+?)(?:[，。\n]|$)", "attribute", "fact"),
    (r"我叫(.+?)(?:[，。\n]|$)", "personal", "name"),
    (r"我的生日是(.+?)(?:[，。\n]|$)", "personal", "birthday"),
]


def _extract_semantic_from_fact(key: str, value: str) -> list:
    """从事实值中提取语义知识。"""
    knowledges = []
    text = f"{key} {value}"
    for pattern, ktype, ksub in _PREF_FACT_PATTERNS:
        matches = __import__("re").findall(pattern, text)
        for m in matches:
            knowledges.append((ktype, ksub, m.strip()[:30], m.strip()[:60]))
    return knowledges


# ===== 统计 & 调试 =====

def get_memory_stats() -> dict:
    """记忆系统统计信息。"""
    stats = {"facts": 0, "events": 0, "sessions": 0, "corrections": 0,
             "semantic_knowledge": 0, "cache_size": 0}
    try:
        with _get_conn() as conn:
            for table in ["facts", "events", "sessions", "corrections", "semantic_knowledge"]:
                row = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()
                stats[table] = row["c"]
    except Exception as e:
        log.debug(f"[memory_store] get_memory_stats 失败: {e}")
    with _hot_cache_lock:
        stats["cache_size"] = len(_hot_fact_cache)
    return stats


def warm_up_cache() -> int:
    """公共接口: 初始化并预热热缓存。返回缓存条目数。"""
    _init_db()
    return _cache_hot_facts()


def format_memories_for_prompt(query: str, limit: int = 3) -> str:
    """从 SQLite 检索相关事件, 格式化为 system prompt 注入片段。
    与 magic-memory.py 的 format_memories_for_prompt 互补:
    - magic-memory: 叙事记忆(bigram 语义检索, 适合深度关联)
    - memory_store: 结构化事件(索引查询, 适合时间/标签筛选)
    """
    events = get_events(limit=limit * 2)  # 多取一些, 下面再筛选
    if not events:
        return ""
    # 简单关键词匹配筛选(与 query 有共现字符的优先)
    scored = []
    q_chars = set(query)
    for ev in events:
        text = ev.get("user_text", "") + " " + ev.get("summary", "")
        overlap = len(q_chars & set(text))
        scored.append((overlap, ev))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ev for _, ev in scored[:limit]]
    lines = []
    for ev in top:
        dt = ev.get("created_at", "")[:16].replace("T", " ")
        summary = ev.get("summary", "")
        lines.append(f"[{dt}] {summary}")
    if not lines:
        return ""
    return "近期事件：\n" + "\n".join(lines)


# ===== 便捷集成接口 =====

def remember_to_store(user_text: str, summary: str, session_id: str = "",
                      tags: list | None = None, category: str = "general") -> int:
    """写入对话事件到 SQLite。magic-memory.py remember_conversation() 调用此函数
    实现双写: JSON (magic-memory) + SQLite (memory_store)。
    返回 event id 或 -1。
    """
    if not user_text or not summary:
        return -1
    return add_event(session_id or "default", user_text, summary, tags, category)


def extract_and_store_facts(user_text: str) -> int:
    """从用户文本中提取事实并写入 SQLite。返回写入的事实数。"""
    import re as _re
    stored = 0
    for pattern, ktype, ksub in _PREF_FACT_PATTERNS:
        matches = _re.findall(pattern, user_text)
        for m in matches:
            val = m.strip() if isinstance(m, str) else "".join(m).strip()
            if val:
                upsert_fact(val, val, category=ktype, confidence=0.8, source="extracted")
                stored += 1
    return stored


# ===== 初始化(模块导入时自动执行) =====
try:
    _init_db()
    if not __import__("os").environ.get("CHARLIE_TEST_MODE"):
        _cache_hot_facts()
except Exception:
    pass  # 容错: 首次初始化失败不影响模块导入
