"""FR-14 批次 2 回归：数据层 uid 分片（`uid → 分片文件`）。

**背景**：批次 2 把 `_resolve_uid()` 落到 `preferences` / `persona` / `history` /
`memory_store` / `state_sync`，但此前**一条测试都没有**。
分片写错文件不会抛异常，只会静默串台 —— 本文件就是补这个护栏。

覆盖两层：

1. **路径层** —— `default` 保留原文件名（**不迁移**，老数据不用动），其他 uid 加
   `_{uid}` 后缀，不同 uid 路径互不相同、同一 uid 稳定。
2. **往返层** —— 以 uid A 写入后，A 读得到、B 读不到、`default` 读不到，且落盘文件名正确。

⚠️ **本轮排查确认的重要事实（务必知道）**：`bind_user()` / `set_session_user()` 目前
**只被测试调用，生产代码里没有任何地方绑定用户**（全仓 grep 只有 `user_context.py`
自己的 docstring 与 `tests/`）。所以：

- 批次 2 的分片**在生产中目前完全没生效** —— `resolve_user()` 一律回落到
  `os.environ["CHARLIE_USER_ID"]`（默认 `"default"`），所有数据仍写原文件名；
- 这同时意味着批次 2 是**零行为变更**（低风险，好）；
- 真正的多人隔离要等 FR-14 批次 4/5 在入口（`websocket.py` / `conversation.py` /
  `xiaozhi_ws.py`）用 `bind_user()` 包住整条处理链才生效。

另一个必须知道的限制：数据层的 `_resolve_uid(uid=None)` **拿不到 session_id**，
因此**看不到** `set_session_user()` 建立的 session→user 注册表，只能看到
ContextVar（`bind_user`）与环境变量。要按 session 解析，必须由调用方把
`resolve_user(session_id)` 的结果显式传下来（批次 4 的职责）。
"""
import json
import os
import shutil
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.history as hist_mod
import agent.memory_store as ms_mod
import agent.persona as persona_mod
import agent.preferences as pref_mod
import agent.state_sync as ss_mod
from agent import user_context as uc

LAOYU = "laoyu"
JIAREN = "jiaren1"


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path, monkeypatch):
    """把数据层各模块的数据目录指向临时目录，并清空按 uid 分键的内存缓存。

    ⚠️ `DATA_DIR` 在 preferences / persona / memory_store / state_sync 里都是
    **import 期**从 `os.environ` 读的模块常量，patch env 不够，必须直接改属性；
    只有 `history._data_dir()` 是调用期读 env，所以对 history 用 setenv。
    """
    tmp = str(tmp_path)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "PREFS_FILE", os.path.join(tmp, "preferences.json"))
    monkeypatch.setattr(persona_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(ms_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(ss_mod, "DATA_DIR", tmp)

    # 内存缓存快照/恢复（conftest 不拍这些键）
    snaps = {}
    for mod, names in (
        (pref_mod, ["_preferences", "_uid_prefs", "_uid_signatures",
                    "_uid_revisions", "_uid_save_seqs"]),
        (persona_mod, ["_states"]),
        (hist_mod, ["_sessions", "_history_file_caches", "_history_file_signatures"]),
        (ms_mod, ["_hot_fact_cache"]),
        (ss_mod, ["_local_state"]),
    ):
        for name in names:
            val = getattr(mod, name, None)
            if isinstance(val, dict):
                snaps[(mod, name)] = dict(val)
                val.clear()
    # default uid 的内存偏好是独立变量，单独清
    pref_mod._preferences.clear()
    persona_mod._states.clear()
    ms_mod._hot_fact_cache.clear()

    # ⚠️ history 的**自动落盘计数器**是模块级标量，conftest 不拍它。
    # `_append_history()` 每累计 `HISTORY_SAVE_EVERY`(=2) 次就调一次
    # `_save_history()`（**不带 uid** → 落到当前绑定用户）。不重置的话，
    # 本文件里前一个用例留下的计数会让后一个用例凭空多写一个分片文件，
    # 表现为「单独跑通过、全量跑失败」的顺序依赖 —— 实测就是这么挂的。
    hist_mod._history_append_count = 0
    hist_mod._history_save_seq = 0

    # 已建表路径集合：必须清，否则上一轮测试的 tmp 路径残留会让新分片库跳过建表。
    #
    # 用 getattr + isinstance 判断而不是直接取属性：在**旧代码**上（无
    # `_db_ready_paths`）本文件应当失败在**断言**上（证明分片行为确实是新的），
    # 而不是失败在 fixture 的 AttributeError 上 —— 后者会让全部用例因为同一个
    # 基础设施原因一起崩，完全证明不了测试本身的价值。
    ready = getattr(ms_mod, "_db_ready_paths", None)
    ready_snapshot = set(ready) if isinstance(ready, set) else None
    if isinstance(ready, set):
        ready.clear()

    uc.clear_all_session_users()
    yield
    uc.clear_all_session_users()
    for (mod, name), val in snaps.items():
        cur = getattr(mod, name, None)
        if isinstance(cur, dict):
            cur.clear()
            cur.update(val)
    hist_mod._history_append_count = 0
    hist_mod._history_save_seq = 0
    if isinstance(ready, set) and ready_snapshot is not None:
        ready.clear()
        ready.update(ready_snapshot)


@pytest.fixture(autouse=True)
def _no_ambient_uid(monkeypatch):
    """去掉环境变量兜底，保证断言只反映 ContextVar / 显式 uid。"""
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)


# ======================================================================
# 1. 路径层
# ======================================================================

class TestShardPaths:
    """default 保留原名（不迁移），其他 uid 用 `_{uid}` 后缀。"""

    def test_default_keeps_original_filenames(self, tmp_path):
        """`default` 必须落到改造前的原文件名 —— 否则老用户数据"消失"。"""
        assert pref_mod._prefs_file("default") == str(tmp_path / "preferences.json")
        assert persona_mod._persona_file("default") == str(tmp_path / "persona_state.json")
        assert hist_mod._get_history_file("default") == str(tmp_path / "conversation_history.json")
        assert ss_mod._get_sync_file("default") == str(tmp_path / "user_state_sync.json")

    def test_named_uid_gets_suffix(self, tmp_path):
        assert pref_mod._prefs_file(LAOYU) == str(tmp_path / f"preferences_{LAOYU}.json")
        assert persona_mod._persona_file(LAOYU) == str(tmp_path / f"persona_state_{LAOYU}.json")
        assert hist_mod._get_history_file(LAOYU) == str(tmp_path / f"conversation_history_{LAOYU}.json")
        assert ss_mod._get_sync_file(LAOYU) == str(tmp_path / f"user_state_sync_{LAOYU}.json")

    def test_distinct_uids_distinct_paths(self):
        """两个用户 + default，三者路径两两不同（防止分片算错退化成同一个文件）。"""
        for fn in (pref_mod._prefs_file, persona_mod._persona_file,
                   hist_mod._get_history_file, ss_mod._get_sync_file):
            paths = {fn("default"), fn(LAOYU), fn(JIAREN)}
            assert len(paths) == 3, f"{fn.__name__} 分片路径冲突: {paths}"

    def test_same_uid_stable(self):
        """同一 uid 多次调用路径稳定（否则同一用户会被写散到不同文件）。"""
        for fn in (pref_mod._prefs_file, persona_mod._persona_file,
                   hist_mod._get_history_file, ss_mod._get_sync_file):
            assert fn(LAOYU) == fn(LAOYU)

    def test_lock_file_is_path_plus_lock(self):
        assert pref_mod._prefs_lock_file(LAOYU) == pref_mod._prefs_file(LAOYU) + ".lock"
        assert hist_mod._get_history_lock_file(LAOYU) == hist_mod._get_history_file(LAOYU) + ".lock"

    def test_memory_db_path_follows_current_user(self):
        with uc.bind_user(LAOYU):
            assert ms_mod._get_db_path() == os.path.join(str(ms_mod.DATA_DIR), f"memory_{LAOYU}.db")
        with uc.bind_user(uc.DEFAULT_USER):
            assert ms_mod._get_db_path() == os.path.join(str(ms_mod.DATA_DIR), "memory.db")


# ======================================================================
# 2. 往返层（真隔离）
# ======================================================================

class TestPreferencesIsolation:
    def test_write_read_per_uid(self, tmp_path):
        pref_mod.set_preference("city", "北京", uid=LAOYU)
        pref_mod.set_preference("city", "上海", uid=JIAREN)

        assert pref_mod.get_preference("city", uid=LAOYU) == "北京"
        assert pref_mod.get_preference("city", uid=JIAREN) == "上海"
        assert pref_mod.get_preference("city") == "", "default 不该读到别人的偏好"

    def test_files_land_in_own_shard(self, tmp_path):
        pref_mod.set_preference("city", "北京", uid=LAOYU)
        assert (tmp_path / f"preferences_{LAOYU}.json").exists()
        assert not (tmp_path / f"preferences_{JIAREN}.json").exists()
        assert not (tmp_path / "preferences.json").exists()

        data = json.loads((tmp_path / f"preferences_{LAOYU}.json").read_text(encoding="utf-8"))
        assert data.get("city") == "北京"

    def test_del_preference_scoped_to_uid(self):
        pref_mod.set_preference("city", "北京", uid=LAOYU)
        pref_mod.set_preference("city", "上海", uid=JIAREN)
        pref_mod.del_preference("city", uid=LAOYU)
        assert pref_mod.get_preference("city", uid=LAOYU) == ""
        assert pref_mod.get_preference("city", uid=JIAREN) == "上海"

    def test_preference_count_per_uid(self):
        pref_mod.set_preference("a", "1", uid=LAOYU)
        pref_mod.set_preference("b", "2", uid=LAOYU)
        pref_mod.set_preference("c", "3", uid=JIAREN)
        assert pref_mod.preference_count(uid=LAOYU) == 2
        assert pref_mod.preference_count(uid=JIAREN) == 1
        assert pref_mod.preference_count() == 0


class TestPersonaIsolation:
    def test_relationship_level_per_uid(self, tmp_path):
        persona_mod.set_relationship_level("family", uid=LAOYU)
        assert persona_mod.get_relationship_level(uid=LAOYU) == "family"
        assert persona_mod.get_relationship_level(uid=JIAREN) == "acquaintance"
        assert persona_mod.get_relationship_level() == "acquaintance"

    def test_persona_file_sharded(self, tmp_path):
        persona_mod.set_relationship_level("friend", uid=LAOYU)
        assert (tmp_path / f"persona_state_{LAOYU}.json").exists()
        assert not (tmp_path / f"persona_state_{JIAREN}.json").exists()
        assert not (tmp_path / "persona_state.json").exists()

    def test_mood_per_uid(self):
        """情绪基调按 uid 隔离（它会被注入 system_msg 的动态段）。"""
        with persona_mod._STATE_LOCK:
            persona_mod._load(LAOYU)["mood"] = "cheerful"
        assert persona_mod.get_current_mood(uid=LAOYU) == "cheerful"
        assert persona_mod.get_current_mood(uid=JIAREN) != "cheerful"


class TestHistoryIsolation:
    def test_history_file_sharded(self, tmp_path):
        with uc.bind_user(LAOYU):
            hist = hist_mod._get_history("s-laoyu")
            hist_mod._append_history(hist, "你好", "你好呀")
            hist_mod._save_history()
        assert (tmp_path / f"conversation_history_{LAOYU}.json").exists()
        assert not (tmp_path / f"conversation_history_{JIAREN}.json").exists()
        assert not (tmp_path / "conversation_history.json").exists()

    def test_history_file_content_sharded(self, tmp_path):
        with uc.bind_user(LAOYU):
            hist = hist_mod._get_history("s-laoyu")
            hist_mod._append_history(hist, "老于的问题", "老于的答案")
            hist_mod._save_history()
        data = json.loads((tmp_path / f"conversation_history_{LAOYU}.json").read_text(encoding="utf-8"))
        assert "s-laoyu" in data
        assert data["s-laoyu"][0]["content"] == "老于的问题"

    def test_explicit_uid_wins_over_binding(self, tmp_path):
        """显式传 uid 时必须压过 ContextVar（否则跨用户写入会被静默改道）。"""
        with uc.bind_user(LAOYU):
            hist = hist_mod._get_history("s-1")
            hist_mod._append_history(hist, "u", "a")
            hist_mod._save_history(JIAREN)
        # 不变量：这次显式保存必须落到 JIAREN 的文件里
        assert (tmp_path / f"conversation_history_{JIAREN}.json").exists()
        assert "u" in (tmp_path / f"conversation_history_{JIAREN}.json").read_text(encoding="utf-8")
        # 不变量：LAOYU 的文件里**绝不能**出现这条消息。
        #
        # 这里刻意不断言"LAOYU 文件不存在"：`_append_history()` 内部有每 2 次
        # 自动落盘（`_save_history()` 不带 uid → 落到当前绑定用户），所以 LAOYU
        # 的文件完全可能被那次自动落盘创建出来，而它不该包含本次消息。
        # 断言"文件不存在"会把测试绑在自动落盘的副作用上，而不是要守的路由语义。
        laoyu_file = tmp_path / f"conversation_history_{LAOYU}.json"
        if laoyu_file.exists():
            assert '"u"' not in laoyu_file.read_text(encoding="utf-8")


class TestMemoryStoreIsolation:
    def test_db_file_sharded(self, tmp_path):
        with uc.bind_user(LAOYU):
            ms_mod.upsert_fact("city", "北京")
        assert (tmp_path / f"memory_{LAOYU}.db").exists()
        assert not (tmp_path / "memory.db").exists()

    def test_facts_not_visible_across_users(self, tmp_path):
        with uc.bind_user(LAOYU):
            ms_mod.upsert_fact("city", "北京")
            assert (ms_mod.get_fact("city") or {}).get("value") == "北京"
        with uc.bind_user(JIAREN):
            assert ms_mod.get_fact("city") is None
            ms_mod.upsert_fact("city", "上海")
            assert (ms_mod.get_fact("city") or {}).get("value") == "上海"
        with uc.bind_user(LAOYU):
            assert (ms_mod.get_fact("city") or {}).get("value") == "北京"


class TestStateSyncIsolation:
    """state_sync 的文件/topic 已按 uid 分片。

    ⚠️ 注意 `_local_state` 本身是**进程级**全局，所以这里只锁"落到哪个文件 / 发哪个
    topic"，不代表状态内容已按用户隔离。
    """

    def test_sync_file_sharded(self, tmp_path):
        assert ss_mod.write_shared_state({"user_state": {"mood": "happy"}}, uid=LAOYU) is True
        assert (tmp_path / f"user_state_sync_{LAOYU}.json").exists()
        assert not (tmp_path / f"user_state_sync_{JIAREN}.json").exists()
        assert not (tmp_path / "user_state_sync.json").exists()

    def test_round_trip_per_uid(self):
        ss_mod.write_shared_state({"a": 1}, uid=LAOYU)
        ss_mod.write_shared_state({"b": 2}, uid=JIAREN)
        assert ss_mod.read_shared_state(uid=LAOYU) == {"a": 1}
        assert ss_mod.read_shared_state(uid=JIAREN) == {"b": 2}

    def test_mqtt_topic_follows_uid(self, monkeypatch):
        """MQTT topic 必须是 `charlie/state/{uid}`，不再读进程级 env。

        注：`app/mqtt_server.py` **并不导出** `get_mqtt_client` / `_MQTT_AVAILABLE`
        （只有 `MqttXiaozhiServer`、`push_tts_to_mqtt`），所以 `_publish_mqtt` 的
        `from app.mqtt_server import ...` 实际抛 ImportError 并被 `except` 吞掉 ——
        MQTT 广播目前是死的。本用例用 `raising=False` 补上这两个名字，
        只为验证 topic 的构造逻辑本身正确（state_sync 整体是死代码，见文件末尾说明）。
        """
        published = []

        class _FakeClient:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append(topic)

        import app.mqtt_server as mqtt_mod
        monkeypatch.setattr(mqtt_mod, "_MQTT_AVAILABLE", True, raising=False)
        monkeypatch.setattr(mqtt_mod, "get_mqtt_client", lambda: _FakeClient(), raising=False)

        ss_mod._publish_mqtt({"x": 1}, uid=LAOYU)
        ss_mod._publish_mqtt({"x": 1}, uid=JIAREN)
        assert published == [f"charlie/state/{LAOYU}", f"charlie/state/{JIAREN}"]

    def test_mqtt_topic_ignores_raw_env(self, monkeypatch):
        """回归护栏：即使 env 里挂着别的 uid，显式 uid 也必须赢。

        改造前这 3 处直接读 `os.environ["CHARLIE_USER_ID"]`，env 一变 topic 就跟着变，
        与本进程正在服务的会话无关。
        """
        monkeypatch.setenv("CHARLIE_USER_ID", "somebody_else")
        published = []

        class _FakeClient:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append(topic)

        import app.mqtt_server as mqtt_mod
        monkeypatch.setattr(mqtt_mod, "_MQTT_AVAILABLE", True, raising=False)
        monkeypatch.setattr(mqtt_mod, "get_mqtt_client", lambda: _FakeClient(), raising=False)

        ss_mod._publish_mqtt({"x": 1}, uid=LAOYU)
        assert published == [f"charlie/state/{LAOYU}"]


# ======================================================================
# 3. resolve_user 是唯一入口
# ======================================================================

class TestResolveUidIsSingleEntry:
    def test_binding_drives_all_data_layers(self):
        with uc.bind_user(LAOYU):
            assert pref_mod._resolve_uid(None) == LAOYU
            assert persona_mod._resolve_uid(None) == LAOYU
            assert hist_mod._resolve_uid(None) == LAOYU
            assert ss_mod._resolve_uid(None) == LAOYU
            assert ms_mod._get_uid() == LAOYU

    def test_explicit_uid_beats_binding(self):
        with uc.bind_user(LAOYU):
            for fn in (pref_mod._resolve_uid, persona_mod._resolve_uid,
                       hist_mod._resolve_uid, ss_mod._resolve_uid):
                assert fn(JIAREN) == JIAREN

    def test_fallback_is_default_when_nothing_set(self):
        assert pref_mod._resolve_uid(None) == uc.DEFAULT_USER
        assert persona_mod._resolve_uid(None) == uc.DEFAULT_USER
        assert hist_mod._resolve_uid(None) == uc.DEFAULT_USER
        assert ss_mod._resolve_uid(None) == uc.DEFAULT_USER

    def test_env_used_only_as_last_resort(self, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", "from_env")
        assert pref_mod._resolve_uid(None) == "from_env"
        with uc.bind_user(LAOYU):
            assert pref_mod._resolve_uid(None) == LAOYU, "ContextVar 必须压过 env"

    def test_session_registry_not_visible_to_data_layer(self):
        """固化当前事实：`_resolve_uid()` 拿不到 session_id，因此看不到 session 注册表。

        这不是缺陷本身，而是**批次 4 的接口约束** —— 数据层要按会话解析，必须由调用方
        显式传 `resolve_user(session_id)` 的结果。此用例防止有人误以为
        `set_session_user()` 一调，全链路就自动隔离了。
        """
        uc.set_session_user("s-laoyu", LAOYU)
        assert uc.resolve_user("s-laoyu") == LAOYU
        assert pref_mod._resolve_uid(None) == uc.DEFAULT_USER, (
            "数据层无 session_id，不该凭空拿到 session 绑定的 uid"
        )
        assert pref_mod._resolve_uid(uc.resolve_user("s-laoyu")) == LAOYU


# ======================================================================
# 4. 端到端：一次"多用户并发写入"的完整往返
# ======================================================================

class TestTwoUsersRoundTrip:
    def test_no_cross_contamination(self, tmp_path):
        """两个用户各写一遍偏好/人格/记忆/历史，断言四类数据都各归各家。"""
        for uid, city, level, fact, msg in (
            (LAOYU, "北京", "family", "北京烤鸭", "老于说的话"),
            (JIAREN, "上海", "friend", "小笼包", "家人一说的话"),
        ):
            pref_mod.set_preference("city", city, uid=uid)
            persona_mod.set_relationship_level(level, uid=uid)
            with uc.bind_user(uid):
                ms_mod.upsert_fact("fav_food", fact)
                hist = hist_mod._get_history(f"s-{uid}")
                hist_mod._append_history(hist, msg, "好的")
                hist_mod._save_history()

        assert pref_mod.get_preference("city", uid=LAOYU) == "北京"
        assert pref_mod.get_preference("city", uid=JIAREN) == "上海"
        assert persona_mod.get_relationship_level(uid=LAOYU) == "family"
        assert persona_mod.get_relationship_level(uid=JIAREN) == "friend"
        with uc.bind_user(LAOYU):
            assert (ms_mod.get_fact("fav_food") or {}).get("value") == "北京烤鸭"
        with uc.bind_user(JIAREN):
            assert (ms_mod.get_fact("fav_food") or {}).get("value") == "小笼包"

        for uid, msg in ((LAOYU, "老于说的话"), (JIAREN, "家人一说的话")):
            data = json.loads(
                (tmp_path / f"conversation_history_{uid}.json").read_text(encoding="utf-8"))
            assert data[f"s-{uid}"][0]["content"] == msg

        # 四个模块各自只该产生自己那两个分片，default 一个都不该出现
        names = {p.name for p in tmp_path.iterdir()}
        assert "preferences.json" not in names
        assert "persona_state.json" not in names
        assert "conversation_history.json" not in names
        assert "memory.db" not in names
