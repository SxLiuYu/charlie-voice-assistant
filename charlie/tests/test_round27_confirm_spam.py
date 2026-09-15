"""Round 27 回归测试：confirm 询问整夜轰炸修复。

场景：用户 22:57 设晚安场景确认询问，ESP32 离线仅推飞书，
用户睡觉未回应，系统每小时重复问一次直到早上（9 次）。

修复：
1. confirm 询问每日上限（默认 2 次），按自然日计数，跨重启持久化
2. 用户状态为 home_sleeping 时不再询问确认
"""
import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def dec_mod(tmp_path, monkeypatch):
    """加载 magic-decisions 模块并隔离决策历史文件"""
    import importlib.util
    modname = "magic_decisions_r27"
    spec = importlib.util.spec_from_file_location(modname, "magic-decisions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DECISIONS_FILE", str(tmp_path / "decision_history.json"))
    monkeypatch.setattr(mod, "PENDING_FILE", str(tmp_path / "pending_confirmation.json"))
    yield mod


class TestConfirmAskDailyCap:
    def test_count_zero_when_no_history(self, dec_mod):
        """无历史记录时返回 0"""
        assert dec_mod.get_confirm_ask_count("late_night_sleep") == 0

    def test_record_and_count(self, dec_mod):
        """记录后计数递增且持久化"""
        dec_mod.record_confirm_ask("late_night_sleep")
        assert dec_mod.get_confirm_ask_count("late_night_sleep") == 1
        dec_mod.record_confirm_ask("late_night_sleep")
        assert dec_mod.get_confirm_ask_count("late_night_sleep") == 2
        # 持久化：重新加载文件可读
        with open(dec_mod.DECISIONS_FILE) as f:
            data = json.load(f)
        assert data["_confirm_late_night_sleep"]["count"] == 2

    def test_daily_reset(self, dec_mod):
        """跨天后计数归零"""
        dec_mod.record_confirm_ask("late_night_sleep")
        # 篡改日期为昨天
        with open(dec_mod.DECISIONS_FILE) as f:
            data = json.load(f)
        data["_confirm_late_night_sleep"]["date"] = "2020-01-01"
        with open(dec_mod.DECISIONS_FILE, "w") as f:
            json.dump(data, f)
        assert dec_mod.get_confirm_ask_count("late_night_sleep") == 0

    def test_per_rule_isolation(self, dec_mod):
        """不同规则计数互不影响"""
        dec_mod.record_confirm_ask("rule_a")
        dec_mod.record_confirm_ask("rule_a")
        dec_mod.record_confirm_ask("rule_b")
        assert dec_mod.get_confirm_ask_count("rule_a") == 2
        assert dec_mod.get_confirm_ask_count("rule_b") == 1

    def test_cap_value_is_2(self, dec_mod):
        """每日上限为 2（22:57 首问 + 23:57 补问后停止）"""
        assert dec_mod._CONFIRM_MAX_ASKS_PER_DAY == 2

    def test_night_spam_scenario(self, dec_mod):
        """端到端：模拟昨晚 9 次询问场景，修复后第 3 次起被拦截"""
        cap = dec_mod._CONFIRM_MAX_ASKS_PER_DAY
        asked = 0
        for hour in range(22, 31):  # 22:57, 23:57, 00:57 ... 06:57 共 9 个评估点
            hour_mod = hour % 24
            if hour_mod in (22, 23, 0, 1, 2, 3, 4, 5, 6):
                if dec_mod.get_confirm_ask_count("late_night_sleep") < cap:
                    dec_mod.record_confirm_ask("late_night_sleep")
                    asked += 1
        # 修复前 asked=9；修复后最多 2 次
        assert asked == cap


class TestSleepingStateSkip:
    def test_schedulers_skip_home_sleeping(self):
        """schedulers confirm 块应跳过 home_sleeping 状态"""
        src = open("app/schedulers.py").read()
        idx = src.find('if rule.get("confirm"):')
        assert idx > 0
        block = src[idx:idx + 1200]
        assert 'home_sleeping' in block

    def test_schedulers_daily_cap_check(self):
        """schedulers confirm 块应有每日上限检查"""
        src = open("app/schedulers.py").read()
        idx = src.find('if rule.get("confirm"):')
        block = src[idx:idx + 1800]
        assert "get_confirm_ask_count" in block
        assert "_CONFIRM_MAX_ASKS_PER_DAY" in block

    def test_schedulers_records_ask(self):
        """每次实际询问应记录计数"""
        src = open("app/schedulers.py").read()
        idx = src.find('if rule.get("confirm"):')
        block = src[idx:idx + 1800]
        assert "record_confirm_ask" in block


class TestNoRegression:
    def test_pending_check_still_present(self, dec_mod):
        """pending 确认检查逻辑保留"""
        src = open("app/schedulers.py").read()
        idx = src.find('if rule.get("confirm"):')
        block = src[idx:idx + 1800]
        assert 'pending.get("rule_id") == rid' in block

    def test_cooldown_still_present(self):
        """1 小时冷却逻辑保留"""
        src = open("app/schedulers.py").read()
        idx = src.find('if rule.get("confirm"):')
        block = src[idx:idx + 1200]
        assert "_DECISION_COOLDOWN" in block

    def test_existing_history_entries_preserved(self, dec_mod, tmp_path):
        """record_confirm_ask 不破坏 decision_history 中已有规则条目"""
        with open(dec_mod.DECISIONS_FILE, "w") as f:
            json.dump({"code_edit_123": {"last_trigger": 123.0, "trigger_time": "x"}}, f)
        dec_mod.record_confirm_ask("late_night_sleep")
        with open(dec_mod.DECISIONS_FILE) as f:
            data = json.load(f)
        assert "code_edit_123" in data
        assert "_confirm_late_night_sleep" in data


class TestVADMemoization:
    """日志驱动修复：VAD import 失败后不再每音频帧重复尝试（97k 警告根因）"""

    def test_failed_flag_exists(self):
        import app.xiaozhi_ws as ws
        assert hasattr(ws, "_silero_failed")

    def test_failure_memoized_no_repeat_warning(self):
        """import 失败后 _silero_failed 置位，第二次调用不再尝试 import"""
        import app.xiaozhi_ws as ws
        old_vad, old_failed, old_enabled = ws._silero_vad, ws._silero_failed, ws.VAD_ENABLED
        try:
            ws._silero_vad = None
            ws._silero_failed = False
            calls = {"n": 0}
            real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

            def fake_import(name, *a, **kw):
                if name == "silero_vad":
                    calls["n"] += 1
                    raise ImportError("No module named 'silero_vad'")
                return real_import(name, *a, **kw)

            with patch("builtins.__import__", side_effect=fake_import), \
                 patch.object(ws, "VAD_ENABLED", True):
                r1 = ws._load_silero_vad()
                assert r1 is None
                assert calls["n"] == 1
                assert ws._silero_failed is True
                # 第二次调用：不再尝试 import
                r2 = ws._load_silero_vad()
                assert r2 is None
                assert calls["n"] == 1
        finally:
            ws._silero_vad = old_vad
            ws._silero_failed = old_failed
            ws.VAD_ENABLED = old_enabled

    def test_success_caches_model(self):
        """加载成功后模型单例缓存，且不置失败标志"""
        import app.xiaozhi_ws as ws
        old_vad, old_failed, old_enabled = ws._silero_vad, ws._silero_failed, ws.VAD_ENABLED
        try:
            ws._silero_vad = None
            ws._silero_failed = False
            fake_model = object()
            with patch.dict("sys.modules", {"silero_vad": MagicMock(load_silero_vad=lambda: fake_model)}), \
                 patch.object(ws, "VAD_ENABLED", True):
                r = ws._load_silero_vad()
                assert r is fake_model
                assert ws._silero_failed is False
        finally:
            ws._silero_vad = old_vad
            ws._silero_failed = old_failed
            ws.VAD_ENABLED = old_enabled
