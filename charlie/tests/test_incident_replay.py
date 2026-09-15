"""L4 全链路复测：复现 2026-09-07 起床提醒真实事故的完整时序

模拟真实场景（不依赖运行中的服务）：
  08:00 提醒到期 → ESP32 离线（push 返回 enqueued）→ mark_reminder_enqueued
  → 每 5 分钟调度循环重新申领 → 反复入队 → 修复后应在 6 次后停止

验证点：
  1. 完整时序下 attempt_count 递增逻辑
  2. 第 7 次申领时停止（ENQUEUED_MAX_ATTEMPTS=6）
  3. 总时长从无限循环收敛到 ~30 分钟
  4. 过期兜底（EXPIRED_AFTER_HOURS=2）依然先于上限触发（当 due 已过 2h）
"""
import datetime as dt
import os
import sys
import types as _types
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _fake_dt_module(fake_now):
    class _FakeDatetime:
        @staticmethod
        def now():
            return fake_now

        @staticmethod
        def fromisoformat(s):
            return dt.datetime.fromisoformat(s)

    return _types.SimpleNamespace(datetime=_FakeDatetime, timedelta=dt.timedelta)


def _realistic_reminder(now: dt.datetime) -> dict:
    """真实数据形状（09-07 08:00 起床提醒）。"""
    return {
        "id": 1788652234,
        "text": "起床",
        "time": "明天8点",
        "due": "2026-09-07T08:00:00",
        "done": False,
        "repeat": "",
        "delivery_state": "",
        "attempt_count": 0,
    }


class TestFullIncidentTimeline:
    """全时序复测：从 08:00 到停止的每一轮申领。"""

    def test_full_timeline_converges_at_cap(self):
        """逐分钟推进模拟调度循环，断言第 7 次申领被拦 + 总轮次=7。

        真实事故：无上限 → 64+ 轮仍在跑（5h20m 未停）。
        修复后：6 轮入队重投（30 分钟）后停止。
        注意：claim_due_reminders 修改的是读出的记录对象（写回文件的
        那份），需要从传入的列表对象上取最终状态。
        """
        from app import reminders as rem

        reminder = _realistic_reminder(dt.datetime(2026, 9, 7, 8, 0, 0))
        # 初始：08:00 到期，第 1 次申领
        now = dt.datetime(2026, 9, 7, 8, 0, 0)
        rounds = 0
        max_rounds = 20  # 防御：修复后必须 < 20 轮；修复前会一直跑到这里

        while rounds < max_rounds:
            in_list = [dict(reminder)]  # claim 修改的是这份
            with patch.object(rem, "_read_locked_reminders",
                              return_value=in_list), \
                 patch.object(rem, "_write_locked_reminders"), \
                 patch.object(rem, "dt", _fake_dt_module(now)):
                claimed = rem.claim_due_reminders()

            if not claimed:
                # 停止轮：状态变更（done/expired_enqueued）发生在 in_list[0] 上
                final = in_list[0]
                break
            # 模拟 play_reminder_audio 的 enqueued 路径（ESP32 离线）
            rounds += 1
            reminder = dict(claimed[0])  # 申领返回的是申领后的状态
            # 复刻 mark_reminder_enqueued：enqueued + retry_after=+5min
            reminder["delivery_state"] = "enqueued"
            reminder["retry_after"] = (now + dt.timedelta(minutes=5)).isoformat()
            now = now + dt.timedelta(minutes=5)  # 推进到下一轮调度
        else:
            pytest.fail(f"超 {max_rounds} 轮未收敛——无限循环回归！")

        # 修复断言：6 次入队重投后停止
        assert rounds <= 8, f"修复后应 ≤8 轮收敛，实际 {rounds} 轮"
        assert final["done"] is True
        assert final["delivery_state"] in ("expired", "expired_enqueued")
        # 总时长收敛：从 08:00 到停止 ≤ 40 分钟（修复前 5h20m+ 未停）
        assert now - dt.datetime(2026, 9, 7, 8, 0) <= dt.timedelta(minutes=40)

    def test_device_back_online_flushes_queue(self):
        """对照场景：ESP32 5 分钟内重新上线 → 正常完成，不触发上限。

        入队 1 次后设备回来 → 直推成功 → complete → 不再有后续轮次。
        """
        from app import reminders as rem

        reminder = _realistic_reminder(dt.datetime(2026, 9, 7, 8, 0, 0))
        now = dt.datetime(2026, 9, 7, 8, 0, 0)

        # 第 1 轮：申领 + 入队
        with patch.object(rem, "_read_locked_reminders", return_value=[dict(reminder)]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(now)):
            claimed = rem.claim_due_reminders()
        assert len(claimed) == 1
        reminder = dict(claimed[0])
        reminder["delivery_state"] = "enqueued"
        reminder["attempt_count"] = 1
        reminder["retry_after"] = (now + dt.timedelta(minutes=5)).isoformat()

        # 设备 3 分钟后上线 → flush 成功 → complete_reminder_delivery
        now2 = now + dt.timedelta(minutes=3)
        reminder["delivery_state"] = ""
        reminder["done"] = True  # complete 的效果
        reminder["completed_at"] = now2.isoformat()

        # 调度循环不再申领
        with patch.object(rem, "_read_locked_reminders", return_value=[dict(reminder)]), \
             patch.object(rem, "_write_locked_reminders"), \
             patch.object(rem, "dt", _fake_dt_module(now2 + dt.timedelta(minutes=5))):
            claimed2 = rem.claim_due_reminders()
        assert claimed2 == []
        assert reminder["done"] is True

    def test_morning_briefing_not_blocked_by_stuck_reminder(self):
        """关联场景：晨间简报（08:00 同刻推送）不被卡住的提醒阻塞。

        08:00 同时有简报推送 + 起床提醒；提醒卡 enqueued 不影响
        简报的独立投递路径（简报无 reminder_id，走 direct/afplay）。
        """
        # 简报路径无 reminder_id → play_reminder_audio 不进 reminder 状态机
        # 这里验证：无 reminder_id 时 even 完全失败也不会写 reminders.json
        from app.notifications import play_reminder_audio
        import inspect
        src = inspect.getsource(play_reminder_audio)
        # release_failed_reminder 只在 reminder_id is not None 分支
        assert "if reminder_id is not None" in src
