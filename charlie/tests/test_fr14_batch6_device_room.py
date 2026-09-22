"""FR-14 批次 6 回归：device_key 清洗一致性 + 房间名映射 + 管理页 UI。

**背景**：批次 4a 把「设备身份 → 用户」接到 ESP32 入口，但入口自己带了一份
`device_key` 清洗正则（`re.sub(r"[^0-9A-Za-z_-]", "")[:32]`），与映射表的
写入侧（`device_users._sanitize_device_key`，`[^0-9A-Za-z_:-][:64]`）**不一致**：

    aa:bb:cc:dd:ee:ff    入口得 'aabbccddeeff'，映射表 key 是 'aa:bb:cc:dd:ee:ff'
    <长 client_id>        入口截 32 字符，映射表存 64 字符

两侧对不上 ⇒ 查不到 ⇒ 设备**永远回落 default**，而日志只说"设备未绑定用户"，
完全看不出是规则漂移。这是本批修掉的第一个真 bug。

本批还补上方案 §6 遗留的两件事：
  - **房间名映射**：`rooms`（设备 → 房间）+ `room_users`（房间 → 用户兜底）。
    设备标识是固件层面的（重刷固件/换板子/OTA 改名都会变），房间是物理事实。
  - **管理页 UI**：`web/manage.html` 新增「设备与用户」面板。

覆盖：
1. `TestDeviceKeySanitization` —— 清洗规则、幂等性、截断长度、**跨模块单一事实来源**。
2. `TestRoomSanitization` —— 中文/空格/非法字符。
3. `TestRoomMappingPersistence` —— 三张表的读写、缓存失效、env 覆盖。
4. `TestRoomFallbackResolution` —— 两级解析优先级 + `devices_for_user` 互逆性。
5. `TestRoomForSession` —— `xiaozhi-<dev>` → 房间。
6. `TestRoomInSystemMsg` —— 房间进 dynamic 段、不进 static 段、无房间时零变更。
7. `TestDeviceUsersApi` —— HTTP 端到端（含 `clear_room` 与非法输入）。
8. `TestManagePageDeviceTab` —— 前端页面确实挂了面板。
"""
import ast
import json
import os
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.context as ctx_mod
import agent.device_users as du_mod
import agent.history as hist_mod
import agent.memory_store as ms_mod
import agent.persona as persona_mod
import agent.preferences as pref_mod
import agent.system_msg as sysmsg_mod
from agent import user_context as uc

LAOYU = "laoyu"
JIAREN = "jiaren1"
JIAREN2 = "jiaren2"

MAC_A = "aa:bb:cc:dd:ee:01"
MAC_B = "aa:bb:cc:dd:ee:02"
DEV_LONG = "LC-S3-WiFi-1.54TFT-A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6"

WS_FILE = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
MANAGE_HTML = os.path.join(PROJECT_DIR, "web", "manage.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """数据目录指向临时目录 + 清内存缓存（同批次 4a 的做法）。

    ⚠️ 这几个模块的 `DATA_DIR` 都是 **import 期**读 `os.environ` 的模块常量，
    只 patch env 不够，必须直接改属性。
    """
    tmp = str(tmp_path)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(pref_mod, "PREFS_FILE", os.path.join(tmp, "preferences.json"))
    monkeypatch.setattr(persona_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(ms_mod, "DATA_DIR", tmp)
    monkeypatch.setattr(du_mod, "DATA_DIR", tmp)
    # ⚠️ 用 getattr 兜底：在**旧代码**（没有房间映射）上跑时这两个常量不存在，
    # 直接取属性会让 fixture 抛 AttributeError ⇒ 全部用例 ERROR 在 fixture 上，
    # **完全证明不了测试的价值**。兜底之后失败会落在断言上。
    monkeypatch.delenv(getattr(du_mod, "_ENV_VAR", "CHARLIE_DEVICE_USERS"), raising=False)
    monkeypatch.delenv(getattr(du_mod, "_ROOMS_ENV_VAR", "CHARLIE_DEVICE_ROOMS"), raising=False)
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)

    snaps = {}
    for mod, names in ((pref_mod, ["_preferences", "_uid_prefs"]),
                       (persona_mod, ["_states"]),
                       (hist_mod, ["_sessions"])):
        for name in names:
            val = getattr(mod, name, None)
            if isinstance(val, dict):
                snaps[(mod, name)] = dict(val)
                val.clear()
    pref_mod._preferences.clear()
    persona_mod._states.clear()
    du_mod.reset_cache()
    hist_mod._history_append_count = 0
    hist_mod._history_save_seq = 0
    uc.clear_all_session_users()
    sysmsg_mod.invalidate_system_msg_cache()
    ctx_mod.invalidate_context_cache()
    yield
    uc.clear_all_session_users()
    for (mod, name), val in snaps.items():
        cur = getattr(mod, name, None)
        if isinstance(cur, dict):
            cur.clear()
            cur.update(val)
    du_mod.reset_cache()
    sysmsg_mod.invalidate_system_msg_cache()
    ctx_mod.invalidate_context_cache()


def _map_file(tmp_path):
    return tmp_path / "device_users.json"


def _write_raw(tmp_path, payload: dict) -> None:
    _map_file(tmp_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    du_mod.reset_cache()


# ======================================================================
# 1. device_key 清洗（本批修的真 bug）
# ======================================================================

class TestDeviceKeySanitization:
    """⚠️ 本类盯的是「入口与映射表用同一套清洗规则」。

    单测 `_sanitize_device_key` 本身只能证明函数正确；真正会出 bug 的是
    **入口没调它**。所以这里既有行为断言，也有 AST 结构守卫。
    """

    def test_mac_keeps_colons(self):
        assert du_mod._sanitize_device_key(MAC_A) == MAC_A

    def test_is_idempotent(self):
        for raw in (MAC_A, DEV_LONG, "living-room", "  padded  ", "a/b\\c"):
            once = du_mod._sanitize_device_key(raw)
            assert du_mod._sanitize_device_key(once) == once, raw

    def test_truncates_at_64_not_32(self):
        """⚠️ 入口旧实现截 32 字符。长 client_id（ESP32 常见）会被截成两个
        不同的 key ⇒ 绑定静默失效。长度必须与映射表一致。"""
        out = du_mod._sanitize_device_key("A" * 80)
        assert len(out) == 64

    def test_strips_unsafe_chars(self):
        assert du_mod._sanitize_device_key("a/b c") == "abc"
        assert du_mod._sanitize_device_key("../evil") == "evil"

    def test_empty_inputs(self):
        assert du_mod._sanitize_device_key("") == ""
        assert du_mod._sanitize_device_key(None) == ""

    def test_mac_with_colons_round_trips_through_binding(self):
        """回归断言：用带冒号的 MAC 绑定后必须能查回来。

        旧入口会把 `aa:bb:cc:dd:ee:01` 洗成 `aabbccddeeff`，
        而映射表里存的是带冒号的 key ⇒ 永远查不到。
        """
        du_mod.set_device_user(MAC_A, LAOYU)
        assert du_mod.bound_device_user(MAC_A) == LAOYU
        # 旧的错误清洗结果**不应该**命中（否则说明写入侧也在做去冒号清洗）
        assert du_mod.bound_device_user("aabbccddee01") is None

    def test_long_client_id_round_trips(self):
        du_mod.set_device_user(DEV_LONG, JIAREN)
        assert du_mod.bound_device_user(DEV_LONG) == JIAREN
        assert du_mod.bound_device_user(DEV_LONG[:32]) is None

    def test_map_file_key_keeps_colons(self, tmp_path):
        du_mod.set_device_user(MAC_A, LAOYU)
        raw = json.loads(_map_file(tmp_path).read_text(encoding="utf-8"))
        assert MAC_A in raw["devices"]

    # ---- 结构守卫：入口必须复用映射表的清洗函数 ----

    @staticmethod
    def _ws_tree():
        return ast.parse(_read(WS_FILE))

    def test_ws_imports_shared_sanitizer(self):
        """`xiaozhi_ws` 必须从 `agent.device_users` 导入 `_sanitize_device_key`。"""
        names = set()
        for node in ast.walk(self._ws_tree()):
            if isinstance(node, ast.ImportFrom) and node.module == "agent.device_users":
                names |= {a.name for a in node.names}
        assert "_sanitize_device_key" in names, (
            "app/xiaozhi_ws.py 必须复用 agent.device_users._sanitize_device_key；"
            "自带一份正则会让绑定静默失效")

    def test_ws_has_no_private_device_key_regex(self):
        """⚠️ AST 里的字符串常量**不含注释**，所以这条守卫不会被说明性注释误伤
        （`xiaozhi_ws` 的注释里故意引用了旧正则作为反面教材）。

        旧模式 `[^0-9A-Za-z_-]` 一旦重新出现在代码里，就说明又有人复制了一份。
        """
        lits = {n.value for n in ast.walk(self._ws_tree())
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "[^0-9A-Za-z_-]" not in lits, "入口又出现了自带的 device_key 清洗正则"


# ======================================================================
# 2. 房间名校验
# ======================================================================

class TestRoomSanitization:

    @pytest.mark.parametrize("room", ["客厅", "卧室", "书房", "living room", "kids-room", "厨房2"])
    def test_accepts_valid(self, room):
        assert du_mod.sanitize_room(room) == room

    def test_strips_surrounding_space(self):
        assert du_mod.sanitize_room("  客厅  ") == "客厅"

    @pytest.mark.parametrize("room", ["", "   ", None, "../evil", "a/b", "客\x00厅",
                                      "客/厅", "客\t厅", "x" * 25])
    def test_rejects_invalid(self, room):
        assert du_mod.sanitize_room(room) is None

    def test_trailing_whitespace_is_trimmed(self):
        """换行/制表符在**两端**是容忍的（strip 掉），在**中间**才是非法。"""
        assert du_mod.sanitize_room("客厅\n") == "客厅"
        assert du_mod.sanitize_room("\t客厅 ") == "客厅"

    def test_does_not_reuse_device_key_sanitizer(self):
        """⚠️ 房间名允许中文，不能走 `_sanitize_device_key`。

        后者按 `[^0-9A-Za-z_:-]` 清洗 ⇒ 「客厅」会被整串吃掉变成空字符串，
        静默丢数据（这正是当初把两者分开的原因）。
        """
        assert du_mod._sanitize_device_key("客厅") == ""
        assert du_mod.sanitize_room("客厅") == "客厅"


# ======================================================================
# 3. 三张表的持久化
# ======================================================================

class TestRoomMappingPersistence:

    def test_set_and_get_room(self):
        assert du_mod.set_device_room(MAC_A, "客厅") is True
        assert du_mod.get_device_room(MAC_A) == "客厅"

    def test_set_same_room_is_noop(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.set_device_room(MAC_A, "客厅") is False

    def test_update_room(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.set_device_room(MAC_A, "书房") is True
        assert du_mod.get_device_room(MAC_A) == "书房"

    def test_clear_room(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.set_device_room(MAC_A, "") is True
        assert du_mod.get_device_room(MAC_A) is None

    def test_clear_missing_room_is_noop(self):
        assert du_mod.set_device_room(MAC_A, "") is False

    def test_invalid_room_raises(self):
        with pytest.raises(ValueError):
            du_mod.set_device_room(MAC_A, "../evil")

    def test_empty_device_key_raises(self):
        with pytest.raises(ValueError):
            du_mod.set_device_room("   ", "客厅")

    def test_room_user_binding(self):
        assert du_mod.set_room_user("客厅", LAOYU) is True
        assert du_mod.all_room_users() == {"客厅": LAOYU}
        assert du_mod.set_room_user("客厅", None) is True
        assert du_mod.all_room_users() == {}

    def test_room_user_invalid_uid_raises(self):
        with pytest.raises(ValueError):
            du_mod.set_room_user("客厅", "../evil")

    def test_room_user_default_means_unbind(self):
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.set_room_user("客厅", "default") is True
        assert du_mod.all_room_users() == {}

    def test_three_tables_written_together(self, tmp_path):
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("书房", JIAREN)
        raw = json.loads(_map_file(tmp_path).read_text(encoding="utf-8"))
        assert raw["devices"] == {MAC_A: LAOYU}
        assert raw["rooms"] == {MAC_A: "客厅"}
        assert raw["room_users"] == {"书房": JIAREN}

    def test_binding_user_preserves_existing_rooms(self, tmp_path):
        """写 `devices` 不能顺手把 `rooms` / `room_users` 覆盖掉。"""
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        du_mod.set_device_user(MAC_B, JIAREN)
        assert du_mod.get_device_room(MAC_A) == "客厅"
        assert du_mod.all_room_users() == {"客厅": LAOYU}

    def test_setting_room_preserves_bindings(self):
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_room_user("客厅", JIAREN)
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.bound_device_user(MAC_A) == LAOYU
        assert du_mod.all_room_users() == {"客厅": JIAREN}

    def test_old_format_file_still_readable(self, tmp_path):
        """批次 4a 写的文件只有 `devices` 一张表 —— 必须继续可读。"""
        _write_raw(tmp_path, {"version": 1, "devices": {MAC_A: LAOYU}})
        assert du_mod.all_mappings() == {MAC_A: LAOYU}
        assert du_mod.all_rooms() == {}
        assert du_mod.all_room_users() == {}

    def test_bad_entry_dropped_not_whole_table(self, tmp_path):
        """手工编辑 JSON 时错一个 key，不应该让其余绑定全失效。"""
        _write_raw(tmp_path, {
            "version": 1,
            "devices": {MAC_A: LAOYU, MAC_B: "../evil", "  ": JIAREN},
            "rooms": {MAC_A: "客厅", MAC_B: "../bad"},
            "room_users": {"客厅": LAOYU, "bad/room": JIAREN},
        })
        assert du_mod.all_mappings() == {MAC_A: LAOYU}
        assert du_mod.all_rooms() == {MAC_A: "客厅"}
        assert du_mod.all_room_users() == {"客厅": LAOYU}

    def test_corrupt_file_is_empty(self, tmp_path):
        _map_file(tmp_path).write_text("{not json", encoding="utf-8")
        du_mod.reset_cache()
        assert du_mod.all_state() == {"devices": {}, "rooms": {}, "room_users": {}}

    def test_cache_invalidated_on_external_edit(self, tmp_path):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.get_device_room(MAC_A) == "客厅"
        _write_raw(tmp_path, {"version": 1, "rooms": {MAC_B: "书房"}})
        assert du_mod.get_device_room(MAC_A) is None
        assert du_mod.get_device_room(MAC_B) == "书房"

    def test_reset_cache_rereads(self, tmp_path):
        du_mod.set_device_room(MAC_A, "客厅")
        _map_file(tmp_path).write_text(
            json.dumps({"version": 1, "rooms": {MAC_A: "书房"}}), encoding="utf-8")
        du_mod.reset_cache()
        assert du_mod.get_device_room(MAC_A) == "书房"

    # ---- env 覆盖 ----

    def test_rooms_env_flat_form(self, monkeypatch):
        monkeypatch.setenv(du_mod._ROOMS_ENV_VAR, json.dumps({MAC_A: "客厅"}, ensure_ascii=False))
        assert du_mod.get_device_room(MAC_A) == "客厅"

    def test_rooms_env_structured_form(self, monkeypatch):
        monkeypatch.setenv(du_mod._ROOMS_ENV_VAR, json.dumps(
            {"rooms": {MAC_A: "客厅"}, "room_users": {"客厅": LAOYU}}, ensure_ascii=False))
        assert du_mod.get_device_room(MAC_A) == "客厅"
        assert du_mod.all_room_users() == {"客厅": LAOYU}
        assert du_mod.get_device_user(MAC_A) == LAOYU

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        du_mod.set_device_room(MAC_A, "客厅")
        monkeypatch.setenv(du_mod._ROOMS_ENV_VAR, json.dumps({MAC_A: "书房"}, ensure_ascii=False))
        assert du_mod.get_device_room(MAC_A) == "书房"

    def test_env_not_written_back_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(du_mod._ROOMS_ENV_VAR, json.dumps({MAC_A: "客厅"}, ensure_ascii=False))
        du_mod.set_device_user(MAC_B, LAOYU)
        raw = json.loads(_map_file(tmp_path).read_text(encoding="utf-8"))
        assert raw.get("rooms", {}) == {}

    def test_bad_rooms_env_ignored(self, monkeypatch):
        monkeypatch.setenv(du_mod._ROOMS_ENV_VAR, "not json")
        assert du_mod.all_rooms() == {}

    def test_stats_reports_rooms(self):
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        st = du_mod.stats()
        assert st["rooms"] == 1
        assert st["room_users"] == 1
        assert st["rooms_env_var"] == du_mod._ROOMS_ENV_VAR


# ======================================================================
# 4. 两级解析与互逆性
# ======================================================================

class TestRoomFallbackResolution:

    def test_room_fallback_when_device_unbound(self):
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.bound_device_user(MAC_A) == LAOYU

    def test_direct_binding_wins_over_room(self):
        """设备直接绑 `laoyu`、其房间绑 `jiaren1` ⇒ 设备级优先。"""
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", JIAREN)
        assert du_mod.bound_device_user(MAC_A) == LAOYU

    def test_room_without_user_binding_is_unbound(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.bound_device_user(MAC_A) is None

    def test_device_without_room_is_unbound(self):
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.bound_device_user(MAC_A) is None

    def test_resolve_device_user_falls_back_to_default(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.resolve_device_user(MAC_A) == uc.DEFAULT_USER
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.resolve_device_user(MAC_A) == LAOYU

    def test_devices_for_user_includes_room_fallback(self):
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.devices_for_user(LAOYU) == {MAC_A}

    def test_devices_for_user_excludes_shadowed_device(self):
        """⚠️ 冲突情形必须**互斥** —— 设备直接绑 laoyu 后，它不能同时出现在
        jiaren1 的设备集合里，否则提醒会被推给错的人。"""
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", JIAREN)
        du_mod.set_device_room(MAC_B, "客厅")     # 这台走房间兜底
        assert du_mod.devices_for_user(LAOYU) == {MAC_A}
        assert du_mod.devices_for_user(JIAREN) == {MAC_B}

    def test_devices_for_user_is_inverse_of_get_device_user(self):
        """对任意设备，`devices_for_user(get_device_user(d))` 必含 d。"""
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_device_room(MAC_B, "客厅")
        du_mod.set_room_user("客厅", JIAREN)
        for dev in (MAC_A, MAC_B):
            uid = du_mod.get_device_user(dev)
            assert uid is not None
            assert dev in du_mod.devices_for_user(uid), dev

    def test_devices_for_user_default_is_empty(self):
        """`default` 不是合法 uid ⇒ 空集。这正是投递层"广播"语义所依赖的。"""
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.devices_for_user("default") == set()
        assert du_mod.devices_for_user(None) == set()

    def test_multiple_devices_same_room(self):
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_device_room(MAC_B, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.devices_for_user(LAOYU) == {MAC_A, MAC_B}

    def test_room_rebinding_moves_devices(self):
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.get_device_user(MAC_A) == LAOYU
        du_mod.set_room_user("客厅", JIAREN)
        assert du_mod.get_device_user(MAC_A) == JIAREN


# ======================================================================
# 5. 解绑语义
# ======================================================================

class TestRemoveDevice:

    def test_remove_clears_binding_and_room(self, tmp_path):
        """⚠️ "忘掉这台设备"是整体动作：只清 `devices` 而留着 `rooms`，
        它会继续通过房间兜底拿到身份，用户看到的是"删了还在"。"""
        du_mod.set_device_user(MAC_A, LAOYU)
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert du_mod.remove_device_user(MAC_A) is True
        assert du_mod.bound_device_user(MAC_A) is None
        assert du_mod.get_device_room(MAC_A) is None
        # 房间维度的绑定不受影响（同房间其他设备还要用）
        assert du_mod.all_room_users() == {"客厅": LAOYU}

    def test_remove_room_only_still_reports_change(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.remove_device_user(MAC_A) is True
        assert du_mod.get_device_room(MAC_A) is None

    def test_remove_missing_is_noop(self):
        assert du_mod.remove_device_user(MAC_A) is False


# ======================================================================
# 6. session_id → 房间
# ======================================================================

class TestRoomForSession:

    def test_xiaozhi_prefix_resolves(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.room_for_session("xiaozhi-" + MAC_A) == "客厅"

    def test_non_xiaozhi_session_returns_none(self):
        """HTTP /ws、飞书等入口没有设备归属 —— 硬猜会把某个房间写进所有入口。"""
        du_mod.set_device_room(MAC_A, "客厅")
        assert du_mod.room_for_session("default") is None
        assert du_mod.room_for_session("feishu-123") is None
        assert du_mod.room_for_session("") is None
        assert du_mod.room_for_session(None) is None

    def test_device_key_from_session(self):
        assert du_mod.device_key_from_session("xiaozhi-" + MAC_A) == MAC_A
        assert du_mod.device_key_from_session("xiaozhi-") == ""
        assert du_mod.device_key_from_session("plain") == ""

    def test_unknown_device_returns_none(self):
        assert du_mod.room_for_session("xiaozhi-" + MAC_B) is None


# ======================================================================
# 7. system prompt 注入
# ======================================================================

class TestRoomInSystemMsg:

    def _dyn(self, session_id):
        sysmsg_mod.invalidate_system_msg_cache()
        return sysmsg_mod._build_system_msg_dynamic(session_id)

    def _static(self):
        sysmsg_mod.invalidate_system_msg_cache()
        return sysmsg_mod._build_system_msg_static()

    def test_room_appears_in_dynamic(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert "客厅" in self._dyn("xiaozhi-" + MAC_A)

    def test_room_is_per_session_not_global(self):
        """⚠️ 房间放 static 段会被 `(mcp_set, role)` 缓存 + 烤进共享 brain ⇒
        "第一次说话那台设备的房间"会出现在所有入口。必须逐 session 解析。"""
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_device_room(MAC_B, "书房")
        assert "客厅" in self._dyn("xiaozhi-" + MAC_A)
        assert "客厅" not in self._dyn("xiaozhi-" + MAC_B)
        assert "书房" in self._dyn("xiaozhi-" + MAC_B)
        assert "书房" not in self._dyn("xiaozhi-" + MAC_A)

    def test_static_never_contains_room(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert "客厅" not in self._static()

    def test_static_identical_with_and_without_rooms(self):
        """static 段与房间无关 —— 这是"能不能被共享缓存"的判据。"""
        before = self._static()
        du_mod.set_device_room(MAC_A, "客厅")
        du_mod.set_room_user("客厅", LAOYU)
        assert self._static() == before

    def test_no_room_configured_means_no_segment(self):
        out = self._dyn("xiaozhi-" + MAC_A)
        assert "当前设备位置" not in out

    def test_room_segment_format(self):
        du_mod.set_device_room(MAC_A, "客厅")
        assert "当前设备位置：客厅" in self._dyn("xiaozhi-" + MAC_A)

    def test_static_does_not_call_room_helper(self):
        """结构守卫：`_build_system_msg_static` 函数体内不得出现房间相关调用。"""
        tree = ast.parse(_read(os.path.join(PROJECT_DIR, "agent", "system_msg.py")))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_build_system_msg_static"),
                  None)
        assert fn is not None
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        called |= {n.func.attr for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "room_for_session" not in called


# ======================================================================
# 8. HTTP API
# ======================================================================

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import manage
    monkeypatch.setattr(du_mod, "DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(manage.router)
    return TestClient(app)


class TestDeviceUsersApi:

    def test_bind_with_room(self, api_client):
        r = api_client.post("/api/device-users", params={
            "device_key": MAC_A, "user_id": LAOYU, "room": "客厅"})
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["changed"] is True and j["room_changed"] is True
        assert j["user_id"] == LAOYU and j["room"] == "客厅"

    def test_bind_without_room_leaves_room_alone(self, api_client):
        api_client.post("/api/device-users",
                        params={"device_key": MAC_A, "user_id": LAOYU, "room": "客厅"})
        api_client.post("/api/device-users",
                        params={"device_key": MAC_A, "user_id": JIAREN})
        j = api_client.get("/api/device-users").json()
        assert j["rooms"] == {MAC_A: "客厅"}
        assert j["mappings"] == {MAC_A: JIAREN}

    def test_clear_room_flag(self, api_client):
        api_client.post("/api/device-users",
                        params={"device_key": MAC_A, "user_id": LAOYU, "room": "客厅"})
        j = api_client.post("/api/device-users",
                            params={"device_key": MAC_A, "user_id": LAOYU,
                                    "clear_room": True}).json()
        assert j["room"] == "" and j["rooms"] == {}

    def test_invalid_room_rejected(self, api_client):
        j = api_client.post("/api/device-users",
                            params={"device_key": MAC_A, "user_id": LAOYU,
                                    "room": "../evil"}).json()
        assert j["ok"] is False and "房间名不合法" in j["error"]

    def test_empty_device_key_rejected(self, api_client):
        j = api_client.post("/api/device-users", params={"device_key": "  "}).json()
        assert j["ok"] is False

    def test_invalid_uid_rejected(self, api_client):
        j = api_client.post("/api/device-users",
                            params={"device_key": MAC_A, "user_id": "../evil"}).json()
        assert j["ok"] is False and "不合法" in j["error"]

    def test_room_users_endpoint(self, api_client):
        j = api_client.post("/api/room-users", params={"room": "客厅", "user_id": LAOYU}).json()
        assert j["ok"] is True and j["room_users"] == {"客厅": LAOYU}

    def test_room_users_unbind(self, api_client):
        api_client.post("/api/room-users", params={"room": "客厅", "user_id": LAOYU})
        j = api_client.post("/api/room-users", params={"room": "客厅", "user_id": ""}).json()
        assert j["ok"] is True and j["room_users"] == {}

    def test_room_users_empty_room_rejected(self, api_client):
        assert api_client.post("/api/room-users", params={"room": "  "}).json()["ok"] is False

    def test_room_users_invalid_room_rejected(self, api_client):
        j = api_client.post("/api/room-users", params={"room": "../evil", "user_id": LAOYU}).json()
        assert j["ok"] is False

    def test_list_exposes_three_tables(self, api_client):
        api_client.post("/api/device-users",
                        params={"device_key": MAC_A, "user_id": LAOYU, "room": "客厅"})
        api_client.post("/api/room-users", params={"room": "书房", "user_id": JIAREN})
        j = api_client.get("/api/device-users").json()
        assert j["mappings"] == {MAC_A: LAOYU}
        assert j["rooms"] == {MAC_A: "客厅"}
        assert j["room_users"] == {"书房": JIAREN}
        assert "online_devices" in j and "stats" in j

    def test_list_source_badge_for_room_fallback(self, api_client, monkeypatch):
        """在线设备要能标出身份来源（direct / room / none）。"""
        from app import state as app_state
        api_client.post("/api/device-users",
                        params={"device_key": MAC_B, "room": "客厅"})
        api_client.post("/api/room-users", params={"room": "客厅", "user_id": JIAREN})
        monkeypatch.setattr(app_state, "snapshot_xiaozhi_clients",
                            lambda: {"sess1": {"device_key": MAC_B}})
        j = api_client.get("/api/device-users").json()
        assert j["online_devices"][0]["bound_user"] == JIAREN
        assert j["online_devices"][0]["source"] == "room"

    def test_delete_clears_room(self, api_client):
        api_client.post("/api/device-users",
                        params={"device_key": MAC_A, "user_id": LAOYU, "room": "客厅"})
        j = api_client.delete("/api/device-users", params={"device_key": MAC_A}).json()
        assert j["ok"] is True and j["mappings"] == {} and j["rooms"] == {}

    def test_delete_empty_key_rejected(self, api_client):
        assert api_client.delete("/api/device-users", params={"device_key": " "}).json()["ok"] is False


# ======================================================================
# 9. 管理页 UI
# ======================================================================

class TestManagePageDeviceTab:
    """前端面板的**存在性**守卫。

    HTML 不是 Python，AST 用不上；但"面板写了却忘了登记进 TAB_LABELS / loadTab"
    这类漏接是**静默**的（面板能开、标签不亮），没有断言根本发现不了。
    """

    @staticmethod
    def _html() -> str:
        return _read(MANAGE_HTML)

    def test_tab_button_present(self):
        assert "showTab('devices')" in self._html()

    def test_tab_container_present(self):
        assert 'id="tab-devices"' in self._html()

    def test_registered_in_label_map(self):
        """⚠️ `showTab` 靠 `TAB_LABELS` 反查该点亮哪个按钮。漏登记 ⇒ 面板能开
        但标签栏没有高亮（静默的 UI 缺陷，没有测试根本发现不了）。"""
        html = self._html()
        assert "devices:'设备与用户'" in html or 'devices:"设备与用户"' in html

    def test_registered_in_load_tab(self):
        assert "case 'devices': loadDevices(); break;" in self._html()

    def test_loader_and_renderer_defined(self):
        html = self._html()
        assert "async function loadDevices()" in html
        assert "function renderDevices()" in html

    def test_calls_both_endpoints(self):
        html = self._html()
        assert "/api/device-users" in html
        assert "/api/room-users" in html

    def test_uses_event_delegation_not_inline_onclick(self):
        """动态数据必须走 data-* + 事件委托，不能拼进 onclick 字符串。"""
        html = self._html()
        assert 'data-act="bind"' in html
        assert "addEventListener('click'" in html

    def test_escapes_dynamic_values(self):
        assert "escHtml(d.device_key)" in self._html()

    def test_declares_clear_room_action(self):
        assert "clear_room" in self._html()

    def test_script_has_no_tab_container_mismatch(self):
        """`showTab` 用 `[id^="tab-"]` 收集容器 —— 每个 tab 都要有对应容器，
        否则 `getElementById('tab-'+name)` 返回 null ⇒ 切过去白屏。"""
        import re
        html = self._html()
        tabs = set(re.findall(r"showTab\('([a-z]+)'\)", html))
        containers = set(re.findall(r'id="tab-([a-z]+)"', html))
        assert tabs <= containers, f"缺容器: {tabs - containers}"
