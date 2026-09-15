"""Round 18: ESP32 离线时决策确认闭环修复回归测试。

修复问题：confirm=True 规则在 ESP32 离线时被完全跳过，
导致飞书用户收不到确认询问，决策反馈闭环断裂。
"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestConfirmRulesEsp32Offline:
    """ESP32 离线时 confirm=True 规则仍应推送飞书 + 创建待确认项。"""

    def test_no_skip_when_esp32_offline(self):
        """不应在 ESP32 离线时直接 continue 跳过确认询问。"""
        sched_path = os.path.join(PROJECT_DIR, "app", "schedulers.py")
        src = open(sched_path).read()
        # 旧逻辑 "ESP32离线，跳过确认询问" + continue 已移除
        # （注意：允许存在其他合法的跳过原因，如"用户睡眠中({rid})，跳过确认询问"）
        assert "ESP32离线，跳过确认询问" not in src, \
            "confirm=True 规则不应在 ESP32 离线时直接跳过（飞书已支持双向确认）"

    def test_esp32_offline_still_notifies_and_confirms(self):
        """ESP32 离线时仍应 add_notification + set_pending_confirmation。"""
        sched_path = os.path.join(PROJECT_DIR, "app", "schedulers.py")
        src = open(sched_path).read()
        # 应有条件播放：在线时播放，离线时仅推送
        assert "_is_esp32_online()" in src, \
            "应检查 ESP32 在线状态决定是否播放音频"
        assert "ESP32离线" in src or "esp32离线" in src.lower(), \
            "ESP32 离线时应有日志说明仅推送飞书"
        # add_notification 和 set_pending_confirmation 应在条件播放之后（不受 ESP32 在线影响）
        confirm_block_start = src.find('if rule.get("confirm"):')
        assert confirm_block_start > 0
        # 从 confirm 块到 set_pending_confirmation 之间不应有 continue（除非冷却/已待确认）
        block = src[confirm_block_start:]
        set_pending_idx = block.find("set_pending_confirmation")
        assert set_pending_idx > 0
        block_to_pending = block[:set_pending_idx]
        # 不应有 "not _is_esp32_online" + continue 的模式
        assert "not _is_esp32_online()" not in block_to_pending or \
               "_play_async" in block_to_pending[block_to_pending.find("_is_esp32_online"):], \
            "ESP32 在线检查应仅影响 _play_async 调用，不阻止通知和确认"

    def test_play_async_only_when_online(self):
        """_play_async 应在 ESP32 在线时才调用。"""
        sched_path = os.path.join(PROJECT_DIR, "app", "schedulers.py")
        src = open(sched_path).read()
        # confirm 块中 _play_async 应在 if _is_esp32_online() 条件内
        confirm_start = src.find('if rule.get("confirm"):')
        block = src[confirm_start:confirm_start + 2200]
        play_idx = block.find("_play_async(msg")
        online_idx = block.find("if _is_esp32_online()")
        assert play_idx > 0 and online_idx > 0, \
            "confirm 块应包含 _play_async 和 _is_esp32_online 检查"
        assert online_idx < play_idx, \
            "_play_async 应在 _is_esp32_online() 条件之后，确保离线时不播放音频"

    def test_confirm_rules_exist(self):
        """确认存在 confirm=True 的决策规则。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_decisions_r18",
            os.path.join(PROJECT_DIR, "magic-decisions.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        confirm_rules = [r for r in mod._DECISION_RULES if r.get("confirm")]
        assert len(confirm_rules) >= 2, \
            "应存在至少 2 条 confirm=True 规则（late_night_sleep, leaving_reminder）"
        ids = {r["id"] for r in confirm_rules}
        assert "late_night_sleep" in ids
        assert "leaving_reminder" in ids
