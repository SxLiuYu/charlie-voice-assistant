"""跨通道去重：兜底通道已送达的消息，不该在 ESP32 上线时再播一遍。

事故（2026-09-17 20:26，服务端日志实测）：
    ESP32 离线期间，3 条磁盘告警被 `enqueue_xiaozhi_pending` 排进待推送队列，
    同时 `play_reminder_audio` 走了飞书兜底 —— **用户手机已经收到**：

        20:21:48 [xiaozhi-push] ESP32未连接，入队(1): 磁盘空间不足：已使用94%…
        20:21:48 [notification] ESP32离线，主动推送转飞书兜底: 磁盘空间不足…
        20:21:50 [feishu] 推送成功: 磁盘空间不足：已使用94%…

    设备一上线，`flush_xiaozhi_pending` 又把队列里那几条逐条补发：

        20:27:02 [xiaozhi] 补发推送: 磁盘空间不足：已使用97%…
        20:27:07 [xiaozhi] 补发推送: 磁盘空间不足：已使用100%…
        20:27:13 [xiaozhi] 补发推送: 磁盘空间不足：已使用93%…

    用户侧表现就是"ESP32 开机后把之前所有未提醒的消息全部播报一遍"。

修复分两层，本文件分别覆盖：
  1. **撤销**（`drop_xiaozhi_pending`）：飞书**确认送达**后，把队列里的同一条删掉。
     ⚠️ 撤销必须晚于送达确认 —— 飞书失败时队列要留着，否则消息彻底丢失。
  2. **去重**（`enqueue_xiaozhi_pending`）：同文本 + 同目标的项不重复入队，
     只把已有项的 ts/mp3 刷新到最新，避免同一条消息在队列里堆出多份副本。
"""
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from app import notifications as notif
from app import state as st


# ======================================================================
# 工具
# ======================================================================

def _clear_queue() -> None:
    with st._xiaozhi_lock:
        st._xiaozhi_pending.clear()


def _queue_texts() -> list:
    with st._xiaozhi_lock:
        return [p["text"] for p in st._xiaozhi_pending]


def _queue() -> list:
    with st._xiaozhi_lock:
        return list(st._xiaozhi_pending)


def _seed(text: str, target_uids=None, ts: float | None = None) -> None:
    st.enqueue_xiaozhi_pending(text, b"fake-mp3", target_uids=target_uids)
    if ts is not None:
        with st._xiaozhi_lock:
            st._xiaozhi_pending[-1]["ts"] = ts


@pytest.fixture(autouse=True)
def _clean_queue():
    _clear_queue()
    yield
    _clear_queue()


class _Resp:
    """最小 requests.Response 替身。"""

    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {"code": 0}
        self.text = text or str(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _fake_feishu_token(token="fake-token"):
    """把 feishu_token 模块塞进 sys.modules，让 _send 里的延迟 import 成功。"""
    mod = types.ModuleType("feishu_token")
    mod.get_tenant_access_token = lambda: token
    return patch.dict(sys.modules, {"feishu_token": mod})


# ======================================================================
# 1. drop_xiaozhi_pending —— 撤销队列项
# ======================================================================

class TestDropXiaozhiPending:
    def test_drops_matching_text(self):
        _seed("磁盘空间不足：已使用97%")
        _seed("该吃药了")

        assert st.drop_xiaozhi_pending("磁盘空间不足：已使用97%") == 1
        assert _queue_texts() == ["该吃药了"]

    def test_returns_zero_when_no_match(self):
        _seed("该吃药了")
        assert st.drop_xiaozhi_pending("不存在的消息") == 0
        assert _queue_texts() == ["该吃药了"]

    def test_empty_text_returns_zero_and_touches_nothing(self):
        """空文本不能当成"匹配全部" —— 那会把整个队列清空。"""
        _seed("a")
        _seed("b")
        assert st.drop_xiaozhi_pending("") == 0
        assert _queue_texts() == ["a", "b"]

    def test_empty_text_does_not_match_empty_text_item(self):
        """空文本是"无操作"哨兵，连 text 恰好为空的项也不能匹配。

        没有 `if not text: return 0` 这道守卫时，`drop("")` 会删掉 text 为空的项。
        这里直接造一个来钉住该行为（否则守卫形同虚设、变异测试能溜过去）。
        """
        with st._xiaozhi_lock:
            st._xiaozhi_pending.append(
                {"text": "", "mp3": b"m", "ts": time.time(),
                 "reminder_id": None, "target_uids": ()})
        assert st.drop_xiaozhi_pending("") == 0
        assert len(_queue()) == 1

    def test_none_text_returns_zero(self):
        _seed("a")
        assert st.drop_xiaozhi_pending(None) == 0
        assert _queue_texts() == ["a"]

    def test_drops_all_duplicate_copies(self):
        """同一条消息的多个副本要一起消失，不能只删一个。

        这里**绕过** `enqueue_xiaozhi_pending` 直接造副本：队列里出现多份副本
        是改造前的历史状态（那时入队没有去重），撤销逻辑必须能一次清干净。
        """
        with st._xiaozhi_lock:
            st._xiaozhi_pending.extend([
                {"text": "喝水", "mp3": b"m", "ts": time.time(),
                 "reminder_id": None, "target_uids": ()}
                for _ in range(3)
            ])
        assert st.drop_xiaozhi_pending("喝水") == 3
        assert _queue_texts() == []

    def test_keeps_other_texts_intact(self):
        _seed("A")
        _seed("B")
        _seed("C")
        st.drop_xiaozhi_pending("B")
        assert _queue_texts() == ["A", "C"]

    def test_drop_is_idempotent(self):
        _seed("A")
        assert st.drop_xiaozhi_pending("A") == 1
        assert st.drop_xiaozhi_pending("A") == 0

    def test_drops_targeted_item_too(self):
        """定向项也要能被撤销 —— 撤销按文本匹配，不区分 target。

        同一条消息的副本本来就该一起消失，否则妈妈的提醒会在队列里留一份
        等设备上线再播。
        """
        _seed("妈妈吃药", target_uids=("mama",))
        assert st.drop_xiaozhi_pending("妈妈吃药") == 1
        assert _queue_texts() == []


# ======================================================================
# 2. enqueue_xiaozhi_pending —— 入队去重
# ======================================================================

class TestEnqueueDedup:
    def test_same_text_same_target_not_appended(self):
        st.enqueue_xiaozhi_pending("喝水", b"m1")
        st.enqueue_xiaozhi_pending("喝水", b"m2")
        assert _queue_texts() == ["喝水"]

    def test_returns_queue_length_not_item_count(self):
        st.enqueue_xiaozhi_pending("A", b"m")
        n = st.enqueue_xiaozhi_pending("A", b"m")
        assert n == 1, "重复入队应返回队列长度(1)，而不是累加"

    def test_refreshes_ts_on_duplicate(self):
        """重复入队要刷新时效，否则那条消息会按第一次的 ts 提前过期。"""
        _seed("喝水", ts=time.time() - 100)
        old_ts = _queue()[0]["ts"]
        st.enqueue_xiaozhi_pending("喝水", b"new-mp3")
        item = _queue()[0]
        assert item["ts"] > old_ts
        assert item["mp3"] == b"new-mp3", "音频应被最新一次覆盖"

    def test_different_text_appends(self):
        st.enqueue_xiaozhi_pending("A", b"m")
        st.enqueue_xiaozhi_pending("B", b"m")
        assert _queue_texts() == ["A", "B"]

    def test_same_text_different_target_appends(self):
        """文本相同但目标不同 ⇒ 是两条不同的投递，不能互相吞掉。"""
        st.enqueue_xiaozhi_pending("吃药", b"m", target_uids=("mama",))
        st.enqueue_xiaozhi_pending("吃药", b"m", target_uids=("laoyu",))
        assert len(_queue()) == 2

    def test_same_text_broadcast_vs_targeted_appends(self):
        """广播项与定向项也不能互相去重。"""
        st.enqueue_xiaozhi_pending("提醒", b"m")
        st.enqueue_xiaozhi_pending("提醒", b"m", target_uids=("mama",))
        assert len(_queue()) == 2

    def test_duplicate_updates_reminder_id(self):
        st.enqueue_xiaozhi_pending("吃药", b"m", reminder_id=None)
        st.enqueue_xiaozhi_pending("吃药", b"m", reminder_id=42)
        assert _queue()[0]["reminder_id"] == 42

    def test_duplicate_keeps_existing_reminder_id_when_none_passed(self):
        """后续入队没带 reminder_id 时，不能把已有的抹掉。"""
        st.enqueue_xiaozhi_pending("吃药", b"m", reminder_id=42)
        st.enqueue_xiaozhi_pending("吃药", b"m")
        assert _queue()[0]["reminder_id"] == 42

    def test_expired_items_pruned_before_dedup(self):
        """过期项先被清掉，不会因为"文本相同"而把新的也一起吞掉。"""
        _seed("喝水", ts=time.time() - st._PENDING_TTL - 10)
        st.enqueue_xiaozhi_pending("喝水", b"new")
        q = _queue()
        assert len(q) == 1
        assert q[0]["mp3"] == b"new"


# ======================================================================
# 3. _has_fallback_channel —— 撤销动作的前置守卫
# ======================================================================

class TestHasFallbackChannel:
    def test_true_when_feishu_enabled(self):
        with patch.object(notif, "FEISHU_PUSH_ENABLED", True):
            assert notif._has_fallback_channel() is True

    def test_true_when_ntfy_topic_set(self):
        """飞书没配但有 ntfy 主题 ⇒ 消息仍能送达 ⇒ 算有兜底通道。"""
        fake = types.ModuleType("app.ntfy_push")
        fake.NTFY_TOPIC = "my-topic"
        with patch.object(notif, "FEISHU_PUSH_ENABLED", False), \
             patch.dict(sys.modules, {"app.ntfy_push": fake}):
            assert notif._has_fallback_channel() is True

    def test_false_when_no_channel(self):
        fake = types.ModuleType("app.ntfy_push")
        fake.NTFY_TOPIC = ""
        with patch.object(notif, "FEISHU_PUSH_ENABLED", False), \
             patch.dict(sys.modules, {"app.ntfy_push": fake}):
            assert notif._has_fallback_channel() is False

    def test_false_when_ntfy_import_fails(self):
        with patch.object(notif, "FEISHU_PUSH_ENABLED", False), \
             patch.dict(sys.modules, {"app.ntfy_push": None}):
            assert notif._has_fallback_channel() is False


# ======================================================================
# 4. push_feishu_async —— 确认送达后才撤销
# ======================================================================

class TestFeishuSendOnceDropsPending:
    """⚠️ 这一组是本次修复的核心护栏：撤销**必须**晚于送达确认。

    直接测同步的 `_feishu_send_once`，而不是线程包装 `push_feishu_async` ——
    conftest 会全局 patch 掉后者防止测试真的外呼，而真正要守住的判定逻辑
    （"什么算送达"）在同步函数里。
    """

    def _send(self, resp=None, side_effect=None, token="fake-token",
              text="该吃药了"):
        kw = {"side_effect": side_effect} if side_effect is not None \
            else {"return_value": resp}
        with patch.object(notif, "FEISHU_PUSH_OPEN_ID", "ou_test"), \
             _fake_feishu_token(token=token), \
             patch.object(notif.requests, "post", **kw) as mpost, \
             patch.object(notif, "_drop_xiaozhi_duplicate") as mdrop:
            ok = notif._feishu_send_once(text)
        return ok, mpost, mdrop

    def test_success_code_zero_triggers_drop(self):
        ok, mpost, mdrop = self._send(resp=_Resp(200, {"code": 0}))
        assert ok is True
        mpost.assert_called_once()
        mdrop.assert_called_once_with("该吃药了")

    def test_api_error_code_does_not_drop(self):
        """飞书返回 code!=0（token 失效 / open_id 无效）⇒ **不许**撤队列。

        否则消息既没送达用户、又被从队列删掉，彻底丢失。
        """
        ok, _, mdrop = self._send(
            resp=_Resp(200, {"code": 99991663, "msg": "token invalid"}))
        assert ok is False
        mdrop.assert_not_called()

    def test_http_error_does_not_drop(self):
        ok, _, mdrop = self._send(
            resp=_Resp(500, {}, text="internal error"))
        assert ok is False
        mdrop.assert_not_called()

    def test_network_exception_does_not_drop(self):
        ok, _, mdrop = self._send(side_effect=ConnectionResetError("reset"))
        assert ok is False
        mdrop.assert_not_called()

    def test_missing_token_does_not_post_or_drop(self):
        ok, mpost, mdrop = self._send(resp=_Resp(200, {"code": 0}), token=None)
        assert ok is False
        mpost.assert_not_called()
        mdrop.assert_not_called()

    def test_non_json_body_falls_back_to_status_code(self):
        """body 不是 JSON 时按 HTTP 状态码判定（200 视为成功）。"""
        ok, _, mdrop = self._send(resp=_Resp(200, ValueError("not json")))
        assert ok is True
        mdrop.assert_called_once_with("该吃药了")

    def test_non_json_body_with_http_error_does_not_drop(self):
        ok, _, mdrop = self._send(resp=_Resp(502, ValueError("bad gateway")))
        assert ok is False
        mdrop.assert_not_called()


class TestPushFeishuAsyncThread:
    """开关判断 + 线程包装（测 `_feishu_send_async`，它不受 conftest 的 patch 影响）。"""

    def test_disabled_returns_none_and_does_not_send(self):
        with patch.object(notif, "FEISHU_PUSH_ENABLED", False), \
             patch.object(notif, "_feishu_send_once") as msend:
            assert notif._feishu_send_async("该吃药了") is None
            msend.assert_not_called()

    def test_enabled_returns_joinable_thread(self):
        with patch.object(notif, "FEISHU_PUSH_ENABLED", True), \
             patch.object(notif, "_feishu_send_once") as msend:
            t = notif._feishu_send_async("该吃药了")
            assert t is not None, "应返回线程对象供调用方等待"
            t.join(timeout=3)
            msend.assert_called_once_with("该吃药了")

    def test_public_alias_delegates(self):
        """对外入口 `push_feishu_async` 必须仍指向同一套实现。

        读源码文件而不是 `notif.push_feishu_async` —— 后者被 conftest patch 成
        mock 了，`inspect.getsource` 拿不到真实定义。
        """
        src = open(notif.__file__, encoding="utf-8").read()
        assert "return _feishu_send_async(text)" in src, \
            "push_feishu_async 应转发到 _feishu_send_async"


# ======================================================================
# 5. play_reminder_audio —— _feishu_done=True 分支的显式撤销
# ======================================================================

class TestFeishuDoneBranchExplicitDrop:
    """`add_notification` 已经推过飞书的场景。

    这条路径**没有**飞书成功回调可用（推送发生在入队之前），所以必须在
    `play_reminder_audio` 里显式撤销；但要先用 `_has_fallback_channel()`
    确认确实存在兜底通道，否则撤了就丢消息。
    """

    def _play(self, delivery="enqueued", feishu_done=True, has_channel=True,
              reminder_id=7, mdrop=None):
        from app.notifications import play_reminder_audio
        with patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch.object(notif, "push_tts_to_xiaozhi", return_value=delivery), \
             patch.object(notif, "_reminder_prefix", return_value=""), \
             patch.object(notif, "_has_fallback_channel", return_value=has_channel), \
             patch.object(notif, "_drop_xiaozhi_duplicate") as mdrop_mock, \
             patch("app.reminders.mark_reminder_enqueued"), \
             patch("app.reminders.complete_reminder_delivery"):
            play_reminder_audio("吃药", reminder_id=reminder_id,
                                _feishu_done=feishu_done)
        return mdrop_mock

    def test_feishu_done_with_channel_drops(self):
        m = self._play(feishu_done=True, has_channel=True)
        m.assert_called_once_with("吃药")

    def test_feishu_done_without_channel_keeps(self):
        """没有任何兜底通道 ⇒ 队列是唯一送达路径，绝不能撤。"""
        m = self._play(feishu_done=True, has_channel=False)
        m.assert_not_called()

    def test_not_feishu_done_relies_on_callback(self):
        """`_feishu_done=False` ⇒ 本次会推飞书，撤销交给成功回调，这里不重复撤。"""
        m = self._play(feishu_done=False, has_channel=True)
        m.assert_not_called()

    def test_direct_delivery_does_not_drop(self):
        """直推成功（设备在线）不需要兜底，也就没有撤销动作。"""
        m = self._play(delivery="direct", feishu_done=True, has_channel=True)
        m.assert_not_called()

    def test_active_push_branch_feishu_done_drops(self):
        """无 reminder_id 的主动推送分支同样要显式撤销。"""
        from app.notifications import play_reminder_audio
        with patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch.object(notif, "push_tts_to_xiaozhi", return_value="enqueued"), \
             patch.object(notif, "_has_fallback_channel", return_value=True), \
             patch.object(notif, "_drop_xiaozhi_duplicate") as mdrop:
            play_reminder_audio("磁盘空间不足", reminder_id=None,
                                _feishu_done=True)
        mdrop.assert_called_once_with("磁盘空间不足")


# ======================================================================
# 6. 端到端：离线入队 → 兜底送达 → 设备上线
# ======================================================================

class TestOfflineEnqueueThenOnline:
    def test_feishu_success_empties_queue_so_device_wont_replay(self):
        """复现 20:26 事故并验证修复：兜底送达后队列应为空。"""
        st.enqueue_xiaozhi_pending("磁盘空间不足：已使用97%", b"m")
        assert _queue_texts() == ["磁盘空间不足：已使用97%"]

        with patch.object(notif, "FEISHU_PUSH_OPEN_ID", "ou_test"), \
             _fake_feishu_token(), \
             patch.object(notif.requests, "post",
                          return_value=_Resp(200, {"code": 0})):
            assert notif._feishu_send_once("磁盘空间不足：已使用97%") is True

        assert _queue_texts() == [], \
            "飞书已送达 ⇒ 队列必须为空，否则设备上线会重复播报"

    def test_without_channel_message_survives_until_flush(self):
        """没有兜底通道时，消息必须留在队列里等设备上线（不能丢）。

        守卫在**调用处**（`play_reminder_audio` 里的 `_has_fallback_channel()`
        判断），而不是 `_drop_xiaozhi_duplicate` 自己 —— 所以这里要跑完整路径。
        """
        from app.notifications import play_reminder_audio
        st.enqueue_xiaozhi_pending("该吃药了", b"m")
        with patch("voice_agent.tts_to_mp3", return_value=b"x" * 500), \
             patch.object(notif, "push_tts_to_xiaozhi", return_value="enqueued"), \
             patch.object(notif, "_reminder_prefix", return_value=""), \
             patch.object(notif, "_has_fallback_channel", return_value=False), \
             patch("app.reminders.mark_reminder_enqueued"), \
             patch("app.reminders.complete_reminder_delivery"):
            play_reminder_audio("该吃药了", reminder_id=7, _feishu_done=True)
        assert _queue_texts() == ["该吃药了"]

    def test_three_repeated_alerts_collapse_to_one_queue_entry(self):
        """同一条告警反复触发 ⇒ 队列只留一份，设备上线不会连播 3 遍。"""
        for pct in ("97%", "97%", "97%"):
            st.enqueue_xiaozhi_pending(f"磁盘空间不足：已使用{pct}", b"m")
        assert len(_queue()) == 1

    def test_distinct_alerts_still_all_delivered(self):
        """内容确实不同的告警不该被合并掉。"""
        for pct in ("93%", "97%", "100%"):
            st.enqueue_xiaozhi_pending(f"磁盘空间不足：已使用{pct}", b"m")
        assert len(_queue()) == 3
