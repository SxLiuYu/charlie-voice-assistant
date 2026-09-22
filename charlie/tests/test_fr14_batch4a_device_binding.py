"""FR-14 批次 4a 回归：设备 → 用户映射 + 入口绑定（`agent/device_users.py`）。

**背景**：批次 2（数据层分片）与批次 3A（动态段移出共享 brain）都已落地，但
`bind_user()` / `set_session_user()` **在生产代码里零调用** —— `resolve_user()`
一律回落 `os.environ["CHARLIE_USER_ID"]`（默认 `"default"`），隔离形同虚设。
本批把「设备身份 → 用户」接到 ESP32 入口，让前两批真正生效。

覆盖三层：

1. **映射层** —— `device_users` 的文件读写、uid 校验、缓存失效、env 覆盖。
2. **绑定层** —— 入口包装（`bound_device_user` + `bind_user`）是否真的让数据层落到
   正确的分片；**未绑定设备必须不 bind**（否则会把 env 兜底盖掉，见下）。
3. **并发层** —— 两台设备同时说话，数据互不污染（FR-14 的核心承诺）。

⚠️ 本文件最要紧的一条断言是
`TestEntryBinding::test_unbound_device_does_not_mask_env_fallback`：
`bind_user("default")` 会盖掉 `CHARLIE_USER_ID=laoyu` 的兜底，表现是"记忆凭空消失"。
这是本批最容易写错、后果最严重的一处。
"""
import json
import os
import sys
import threading

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.device_users as du_mod
import agent.history as hist_mod
import agent.memory_store as ms_mod
import agent.persona as persona_mod
import agent.preferences as pref_mod
import agent.state_sync as ss_mod
from agent import user_context as uc

LAOYU = "laoyu"
JIAREN = "jiaren1"
JIAREN2 = "jiaren2"

DEV_A = "aa:bb:cc:dd:ee:01"
DEV_B = "aa:bb:cc:dd:ee:02"


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path, monkeypatch):
    """把数据层与映射文件都指向临时目录，并清掉内存缓存。

    ⚠️ `DATA_DIR` 在这几个模块里都是 **import 期**读 `os.environ` 的模块常量，
    只 patch env 不够，必须直接改属性。
    """
    tmp = str(tmp_path)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "PREFS_FILE", os.path.join(tmp, "preferences.json"))
    monkeypatch.setattr(persona_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(ms_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(ss_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(du_mod, "DATA_DIR", tmp)
    # 映射的 env 覆盖默认清空，各用例按需设置
    monkeypatch.delenv(du_mod._ENV_VAR, raising=False)

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
    pref_mod._preferences.clear()
    persona_mod._states.clear()
    ms_mod._hot_fact_cache.clear()
    du_mod.reset_cache()

    # history 的自动落盘计数器是模块级标量，conftest 不拍（见 batch2 测试的教训）
    hist_mod._history_append_count = 0
    hist_mod._history_save_seq = 0

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
    du_mod.reset_cache()
    if isinstance(ready, set) and ready_snapshot is not None:
        ready.clear()
        ready.update(ready_snapshot)


@pytest.fixture(autouse=True)
def _no_ambient_uid(monkeypatch):
    """默认去掉 env 兜底，断言只反映显式绑定。需要测兜底的用例自己设回来。"""
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)


def _map_file(tmp_path):
    return tmp_path / "device_users.json"


def _write_map(tmp_path, devices):
    _map_file(tmp_path).write_text(
        json.dumps({"version": 1, "devices": devices}, ensure_ascii=False),
        encoding="utf-8")
    du_mod.reset_cache()


# ======================================================================
# 1. 映射层
# ======================================================================

class TestSanitizeUid:
    def test_accepts_normal_names(self):
        for uid in ("laoyu", "jiaren1", "妈妈", "user-2", "a_b", "A1"):
            assert du_mod.sanitize_uid(uid) == uid

    def test_rejects_default_and_empty(self):
        assert du_mod.sanitize_uid("default") is None
        assert du_mod.sanitize_uid("") is None
        assert du_mod.sanitize_uid(None) is None
        assert du_mod.sanitize_uid("   ") is None

    @pytest.mark.parametrize("bad", [
        "../etc/passwd", "a/b", "a\\b", "a b", "a..b", "x" * 33, "a.b", "a$b",
    ])
    def test_rejects_path_traversal_and_illegal_chars(self, bad):
        """uid 会拼进分片文件名，`/` 与 `..` 必须挡住（否则能写出目录）。"""
        assert du_mod.sanitize_uid(bad) is None


class TestMapFileRoundTrip:
    def test_set_then_get(self, tmp_path):
        assert du_mod.set_device_user(DEV_A, LAOYU) is True
        assert du_mod.get_device_user(DEV_A) == LAOYU
        assert du_mod.get_device_user(DEV_B) is None

    def test_set_is_idempotent(self, tmp_path):
        assert du_mod.set_device_user(DEV_A, LAOYU) is True
        assert du_mod.set_device_user(DEV_A, LAOYU) is False  # 无变更

    def test_persists_to_disk(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        raw = json.loads(_map_file(tmp_path).read_text(encoding="utf-8"))
        assert raw["devices"][DEV_A] == LAOYU

    def test_rebinds_to_another_user(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        assert du_mod.set_device_user(DEV_A, JIAREN) is True
        assert du_mod.get_device_user(DEV_A) == JIAREN

    def test_remove(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        assert du_mod.remove_device_user(DEV_A) is True
        assert du_mod.get_device_user(DEV_A) is None
        assert du_mod.remove_device_user(DEV_A) is False  # 已不存在

    def test_set_default_means_unbind(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        assert du_mod.set_device_user(DEV_A, "default") is True
        assert du_mod.get_device_user(DEV_A) is None

    def test_illegal_uid_raises(self, tmp_path):
        """API 传入非法 uid 必须显式报错，不能静默丢弃。"""
        with pytest.raises(ValueError):
            du_mod.set_device_user(DEV_A, "../evil")

    def test_empty_device_key_raises(self, tmp_path):
        with pytest.raises(ValueError):
            du_mod.set_device_user("   ", LAOYU)


class TestExternalFileEdits:
    def test_cache_invalidated_on_file_change(self, tmp_path):
        """人工改 JSON 后无需重启服务 —— 靠 mtime/size 指纹失效。"""
        _write_map(tmp_path, {DEV_A: LAOYU})
        assert du_mod.get_device_user(DEV_A) == LAOYU
        assert du_mod.get_device_user(DEV_B) is None
        # 外部改写
        _write_map(tmp_path, {DEV_B: JIAREN})
        assert du_mod.get_device_user(DEV_A) is None
        assert du_mod.get_device_user(DEV_B) == JIAREN

    def test_missing_file_is_empty_map(self, tmp_path):
        assert du_mod.all_mappings() == {}
        assert du_mod.stats()["map_file_exists"] is False

    def test_corrupt_file_does_not_raise(self, tmp_path):
        _map_file(tmp_path).write_text("{ this is not json", encoding="utf-8")
        du_mod.reset_cache()
        assert du_mod.all_mappings() == {}

    def test_illegal_entries_in_file_are_dropped(self, tmp_path):
        """文件里混入非法 uid / 空 key 时只丢那一条，不影响其他条目。"""
        _write_map(tmp_path, {DEV_A: LAOYU, "": JIAREN, DEV_B: "../evil"})
        m = du_mod.all_mappings()
        assert m == {DEV_A: LAOYU}


class TestEnvOverride:
    def test_env_supplies_mapping(self, tmp_path, monkeypatch):
        monkeypatch.setenv(du_mod._ENV_VAR, json.dumps({DEV_A: LAOYU}))
        assert du_mod.get_device_user(DEV_A) == LAOYU

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        _write_map(tmp_path, {DEV_A: JIAREN})
        monkeypatch.setenv(du_mod._ENV_VAR, json.dumps({DEV_A: LAOYU}))
        assert du_mod.get_device_user(DEV_A) == LAOYU

    def test_env_accepts_flat_object(self, tmp_path, monkeypatch):
        """允许 `{"dev":"uid"}` 直写，不必套 `{"devices": {...}}`。"""
        monkeypatch.setenv(du_mod._ENV_VAR, json.dumps({DEV_A: LAOYU}))
        assert du_mod.get_device_user(DEV_A) == LAOYU

    def test_bad_env_json_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv(du_mod._ENV_VAR, "not json at all")
        assert du_mod.all_mappings() == {}

    def test_env_entries_not_written_into_file(self, tmp_path, monkeypatch):
        """set_device_user 以**文件内容**为基准改，不能把 env 条目固化进文件。"""
        monkeypatch.setenv(du_mod._ENV_VAR, json.dumps({DEV_B: JIAREN}))
        du_mod.set_device_user(DEV_A, LAOYU)
        raw = json.loads(_map_file(tmp_path).read_text(encoding="utf-8"))
        assert DEV_A in raw["devices"]
        assert DEV_B not in raw["devices"]


class TestResolveSemantics:
    def test_resolve_falls_back_to_default(self, tmp_path):
        assert du_mod.resolve_device_user(DEV_A) == uc.DEFAULT_USER

    def test_resolve_returns_bound_user(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        assert du_mod.resolve_device_user(DEV_A) == LAOYU

    def test_bound_returns_none_when_unbound(self, tmp_path):
        """`bound_device_user` 未绑定必须返回 None（入口层靠它决定"不 bind"）。"""
        assert du_mod.bound_device_user(DEV_A) is None

    def test_bound_returns_user_when_bound(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        assert du_mod.bound_device_user(DEV_A) == LAOYU

    def test_bound_none_for_empty_key(self, tmp_path):
        assert du_mod.bound_device_user("") is None
        assert du_mod.bound_device_user(None) is None


# ======================================================================
# 2. 入口绑定层
# ======================================================================

def _handle_utterance(device_key, sink):
    """复刻 `app/xiaozhi_ws.py::_process_utterance_bound` 的绑定语义。

    刻意不 import xiaozhi_ws（会拉起 torch/opuslib 等重依赖）。这里复刻的是
    **决策逻辑**本身：拿到真实 uid 才 bind，未绑定就完全不 bind。
    对应源码见 `app/xiaozhi_ws.py` 的 `_process_utterance_bound`。
    """
    uid = du_mod.bound_device_user(device_key)
    if uid is None:
        sink.append(uc.resolve_user())
        return
    with uc.bind_user(uid):
        sink.append(uc.resolve_user())


class TestEntryBinding:
    def test_bound_device_binds_user(self, tmp_path):
        du_mod.set_device_user(DEV_A, LAOYU)
        seen = []
        _handle_utterance(DEV_A, seen)
        assert seen == [LAOYU]

    def test_unbound_device_does_not_mask_env_fallback(self, tmp_path, monkeypatch):
        """⚠️ 本批最关键的一条。

        未绑定设备若 bind("default")，会把 `CHARLIE_USER_ID` 兜底盖掉 ——
        单用户老配置（env=laoyu）会突然变成 default，表现是"数据凭空消失"。
        """
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        seen = []
        _handle_utterance(DEV_A, seen)   # DEV_A 未绑定
        assert seen == [LAOYU], "未绑定设备必须保留 env 兜底，不能被 default 盖掉"

    def test_unbound_device_falls_back_to_default_when_no_env(self, tmp_path):
        seen = []
        _handle_utterance(DEV_A, seen)
        assert seen == [uc.DEFAULT_USER]

    def test_binding_does_not_leak_to_next_utterance(self, tmp_path):
        """bind 的作用域必须只覆盖本次执行流。"""
        du_mod.set_device_user(DEV_A, LAOYU)
        seen = []
        _handle_utterance(DEV_A, seen)
        _handle_utterance(DEV_B, seen)   # 未绑定
        assert seen == [LAOYU, uc.DEFAULT_USER]

    def test_data_layers_follow_the_binding(self, tmp_path):
        """绑定后 5 个数据层必须都落到该用户的分片（批次 2 的隔离真正生效）。"""
        du_mod.set_device_user(DEV_A, LAOYU)
        with uc.bind_user(du_mod.bound_device_user(DEV_A)):
            pref_mod.set_preference("city", "北京")
            persona_mod.set_relationship_level("family")
            ms_mod.upsert_fact("fav_food", "北京烤鸭")
            hist = hist_mod._get_history("s-1")
            hist_mod._append_history(hist, "你好", "你好呀")
            hist_mod._save_history()
        assert (tmp_path / f"preferences_{LAOYU}.json").exists()
        assert (tmp_path / f"persona_state_{LAOYU}.json").exists()
        assert (tmp_path / f"memory_{LAOYU}.db").exists()
        assert (tmp_path / f"conversation_history_{LAOYU}.json").exists()
        # 且都没有落到 default
        assert not (tmp_path / "preferences.json").exists()
        assert not (tmp_path / "memory.db").exists()
        assert not (tmp_path / "conversation_history.json").exists()

    def test_set_session_user_makes_session_id_lookup_work(self, tmp_path):
        """`set_session_user` 让 `resolve_user(session_id)` 命中注册表。

        `agent/llm.py::_resolve_uid(session_id)` 走这条路径，负责动态 system prompt
        里"给谁看"的部分 —— 与数据层的 ContextVar 路径**互补，缺一不可**。
        """
        du_mod.set_device_user(DEV_A, LAOYU)
        session_id = f"xiaozhi-{DEV_A}"
        uc.set_session_user(session_id, du_mod.bound_device_user(DEV_A))
        assert uc.resolve_user(session_id) == LAOYU

    def test_unbound_device_clears_session_registry(self, tmp_path):
        """未绑定设备必须把注册表清干净，不能残留上一台设备的身份。"""
        session_id = f"xiaozhi-{DEV_A}"
        uc.set_session_user(session_id, LAOYU)
        # 同一 session_id 改为未绑定
        uc.set_session_user(session_id, du_mod.bound_device_user(DEV_A))
        assert uc.get_session_user(session_id) is None


# ======================================================================
# 3. 并发隔离（FR-14 的核心承诺）
# ======================================================================

class TestConcurrentDevices:
    def test_two_devices_do_not_cross_contaminate(self, tmp_path):
        """两台设备并发写入，各自只看到自己的数据。"""
        du_mod.set_device_user(DEV_A, LAOYU)
        du_mod.set_device_user(DEV_B, JIAREN)

        def speak(device_key, city, food):
            with uc.bind_user(du_mod.bound_device_user(device_key)):
                pref_mod.set_preference("city", city)
                ms_mod.upsert_fact("fav_food", food)

        t1 = threading.Thread(target=speak, args=(DEV_A, "北京", "北京烤鸭"))
        t2 = threading.Thread(target=speak, args=(DEV_B, "上海", "小笼包"))
        t1.start(); t2.start(); t1.join(); t2.join()

        with uc.bind_user(LAOYU):
            assert pref_mod.get_preference("city") == "北京"
            assert (ms_mod.get_fact("fav_food") or {}).get("value") == "北京烤鸭"
        with uc.bind_user(JIAREN):
            assert pref_mod.get_preference("city") == "上海"
            assert (ms_mod.get_fact("fav_food") or {}).get("value") == "小笼包"

    def test_binding_in_thread_does_not_leak_to_caller(self, tmp_path):
        """子线程里的 bind 不能改主线程的 ContextVar。"""
        du_mod.set_device_user(DEV_A, LAOYU)
        uc.set_session_user("main-session", JIAREN2)

        def worker():
            with uc.bind_user(du_mod.bound_device_user(DEV_A)):
                assert uc.resolve_user() == LAOYU

        t = threading.Thread(target=worker)
        t.start(); t.join()
        assert uc.resolve_user() == uc.DEFAULT_USER
        assert uc.resolve_user("main-session") == JIAREN2

    def test_three_devices_two_people(self, tmp_path):
        """老于的实际形态：多台设备、少于设备数的人（同一人的多台设备共享身份）。"""
        DEV_C = "aa:bb:cc:dd:ee:03"
        du_mod.set_device_user(DEV_A, LAOYU)
        du_mod.set_device_user(DEV_B, LAOYU)      # 老于的第二台
        du_mod.set_device_user(DEV_C, JIAREN)

        with uc.bind_user(du_mod.bound_device_user(DEV_A)):
            ms_mod.upsert_fact("fav_food", "北京烤鸭")
        with uc.bind_user(du_mod.bound_device_user(DEV_B)):
            assert (ms_mod.get_fact("fav_food") or {}).get("value") == "北京烤鸭"
        with uc.bind_user(du_mod.bound_device_user(DEV_C)):
            assert ms_mod.get_fact("fav_food") is None


# ======================================================================
# 4. 连接表记录 device_key（管理页可见性）
# ======================================================================

class TestClientDeviceRegistration:
    """`/api/devices` 与 `/api/device-users` 要靠连接表里的 `device_key` 才能
    把"在线设备"和"绑定关系"对上。`client_id` 是 uuid，重连就变，不能当身份。"""

    @pytest.fixture(autouse=True)
    def _clean_clients(self):
        import app.state as st
        with st._xiaozhi_lock:
            st._xiaozhi_clients.clear()
        yield
        with st._xiaozhi_lock:
            st._xiaozhi_clients.clear()

    def test_device_key_recorded_after_hello(self):
        import app.state as st
        st.register_xiaozhi_client("sess-1", object(), None)
        st.set_xiaozhi_client_device("sess-1", DEV_A)
        snap = st.snapshot_xiaozhi_clients()
        assert snap["sess-1"]["device_key"] == DEV_A

    def test_unknown_client_is_noop(self):
        """hello 可能晚于/早于注册（竞态），不能因此抛异常。"""
        import app.state as st
        st.set_xiaozhi_client_device("never-registered", DEV_A)
        assert "never-registered" not in st.snapshot_xiaozhi_clients()

    @pytest.mark.parametrize("client_id,device_key", [
        ("", DEV_A), ("sess-1", ""), ("", ""), (None, DEV_A), ("sess-1", None),
    ])
    def test_empty_args_are_ignored(self, client_id, device_key):
        import app.state as st
        st.register_xiaozhi_client("sess-1", object(), None)
        st.set_xiaozhi_client_device(client_id, device_key)
        snap = st.snapshot_xiaozhi_clients()
        assert "device_key" not in snap["sess-1"]
