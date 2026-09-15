"""Round 15: 久坐检测+晚安提醒 UX 修复回归测试。"""
import os
import sys
import inspect
import importlib.util

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _load_magic_decisions():
    """加载 magic-decisions.py（文件名含横线，需用 importlib）。"""
    spec = importlib.util.spec_from_file_location(
        "magic_decisions_round15",
        os.path.join(PROJECT_DIR, "magic-decisions.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSedentaryCheck:
    def test_no_working_state_requirement(self):
        """久坐检测不应强制要求 state=='working'（voice_idle 条件矛盾）。"""
        md = _load_magic_decisions()
        src = inspect.getsource(md._sedentary_check)
        # 不应再检查 state != "working" 就 return None
        assert 'state.get("state") != "working"' not in src, \
            "久坐检测不应强制 state=='working'（与 voice_idle<600 条件矛盾）"

    def test_checks_screen_active(self):
        """久坐检测应基于屏幕活跃时间而非仅语音交互。"""
        md = _load_magic_decisions()
        src = inspect.getsource(md._sedentary_check)
        # 应排除离家/睡眠状态
        assert "away" in src or "home_sleeping" in src, \
            "应排除离家/睡眠状态"
        # 不应再有强制 state == "working" 才继续的条件
        assert 'state.get("state") != "working"' not in src, \
            "应取消 state=='working' 的强制要求（与 voice_idle<600 矛盾）"


class TestEveningWindDownDynamic:
    def test_hours_is_callable(self):
        """evening_wind_down 的 hours 应为 callable，动态读取 sleep_time。"""
        md = _load_magic_decisions()
        rule = next((r for r in md._DECISION_RULES if r["id"] == "evening_wind_down"), None)
        assert rule is not None, "应存在 evening_wind_down 规则"
        hours = rule["condition"]["hours"]
        assert callable(hours), \
            "evening_wind_down hours 应为 callable，动态读取 sleep_time 偏好"

    def test_hours_respects_sleep_preference(self):
        """晚安提醒应在 sleep_time 前2小时到前1小时。"""
        md = _load_magic_decisions()
        from unittest.mock import patch
        rule = next((r for r in md._DECISION_RULES if r["id"] == "evening_wind_down"), None)
        hours_fn = rule["condition"]["hours"]
        # mock sleep_time=01:00 → 提醒应在 23:00-00:00
        with patch("agent.preferences.get_preference", side_effect=lambda k: "01:00" if k == "sleep_time" else "07:00" if k == "wake_time" else None):
            start, end = hours_fn()
        assert start == 23, f"sleep_time=01:00 → 提醒开始应 23，实际 {start}"
        assert end == 0, f"sleep_time=01:00 → 提醒结束应 0，实际 {end}"

    def test_text_not_hardcoded_time(self):
        """文案不应硬编码'晚上9点'。"""
        md = _load_magic_decisions()
        rule = next((r for r in md._DECISION_RULES if r["id"] == "evening_wind_down"), None)
        text = rule["action"]["text"]
        assert "9点" not in text, \
            "文案不应硬编码具体时间，因提醒时间已动态化"
