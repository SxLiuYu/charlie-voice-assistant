"""FR-14 批次 4b：主动推送（提醒）按用户隔离 —— 归属、路由、补发。

背景
----
批次 2 把数据层按 uid 分片、批次 4a 把 ESP32 入口绑定到设备 → 用户映射。
但**主动推送**这条路仍然走进程级身份：

    schedulers._play_async → notifications.play_reminder_audio
        → push_tts_to_xiaozhi → app.state 待推送队列 → ESP32

后果：妈妈设的「8 点吃药」会放到老于房间；老于问「我有哪些提醒」会听到妈妈的。

本批次做的事
------------
1. `reminders.append_reminder` 给每条提醒打 `owner` 标记；
2. 投递层按 `owner` **路由到绑定该用户的设备**，而不是广播；
3. 待推送队列支持 `target_uids` 定向取件（不匹配的**留在队列**，不能静默吃掉）；
4. 用户面读取（`/api/reminders`、`magic-reminder` 的 3 个工具）按用户过滤；
5. 播报称呼 `reminder_prefix` 按用户可配，默认值与改造前完全一致。

本文件锁定的不变量
------------------
- **写默认是广播、读才用 env 兜底**（这条不对称是刻意的，见 `_owner_for_new_reminder`）；
- `owner="default"` 在投递层是**广播**，不是"只发给未绑定设备"；
- 定向推送**不走 MQTT**（MQTT 是广播通道）；
- 定向项在设备不匹配时**留在队列**，等目标设备上线补发；
- 循环提醒生成下一次时**继承 owner**（漏了会从第二天起变成广播）。

⚠️ 注意：`CHARLIE_USER_ID` 在这批测试里**不能**当成"新提醒的 owner"。
这是本批最容易写错的断言 —— 老于的 `.env` 里设了 `CHARLIE_USER_ID=laoyu`，
如果写路径按 env 解析，每条新提醒都会带 `owner=laoyu`；而此时若没有设备
映射到 laoyu，投递层会认为"目标用户没设备"→ 既不推也不广播 → 提醒静默消失。
"""
import datetime as dt
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

LAOYU = "laoyu"
MAMA = "mama"
DEV_LAOYU = "esp32-laoyu"
DEV_MAMA = "esp32-mama"


# ======================================================================
# 公共 fixture / helper
# ======================================================================

@pytest.fixture
def rem(tmp_path, monkeypatch):
    """把 reminders.json 与它的锁文件指到 tmp_path，返回 app.reminders 模块。

    只换路径不动逻辑：`_locked_reminders` 在调用时读模块全局
    `REMINDERS_LOCK_FILE`，所以 patch 模块属性即可生效。
    """
    import app.reminders as mod

    rfile = tmp_path / "reminders.json"
    monkeypatch.setattr(mod, "REMINDERS_FILE", str(rfile), raising=True)
    monkeypatch.setattr(mod, "REMINDERS_LOCK_FILE", str(rfile) + ".lock", raising=True)
    monkeypatch.setattr(mod, "SCHEDULER_LOCK_FILE", str(rfile) + ".scheduler.lock", raising=True)
    # 写路径不看 env，但读路径会兜底到 env —— 显式清掉，避免宿主机 .env 干扰
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
    return mod


@pytest.fixture
def no_binding():
    """确保当前执行流没有显式 uid 绑定（`current_binding()` 返回 None）。"""
    from agent import user_context as uc
    assert uc.current_binding() is None, "前置条件：本用例不应在 bind_user 内运行"
    yield


def _write_raw(mod, items: list) -> None:
    """绕过 append_reminder 直接落盘（模拟"改造前写入的旧数据"）。"""
    mod._write_locked_reminders(items)


def _client_info(ws, loop, device_key: str | None = None) -> dict:
    info = {"ws": ws, "loop": loop}
    if device_key:
        info["device_key"] = device_key
    return info


class _FakeFuture:
    def result(self, timeout=None):
        return None

    def cancel(self):
        pass


# ======================================================================
# 1. 新提醒归属的解析规则（本批最关键的一组）
# ======================================================================

class TestOwnerForNewReminder:
    def test_explicit_owner_wins(self, rem):
        assert rem._owner_for_new_reminder(MAMA) == MAMA

    def test_env_fallback_is_ignored(self, rem, monkeypatch, no_binding):
        """⚠️ 本批最关键的一条。

        env 里有 `CHARLIE_USER_ID=laoyu`，但没有显式绑定 → 新提醒必须还是
        `"default"`（广播）。若这里返回 laoyu，在"laoyu 没有设备映射"的配置下
        提醒会进入投递黑洞（既不推也不广播，用户只看到"已添加提醒"）。
        """
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        assert rem._owner_for_new_reminder() == "default"

    def test_explicit_binding_is_used(self, rem, monkeypatch, no_binding):
        """入口层 bind_user 之后（= 设备已绑定用户）→ 新提醒归该用户。"""
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        from agent.user_context import bind_user
        with bind_user(MAMA):
            assert rem._owner_for_new_reminder() == MAMA

    def test_binding_beats_env(self, rem, monkeypatch, no_binding):
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        from agent.user_context import bind_user
        with bind_user(MAMA):
            assert rem._owner_for_new_reminder() == MAMA
        # 退出绑定后又回到"无绑定 ⇒ 广播"，不受 env 影响
        assert rem._owner_for_new_reminder() == "default"

    def test_read_path_still_uses_env_fallback(self, rem, monkeypatch, no_binding):
        """读路径（list_reminders_for）**保留** env 兜底 —— 与写路径刻意不对称。

        老配置（`CHARLIE_USER_ID=laoyu`、无任何设备映射）下，用户问
        "我有哪些提醒"必须还能看到自己那批 owner=laoyu 的历史提醒。
        """
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        assert rem._resolve_owner(None) == LAOYU
        assert rem._resolve_owner(MAMA) == MAMA

    def test_reminder_owner_defaults_for_legacy_data(self, rem):
        assert rem.reminder_owner({}) == "default"
        assert rem.reminder_owner({"text": "x"}) == "default"
        assert rem.reminder_owner({"owner": MAMA}) == MAMA
        assert rem.reminder_owner({"owner": ""}) == "default"


# ======================================================================
# 2. append_reminder 落库时的 owner
# ======================================================================

class TestAppendReminderOwner:
    def test_no_binding_writes_default(self, rem, monkeypatch, no_binding):
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        item = rem.append_reminder("喝水", "8点", "2030-01-01T08:00:00")
        assert item["owner"] == "default"
        assert rem._load_reminders()[0]["owner"] == "default"

    def test_binding_writes_uid(self, rem, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            item = rem.append_reminder("吃药", "8点", "2030-01-01T08:00:00")
        assert item["owner"] == MAMA

    def test_explicit_owner_wins_over_binding(self, rem, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            item = rem.append_reminder("取快递", "", None, owner=LAOYU)
        assert item["owner"] == LAOYU

    def test_dedup_key_includes_owner(self, rem, no_binding):
        """⚠️ 去重键必须含 owner。

        否则老于和妈妈各设一个「8 点喝水」会被判为重复，后设的直接被丢掉，
        表现是"我的提醒怎么没了"。
        """
        from agent.user_context import bind_user
        with bind_user(LAOYU):
            a = rem.append_reminder("喝水", "8点", "2030-01-01T08:00:00")
        with bind_user(MAMA):
            b = rem.append_reminder("喝水", "8点", "2030-01-01T08:00:00")
        assert a["id"] != b["id"]
        assert len(rem._load_reminders()) == 2

    def test_dedup_still_works_within_same_owner(self, rem, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            a = rem.append_reminder("喝水", "8点", "2030-01-01T08:00:00")
            b = rem.append_reminder("喝水", "8点", "2030-01-01T08:00:00")
        assert a["id"] == b["id"]
        assert len(rem._load_reminders()) == 1


# ======================================================================
# 3. list_reminders_for —— 用户面读取
# ======================================================================

class TestListRemindersFor:
    @pytest.fixture
    def seeded(self, rem):
        _write_raw(rem, [
            {"id": 1, "text": "老于吃药", "due": "2030-01-01T08:00:00", "done": False, "owner": LAOYU},
            {"id": 2, "text": "妈妈买菜", "due": "2030-01-01T09:00:00", "done": False, "owner": MAMA},
            {"id": 3, "text": "老于取快递", "due": "2030-01-01T10:00:00", "done": True, "owner": LAOYU},
            # 改造前的旧数据：没有 owner 字段
            {"id": 4, "text": "旧提醒", "due": "2030-01-01T07:00:00", "done": False},
        ])
        return rem

    def test_filters_by_uid(self, seeded):
        got = seeded.list_reminders_for(LAOYU)
        assert [r["id"] for r in got] == [1]

    def test_other_user_not_visible(self, seeded):
        got = seeded.list_reminders_for(MAMA)
        assert [r["id"] for r in got] == [2]

    def test_legacy_rows_belong_to_default(self, seeded):
        got = seeded.list_reminders_for("default")
        assert [r["id"] for r in got] == [4]

    def test_include_completed(self, seeded):
        assert [r["id"] for r in seeded.list_reminders_for(LAOYU)] == [1]
        assert [r["id"] for r in seeded.list_reminders_for(LAOYU, include_completed=True)] == [1, 3]

    def test_sorted_by_due(self, seeded):
        got = seeded.list_reminders_for(LAOYU, include_completed=True)
        assert [r["due"] for r in got] == sorted(r["due"] for r in got)

    def test_global_view_still_sees_everything(self, seeded):
        """`list_reminders()` 是调度器/晨报用的全局视图，不能被过滤掉。"""
        assert len(seeded.list_reminders()) == 3
        assert len(seeded.list_reminders(include_completed=True)) == 4

    def test_read_uses_binding_when_present(self, seeded, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            assert [r["id"] for r in seeded.list_reminders_for()] == [2]


# ======================================================================
# 4. 循环提醒的归属继承（本批修掉的真 bug）
# ======================================================================

class TestRepeatOccurrenceOwner:
    def test_next_occurrence_keeps_owner(self, rem):
        """⚠️ 回归守卫。

        `_generate_next_occurrence_locked` 跑在调度器后台线程里，**没有任何身份绑定**。
        旧实现新建条目时没带 `owner` → `reminder_owner()` 回落 `"default"` →
        妈妈的每日提醒从**第二天**起变成全屋广播（第一条还是对的，之后全错）。
        """
        now = dt.datetime(2030, 1, 1, 7, 0, 0)
        reminders = [{
            "id": 1, "text": "吃药", "due": "2030-01-01T08:00:00",
            "done": False, "repeat": "daily", "owner": MAMA,
        }]
        with rem._locked_reminders():
            assert rem._generate_next_occurrence_locked(reminders[0], reminders, now) is True
        new = [r for r in reminders if r["id"] != 1]
        assert len(new) == 1
        assert new[0]["owner"] == MAMA, "循环提醒生成下一次时必须继承 owner"

    def test_broadcast_reminder_stays_broadcast(self, rem):
        now = dt.datetime(2030, 1, 1, 7, 0, 0)
        reminders = [{
            "id": 1, "text": "喝水", "due": "2030-01-01T08:00:00",
            "done": False, "repeat": "daily",
        }]
        with rem._locked_reminders():
            rem._generate_next_occurrence_locked(reminders[0], reminders, now)
        new = [r for r in reminders if r["id"] != 1][0]
        assert rem.reminder_owner(new) == "default"

    def test_weekly_occurrence_keeps_owner(self, rem):
        now = dt.datetime(2030, 1, 1, 7, 0, 0)
        reminders = [{
            "id": 1, "text": "交房租", "due": "2030-01-01T08:00:00",
            "done": False, "repeat": "weekly", "owner": LAOYU,
        }]
        with rem._locked_reminders():
            rem._generate_next_occurrence_locked(reminders[0], reminders, now)
        assert [r for r in reminders if r["id"] != 1][0]["owner"] == LAOYU


# ======================================================================
# 5. 投递路由：按设备归属过滤
# ======================================================================

class TestPushRouting:
    @pytest.fixture
    def two_devices(self, monkeypatch):
        """两台在线设备：dev-a 绑 laoyu，dev-b 绑 mama。"""
        import agent.device_users as du

        mapping = {DEV_LAOYU: LAOYU, DEV_MAMA: MAMA}
        monkeypatch.setattr(du, "get_device_user",
                            lambda dk: mapping.get(dk), raising=True)
        monkeypatch.setattr(du, "devices_for_user",
                            lambda uid: {k for k, v in mapping.items() if v == uid},
                            raising=True)
        return mapping

    @patch("asyncio.run_coroutine_threadsafe")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_targeted_push_only_hits_own_device(self, mock_snap, mock_run, rem,
                                                two_devices, no_binding):
        from app.notifications import push_tts_to_xiaozhi

        loop = MagicMock()
        mock_snap.return_value = {
            "sess-a": _client_info(object(), loop, DEV_LAOYU),
            "sess-b": _client_info(object(), loop, DEV_MAMA),
        }
        mock_run.return_value = _FakeFuture()

        status = push_tts_to_xiaozhi("妈妈吃药", b"x" * 500,
                                     target_uids=(MAMA,))
        assert status == "direct"
        assert mock_run.call_count == 1, "只应推给 mama 的设备"

    @patch("app.state.enqueue_xiaozhi_pending")
    @patch("app.mqtt_server.push_tts_to_mqtt")
    @patch("asyncio.run_coroutine_threadsafe")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_no_matching_device_enqueues_with_targets_and_skips_mqtt(
            self, mock_snap, mock_run, mock_mqtt, mock_enqueue, rem, two_devices, no_binding):
        """⚠️ 两台设备在线但都不是目标用户 ⇒ 入队等待，**不走 MQTT**。

        MQTT 是广播通道，无法按设备过滤；走它就会把"妈妈的提醒"放到老于房间。
        """
        from app.notifications import push_tts_to_xiaozhi

        mock_snap.return_value = {
            "sess-a": _client_info(object(), MagicMock(), DEV_LAOYU),
        }
        mock_run.return_value = _FakeFuture()
        mock_enqueue.return_value = 1

        status = push_tts_to_xiaozhi("妈妈吃药", b"x" * 500, target_uids=(MAMA,))
        assert status == "enqueued"
        assert mock_run.call_count == 0
        assert mock_enqueue.call_count == 1
        assert mock_enqueue.call_args.kwargs.get("target_uids") == (MAMA,)
        mock_mqtt.assert_not_called()

    @patch("asyncio.run_coroutine_threadsafe")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_broadcast_still_hits_every_device(self, mock_snap, mock_run, rem,
                                               two_devices, no_binding):
        from app.notifications import push_tts_to_xiaozhi

        loop = MagicMock()
        mock_snap.return_value = {
            "sess-a": _client_info(object(), loop, DEV_LAOYU),
            "sess-b": _client_info(object(), loop, DEV_MAMA),
        }
        mock_run.return_value = _FakeFuture()

        assert push_tts_to_xiaozhi("系统提醒", b"x" * 500) == "direct"
        assert mock_run.call_count == 2

    @patch("app.state.enqueue_xiaozhi_pending")
    @patch("app.mqtt_server.push_tts_to_mqtt")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_unknown_target_uid_degrades_to_broadcast(
            self, mock_snap, mock_mqtt, mock_enqueue, rem, monkeypatch, no_binding):
        """⚠️ 目标 uid **压根没有设备映射** ⇒ 降级为广播，而不是进投递黑洞。

        "设备已映射但离线"要定向排队等上线；"uid 没有任何设备"是配置错误，
        此时定向毫无意义 —— 排队后 `_take_pending_for_flush(uid)` 也取不出来，
        提醒会静默消失。宁可放错房间也不能丢。
        """
        import agent.device_users as du
        monkeypatch.setattr(du, "devices_for_user", lambda uid: set(), raising=True)

        from app.notifications import push_tts_to_xiaozhi

        mock_snap.return_value = {}
        mock_enqueue.return_value = 1
        mock_mqtt.return_value = True

        status = push_tts_to_xiaozhi("妈妈吃药", b"x" * 500, target_uids=(MAMA,))
        assert status == "direct"
        assert mock_enqueue.call_count == 0
        mock_mqtt.assert_called_once()

    def test_client_uid_without_device_key_is_default(self, rem):
        from app.notifications import _client_uid
        assert _client_uid({}) == "default"
        assert _client_uid({"ws": object()}) == "default"

    def test_client_uid_resolves_mapping(self, rem, monkeypatch):
        import agent.device_users as du
        monkeypatch.setattr(du, "get_device_user",
                            lambda dk: MAMA if dk == DEV_MAMA else None, raising=True)
        from app.notifications import _client_uid
        assert _client_uid({"device_key": DEV_MAMA}) == MAMA
        # 设备未绑定 → default（不是"抛错"，也不是"跳过"）
        assert _client_uid({"device_key": "esp32-unknown"}) == "default"

    def test_any_device_bound_false_when_no_mapping(self, rem, monkeypatch):
        import agent.device_users as du
        monkeypatch.setattr(du, "devices_for_user", lambda uid: set(), raising=True)
        from app.notifications import _any_device_bound
        assert _any_device_bound((MAMA,)) is False

    def test_any_device_bound_true_when_mapped_but_offline(self, rem, monkeypatch):
        """有映射（只是离线）→ 保持定向排队，不降级。"""
        import agent.device_users as du
        monkeypatch.setattr(du, "devices_for_user",
                            lambda uid: {DEV_MAMA} if uid == MAMA else set(), raising=True)
        from app.notifications import _any_device_bound
        assert _any_device_bound((MAMA,)) is True

    def test_any_device_bound_conservative_on_error(self, rem, monkeypatch):
        """映射查询炸了 → 返回 True（保守：保持定向，不误发到别人房间）。"""
        import agent.device_users as du

        def _boom(uid):
            raise RuntimeError("map file unreadable")

        monkeypatch.setattr(du, "devices_for_user", _boom, raising=True)
        from app.notifications import _any_device_bound
        assert _any_device_bound((MAMA,)) is True


# ======================================================================
# 6. 待推送队列的定向取件
# ======================================================================

class TestPendingFlushTargeting:
    @pytest.fixture
    def queue(self):
        import app.state as st
        with st._xiaozhi_lock:
            st._xiaozhi_pending.clear()
        yield st
        with st._xiaozhi_lock:
            st._xiaozhi_pending.clear()

    def test_enqueue_stores_target_uids_as_tuple(self, queue):
        queue.enqueue_xiaozhi_pending("a", b"1", target_uids=[MAMA])
        with queue._xiaozhi_lock:
            assert queue._xiaozhi_pending[0]["target_uids"] == (MAMA,)
        queue.enqueue_xiaozhi_pending("b", b"2")
        with queue._xiaozhi_lock:
            assert queue._xiaozhi_pending[1]["target_uids"] == ()

    def test_unknown_uid_only_takes_broadcast(self, queue):
        """连接刚建立、hello 还没来（uid=None）⇒ 只取广播项。"""
        queue.enqueue_xiaozhi_pending("广播", b"1")
        queue.enqueue_xiaozhi_pending("妈妈吃药", b"2", target_uids=(MAMA,))

        taken = queue._take_pending_for_flush(None)
        assert [p["text"] for p in taken] == ["广播"]
        # 定向项必须留在队列，不能被这次连接吃掉
        with queue._xiaozhi_lock:
            assert [p["text"] for p in queue._xiaozhi_pending] == ["妈妈吃药"]

    def test_matching_uid_takes_broadcast_plus_own(self, queue):
        queue.enqueue_xiaozhi_pending("广播", b"1")
        queue.enqueue_xiaozhi_pending("妈妈吃药", b"2", target_uids=(MAMA,))
        queue.enqueue_xiaozhi_pending("老于吃药", b"3", target_uids=(LAOYU,))

        taken = queue._take_pending_for_flush(MAMA)
        assert {p["text"] for p in taken} == {"广播", "妈妈吃药"}
        with queue._xiaozhi_lock:
            assert [p["text"] for p in queue._xiaozhi_pending] == ["老于吃药"]

    def test_wrong_device_does_not_consume_other_users_item(self, queue):
        """⚠️ 本组最关键的一条：定向项不能被"别人的设备"静默吃掉。"""
        queue.enqueue_xiaozhi_pending("妈妈吃药", b"2", target_uids=(MAMA,))
        assert queue._take_pending_for_flush(LAOYU) == []
        with queue._xiaozhi_lock:
            assert len(queue._xiaozhi_pending) == 1
        # 妈妈的设备连上来时还能拿到
        assert len(queue._take_pending_for_flush(MAMA)) == 1

    def test_expired_items_dropped_regardless_of_target(self, queue):
        import time
        queue.enqueue_xiaozhi_pending("妈妈吃药", b"2", target_uids=(MAMA,))
        with queue._xiaozhi_lock:
            queue._xiaozhi_pending[0]["ts"] = time.time() - queue._PENDING_TTL - 1
        assert queue._take_pending_for_flush(MAMA) == []
        with queue._xiaozhi_lock:
            assert queue._xiaozhi_pending == []

    def test_requeue_preserves_target_uids(self, queue):
        queue.enqueue_xiaozhi_pending("妈妈吃药", b"2", target_uids=(MAMA,))
        taken = queue._take_pending_for_flush(MAMA)
        queue._requeue_xiaozhi_pending(taken)
        with queue._xiaozhi_lock:
            assert queue._xiaozhi_pending[0]["target_uids"] == (MAMA,)


# ======================================================================
# 7. 播报称呼按用户可配
# ======================================================================

class TestReminderPrefix:
    def test_default_when_no_owner(self, rem):
        from app.notifications import _reminder_prefix, DEFAULT_REMINDER_PREFIX
        assert _reminder_prefix(None) == DEFAULT_REMINDER_PREFIX

    def test_default_owner_uses_hardcoded_prefix(self, rem):
        """`"default"` 不查偏好 —— 默认行为零变化。"""
        from app.notifications import _reminder_prefix, DEFAULT_REMINDER_PREFIX
        assert _reminder_prefix("default") == DEFAULT_REMINDER_PREFIX

    def test_user_preference_is_used(self, rem, monkeypatch):
        import agent.preferences as pref
        monkeypatch.setattr(pref, "get_preference",
                            lambda k, uid=None, **kw: "妈妈，提醒您：" if uid == MAMA else None,
                            raising=True)
        from app.notifications import _reminder_prefix
        assert _reminder_prefix(MAMA) == "妈妈，提醒您："

    def test_missing_preference_falls_back(self, rem, monkeypatch):
        import agent.preferences as pref
        monkeypatch.setattr(pref, "get_preference", lambda k, uid=None, **kw: None,
                            raising=True)
        from app.notifications import _reminder_prefix, DEFAULT_REMINDER_PREFIX
        assert _reminder_prefix(MAMA) == DEFAULT_REMINDER_PREFIX

    def test_blank_preference_falls_back(self, rem, monkeypatch):
        import agent.preferences as pref
        monkeypatch.setattr(pref, "get_preference", lambda k, uid=None, **kw: "   ",
                            raising=True)
        from app.notifications import _reminder_prefix, DEFAULT_REMINDER_PREFIX
        assert _reminder_prefix(MAMA) == DEFAULT_REMINDER_PREFIX


# ======================================================================
# 8. play_reminder_audio 的 owner → target_uids 映射
# ======================================================================

class TestPlayReminderAudioOwner:
    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    def test_owner_becomes_target_uids(self, mock_tts, mock_push, rem, no_binding):
        from app.notifications import play_reminder_audio
        play_reminder_audio("吃药", reminder_id=1, owner=MAMA)
        assert mock_push.call_args.kwargs["target_uids"] == (MAMA,)

    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    def test_default_owner_is_broadcast(self, mock_tts, mock_push, rem, no_binding):
        """⚠️ `"default"` = 广播，不是"只发给未绑定设备"。

        生产现状是**所有**提醒的 owner 都是 default、且没有任何设备绑定；
        若把 default 当成定向，一旦老于开始绑设备，这些提醒会从"全屋都响"
        变成"只在没绑的房间响"—— 属于没人预期的行为突变。
        """
        from app.notifications import play_reminder_audio
        play_reminder_audio("吃药", reminder_id=1, owner="default")
        assert mock_push.call_args.kwargs["target_uids"] is None

    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    def test_no_owner_is_broadcast(self, mock_tts, mock_push, rem, no_binding):
        from app.notifications import play_reminder_audio
        play_reminder_audio("吃药", reminder_id=1)
        assert mock_push.call_args.kwargs["target_uids"] is None

    @patch("app.notifications.push_tts_to_xiaozhi", return_value="direct")
    @patch("voice_agent.tts_to_mp3", return_value=b"x" * 500)
    def test_tts_runs_under_owner_identity(self, mock_tts, mock_push, rem, no_binding):
        """TTS 合成必须在 owner 身份下跑 —— 称呼/人格等用户相关内容取的是它。"""
        from agent import user_context as uc
        from app.notifications import play_reminder_audio

        seen = []
        mock_tts.side_effect = lambda text: (seen.append(uc.resolve_user()), b"x" * 500)[1]
        play_reminder_audio("吃药", reminder_id=1, owner=MAMA)
        assert seen == [MAMA]


# ======================================================================
# 9. schedulers 只对"真提醒"传 owner，系统级主动建议保持广播
# ======================================================================

class TestSchedulerOwnerPlumbing:
    def test_play_async_forwards_owner(self, rem):
        import app.schedulers as sched
        captured = {}

        def _fake(text, reminder_id=None, _feishu_done=False, owner=None):
            captured.update(text=text, reminder_id=reminder_id, owner=owner)

        orig = sched.play_reminder_audio
        sched.play_reminder_audio = _fake
        try:
            sched._play_async("吃药", reminder_id=7, _feishu_done=True, owner=MAMA)
            import time as _t
            for _ in range(50):
                if captured:
                    break
                _t.sleep(0.02)
        finally:
            sched.play_reminder_audio = orig
        assert captured.get("owner") == MAMA
        assert captured.get("reminder_id") == 7

    def test_reminder_delivery_path_passes_reminder_owner(self):
        """调度器投递真提醒时必须传 `reminder_owner(reminder)`（源码级守卫）。

        系统级主动建议（晨报/健康/决策）**不传** owner ⇒ 广播，保持改造前行为。
        """
        import inspect
        import app.schedulers as sched
        src = inspect.getsource(sched)
        assert "reminder_owner(reminder)" in src, (
            "调度器投递提醒时未传 owner —— 提醒会退化成全屋广播"
        )


# ======================================================================
# 10. magic-reminder 的用户面工具
# ======================================================================

class TestMagicReminderTools:
    @pytest.fixture
    def tools(self, rem):
        import importlib.util
        path = os.path.join(PROJECT_DIR, "magic-reminder.py")
        spec = importlib.util.spec_from_file_location("_t_magic_reminder", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @pytest.fixture
    def seeded(self, rem):
        _write_raw(rem, [
            {"id": 1, "text": "老于吃药", "due": "2030-01-01T08:00:00", "done": False, "owner": LAOYU},
            {"id": 2, "text": "妈妈买菜", "due": "2030-01-01T09:00:00", "done": False, "owner": MAMA},
        ])
        return rem

    def test_list_reminders_is_scoped(self, tools, seeded, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            out = tools.list_reminders()
        assert "妈妈买菜" in out
        assert "老于吃药" not in out, "用户面工具不应列出别人的提醒"

    def test_delete_reminder_cannot_touch_other_users(self, tools, seeded, no_binding):
        """⚠️ 用全局列表会让"帮我删掉吃药提醒"误删妈妈的。"""
        from agent.user_context import bind_user
        with bind_user(LAOYU):
            out = tools.delete_reminder(reminder_id=2)
        assert "没有" in out or "失败" in out
        rows = {r["id"]: r for r in seeded._load_reminders()}
        assert rows[2].get("done") is not True, "不应删掉别人的提醒"

    def test_delete_reminder_works_for_own(self, tools, seeded, no_binding):
        from agent.user_context import bind_user
        with bind_user(LAOYU):
            out = tools.delete_reminder(reminder_id=1)
        assert "已删除" in out
        rows = {r["id"]: r for r in seeded._load_reminders()}
        assert rows[1]["done"] is True

    def test_list_alarms_is_scoped(self, tools, seeded, no_binding):
        from agent.user_context import bind_user
        with bind_user(LAOYU):
            out = tools.list_alarms()
        assert "老于吃药" in out
        assert "妈妈买菜" not in out

    def test_add_reminder_inherits_binding(self, tools, seeded, no_binding):
        from agent.user_context import bind_user
        with bind_user(MAMA):
            # ⚠️ 必须用**未来**时间：add_reminder 对已过去的时间会直接返回
            # "这个时间已经过了"而不落库（避免僵尸提醒），用例会静默变成空断言。
            tools.add_reminder("倒垃圾", "明天8点")
        rows = {r["text"]: r for r in seeded._load_reminders()}
        assert rows["倒垃圾"]["owner"] == MAMA


# ======================================================================
# 11. /api/reminders 的作用域
# ======================================================================

class TestRemindersApiScope:
    @pytest.fixture
    def client(self, rem):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes.reminders import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @pytest.fixture
    def seeded(self, rem):
        _write_raw(rem, [
            {"id": 1, "text": "老于吃药", "due": "2030-01-01T08:00:00", "done": False, "owner": LAOYU},
            {"id": 2, "text": "妈妈买菜", "due": "2030-01-01T09:00:00", "done": False, "owner": MAMA},
        ])
        return rem

    def test_default_is_global_admin_view(self, client, seeded):
        """⚠️ 默认必须是全局 —— 管理台请求上没有用户身份。

        若默认按 env 过滤，多用户上线后管理台会看不到家人通过语音设的提醒
        （静默少数据，比报错难发现得多）。
        """
        body = client.get("/api/reminders").json()
        assert body["total"] == 2
        assert body["scope"] == "all"

    def test_user_filter(self, client, seeded):
        body = client.get("/api/reminders?user=mama").json()
        assert body["total"] == 1
        assert body["reminders"][0]["text"] == "妈妈买菜"
        assert body["scope"] == "mama"

    def test_items_expose_owner(self, client, seeded):
        body = client.get("/api/reminders").json()
        assert {r["owner"] for r in body["reminders"]} == {LAOYU, MAMA}

    def test_post_with_explicit_user(self, client, seeded):
        r = client.post("/api/reminders", json={"text": "遛狗", "user": MAMA})
        assert r.status_code == 200
        assert r.json()["owner"] == MAMA

    def test_post_without_user_is_broadcast(self, client, seeded, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", LAOYU)
        r = client.post("/api/reminders", json={"text": "浇花"})
        assert r.json()["owner"] == "default"


# ======================================================================
# 12. 端到端：从"设备说话"到"只有他的设备响"
# ======================================================================

class TestEndToEndDeviceToDevice:
    """把整条链串起来跑一遍：入口绑定 → 建提醒 → 调度器投递 → 设备路由。

    单元用例各自守住一环，这一条守的是**用户真正感知的保证**：
    "妈妈在自己房间说'提醒我吃药'，8 点只在她房间响，不在老于房间响。"
    """

    @pytest.fixture
    def two_devices(self, monkeypatch):
        import agent.device_users as du
        mapping = {DEV_LAOYU: LAOYU, DEV_MAMA: MAMA}
        monkeypatch.setattr(du, "get_device_user", lambda dk: mapping.get(dk), raising=True)
        monkeypatch.setattr(du, "devices_for_user",
                            lambda uid: {k for k, v in mapping.items() if v == uid},
                            raising=True)
        return mapping

    def test_mama_reminder_rings_only_in_mamas_room(self, rem, two_devices, no_binding):
        from agent.user_context import bind_user
        from app.notifications import play_reminder_audio

        # ① 妈妈在她自己房间（设备 esp32-mama）说"提醒我吃药"
        with bind_user(MAMA):
            item = rem.append_reminder("吃药", "8点", "2030-01-01T08:00:00")
        assert item["owner"] == MAMA

        # ② 调度器到期投递：只有这一处带 reminder_id，走 owner 路由
        pushed = {}

        def _fake_push(text, mp3, reminder_id=None, target_uids=None):
            pushed.update(text=text, target_uids=target_uids)
            return "direct"

        import app.notifications as notif
        orig = notif.push_tts_to_xiaozhi
        notif.push_tts_to_xiaozhi = _fake_push
        try:
            with patch("voice_agent.tts_to_mp3", return_value=b"x" * 500):
                play_reminder_audio("吃药", reminder_id=item["id"],
                                    owner=rem.reminder_owner(item))
        finally:
            notif.push_tts_to_xiaozhi = orig

        assert pushed["target_uids"] == (MAMA,), "必须定向到妈妈，不能广播"

    @patch("asyncio.run_coroutine_threadsafe")
    @patch("app.state.snapshot_xiaozhi_clients")
    def test_routing_layer_sends_to_mamas_device_only(self, mock_snap, mock_run, rem,
                                                      two_devices, no_binding):
        """承接上一条：拿到的 target_uids=(mama,) 在投递层确实只命中妈妈的设备。"""
        from app.notifications import push_tts_to_xiaozhi

        loop = MagicMock()
        mock_snap.return_value = {
            "sess-laoyu": _client_info(object(), loop, DEV_LAOYU),
            "sess-mama": _client_info(object(), loop, DEV_MAMA),
        }
        mock_run.return_value = _FakeFuture()

        assert push_tts_to_xiaozhi("妈妈，提醒您：吃药", b"x" * 500,
                                   target_uids=(MAMA,)) == "direct"
        assert mock_run.call_count == 1
        # 唯一那次调度必须是妈妈的 loop（这里两台共用同一个 mock loop，
        # 所以只断言"只推了一台"；具体是哪台由设备归属过滤保证）
        assert mock_run.call_args[0][1] is loop

    def test_unbound_household_still_broadcasts(self, rem, no_binding):
        """⚠️ 回归守卫：**没有任何设备映射**时，提醒必须照旧全屋广播。

        这是"默认零行为变更"的核心保证 —— 老于还没开始绑设备之前，
        升级到本批次不能让他现有的提醒突然不响了。
        """
        from app.notifications import _any_device_bound, push_tts_to_xiaozhi

        assert _any_device_bound(("default",)) is False
        with patch("asyncio.run_coroutine_threadsafe") as mock_run, \
             patch("app.state.snapshot_xiaozhi_clients") as mock_snap:
            loop = MagicMock()
            mock_snap.return_value = {
                "sess-a": _client_info(object(), loop, None),
                "sess-b": _client_info(object(), loop, None),
            }
            mock_run.return_value = _FakeFuture()
            # owner 为 default ⇒ 投递层收到 target_uids=None ⇒ 广播
            assert push_tts_to_xiaozhi("主人，提醒您：吃药", b"x" * 500,
                                       target_uids=None) == "direct"
            assert mock_run.call_count == 2
