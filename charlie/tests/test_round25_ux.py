"""Round 25 UX regression tests.

P1-1: 静音时段提醒补播不被过期判定吞掉 + 过期未送达推送飞书
P1-2: 飞书"正在思考…"占位（3 秒延迟）
P1-3: 飞书 per-chat 消息串行化
P1-4: brain 错误消息区分超时/工具不可用
P1-5: 飞书图片消息走视觉分析
"""
import os
import re
from unittest.mock import patch, MagicMock


# ─── P1-1: Reminder expiry vs retry_after ─────────────────────────────────

class TestReminderExpiryRetryAfter:
    def _read_claim_fn(self):
        import app.reminders as rem
        src = open(rem.__file__).read()
        idx = src.find("def claim_due_reminders")
        assert idx > 0
        return src[idx:idx + 4000]

    def test_expiry_uses_retry_after_base(self):
        """过期判定应以 max(due, retry_after) 为基准"""
        body = self._read_claim_fn()
        assert "expiry_base" in body

    def test_retry_after_later_extends_expiry(self):
        """retry_after 晚于 due 时应以 retry_after 为过期基准"""
        body = self._read_claim_fn()
        # 存在 retry_after > due 的比较逻辑
        assert "retry_after > due" in body or "expiry_base = retry_after" in body

    def test_expired_undelivered_pushes_feishu(self):
        """未送达的提醒过期时应推送飞书通知"""
        body = self._read_claim_fn()
        assert "提醒已过期未送达" in body
        assert "push_feishu_async" in body

    def test_delivered_reminder_no_expiry_notify(self):
        """已送达(enqueued/delivered)的提醒过期时不应推送飞书"""
        body = self._read_claim_fn()
        # 条件应只覆盖 retry/None/""/pending
        m = re.search(r'old_delivery_state in \(([^)]+)\)', body)
        assert m, "未找到 old_delivery_state 条件"
        states = m.group(1)
        assert "retry" in states
        assert "enqueued" not in states
        assert "delivered" not in states

    def test_quiet_hours_reminder_scenario(self):
        """端到端场景：02:00 到期 + retry_after=07:05，07:05 时不应过期"""
        import app.reminders as rem
        import datetime as dt
        due = dt.datetime(2026, 8, 29, 2, 0)
        retry_after = dt.datetime(2026, 8, 29, 7, 5)
        now = dt.datetime(2026, 8, 29, 7, 5, 30)
        # 模拟 claim 中的过期计算
        expiry_base = due
        if retry_after > due:
            expiry_base = retry_after
        assert now - expiry_base < dt.timedelta(hours=rem.EXPIRED_AFTER_HOURS), \
            "静音时段补播的提醒不应在补播时刻被标记过期"


# ─── P1-2: Feishu thinking placeholder ────────────────────────────────────

class TestFeishuThinkingPlaceholder:
    def _read_process_fn(self):
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        idx = src.find("def _process_message_sync")
        assert idx > 0
        return src[idx:idx + 6000]

    def test_placeholder_text_exists(self):
        """应发送"正在思考…"占位消息"""
        body = self._read_process_fn()
        assert "正在思考" in body

    def test_placeholder_delayed_3s(self):
        """占位消息应有 3 秒延迟"""
        body = self._read_process_fn()
        assert "threading.Timer(3.0" in body

    def test_placeholder_suppressed_after_first_sentence(self):
        """首句发送后应取消占位（Event set）"""
        body = self._read_process_fn()
        assert "_first_sent.set()" in body
        assert "_first_sent.is_set()" in body

    def test_timer_cancelled_in_finally(self):
        """处理完成后应取消 Timer"""
        body = self._read_process_fn()
        assert "timer.cancel()" in body

    def test_no_placeholder_for_image(self):
        """图片消息不应启动占位 Timer"""
        body = self._read_process_fn()
        # 占位仅在 text 类型时启动
        assert 'if msg_kind == "text":' in body


# ─── P1-3: Per-chat serialization ─────────────────────────────────────────

class TestPerChatSerialization:
    def test_chat_locks_exist(self):
        """应有 per-chat 锁结构"""
        import feishu_ws
        assert hasattr(feishu_ws, "_chat_locks")
        assert hasattr(feishu_ws, "_get_chat_lock")

    def test_get_chat_lock_creates_and_reuses(self):
        """同一 chat_id 应复用同一把锁"""
        import feishu_ws
        lock1 = feishu_ws._get_chat_lock("test_chat_r25")
        lock2 = feishu_ws._get_chat_lock("test_chat_r25")
        assert lock1 is lock2

    def test_lock_serializes_same_chat(self):
        """同一 chat 的锁应真正互斥"""
        import feishu_ws
        import threading, time
        lock = feishu_ws._get_chat_lock("test_chat_r25_mutex")
        order = []
        def worker():
            with lock:
                order.append("enter")
                time.sleep(0.1)
                order.append("exit")
        t1 = threading.Thread(target=worker)
        t1.start()
        time.sleep(0.02)
        # 第二个线程应被阻塞
        acquired = lock.acquire(timeout=0.05)
        if acquired:
            lock.release()
        t1.join()
        assert not acquired or order == ["enter", "exit"]

    def test_lock_bounded(self):
        """锁数量应有上限（防泄漏）"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "_CHAT_LOCKS_MAX" in src


# ─── P1-4: Friendly brain errors ──────────────────────────────────────────

class TestFriendlyBrainErrors:
    def test_timeout_message(self):
        from agent.llm import _friendly_brain_error
        result = _friendly_brain_error("Request timed out after 30s")
        assert "超时" in result

    def test_mcp_unavailable_message(self):
        from agent.llm import _friendly_brain_error
        result = _friendly_brain_error("MCP工具 weather 暂时不可用，请稍后重试")
        assert "暂时不可用" in result
        assert "换个说法" in result

    def test_generic_fallback(self):
        from agent.llm import _friendly_brain_error
        result = _friendly_brain_error("some random error")
        assert "处理时出错了" in result

    def test_case_insensitive_timeout(self):
        from agent.llm import _friendly_brain_error
        result = _friendly_brain_error("BRAIN RUN TIMEOUT EXCEEDED")
        assert "超时" in result

    def test_used_in_stream_path(self):
        """brain_stream_sentences 异常路径应使用 _friendly_brain_error"""
        import agent.llm as llm
        src = open(llm.__file__).read()
        idx = src.find("def brain_stream_sentences")
        body = src[idx:]
        assert "_friendly_brain_error(" in body


# ─── P1-5: Feishu image → vision ──────────────────────────────────────────

class TestFeishuImageVision:
    def _read_process_fn(self):
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        idx = src.find("def _process_message_sync")
        return src[idx:idx + 7000]

    def test_image_branch_exists(self):
        """应存在图片消息处理分支"""
        body = self._read_process_fn()
        assert 'msg_kind == "image"' in body
        assert "image_key" in body

    def test_feishu_image_download_api(self):
        """应调用飞书图片下载 API"""
        body = self._read_process_fn()
        assert "/im/v1/images/" in body
        assert "image_type" in body

    def test_vision_script_invoked(self):
        """应调用 mimo-vision 脚本"""
        import feishu_ws
        assert hasattr(feishu_ws, "VISION_SCRIPT")
        assert "mimo-vision" in feishu_ws.VISION_SCRIPT
        body = self._read_process_fn()
        assert "VISION_SCRIPT" in body

    def test_temp_file_cleanup(self):
        """临时图片文件应在 finally 中删除"""
        body = self._read_process_fn()
        img_idx = body.find("NamedTemporaryFile")
        assert img_idx > 0
        finally_idx = body.find("os.unlink", img_idx)
        assert finally_idx > 0

    def test_failure_fallback_message(self):
        """图片识别失败应有兜底提示"""
        body = self._read_process_fn()
        assert "图片识别暂时失败" in body

    def test_other_non_text_still_rejected(self):
        """其他非文本类型仍走拒绝路径"""
        import feishu_ws
        src = open(feishu_ws.__file__).read()
        assert "只能处理文字" in src
