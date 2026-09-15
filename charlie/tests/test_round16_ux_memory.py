"""Round 16: 记忆提取+晚安建议时间 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestMemorySkipKeywords:
    def test_skip_uses_exact_match_not_substring(self):
        """记忆提取跳过应使用精确匹配，而非子串匹配。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_memory", os.path.join(PROJECT_DIR, "magic-memory.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = inspect.getsource(mod)
        # 应使用 text.strip() in _SKIP_EXACT 或类似精确匹配
        assert "_SKIP_EXACT" in src or "text.strip() in" in src, \
            "应使用精确匹配（_SKIP_EXACT），而非子串匹配（any(kw in text)）"
        # 不应再使用子串匹配
        assert "any(kw in text for kw in _SKIP_KEYWORDS)" not in src, \
            "不应再使用子串匹配 any(kw in text)"

    def test_greeting_plus_command_not_skipped(self):
        """'你好，帮我记一下明天开会' 不应被跳过。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "magic_memory", os.path.join(PROJECT_DIR, "magic-memory.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 调用 extract_events，验证 "你好，帮我记一下明天开会" 不返回空
        events = mod._extract_events("你好，帮我记一下明天开会", "")
        # 应至少提取到一些事件（"明天开会" 应匹配 EVENT_PATTERNS）
        assert len(events) > 0, \
            "带问候语的指令不应被跳过，应继续提取事件"


class TestBedtimeSuggestionDynamic:
    def test_no_hardcoded_22(self):
        """主动建议晚安时间不应硬编码 22 点。"""
        from app import schedulers
        src = inspect.getsource(schedulers)
        # 找到 "睡" in pkey 分支
        sleep_idx = src.find('"睡" in pkey')
        assert sleep_idx > 0
        after_sleep = src[sleep_idx:sleep_idx+400]
        # 不应硬编码 22 <= hour < 24
        assert "22 <= hour < 24" not in after_sleep, \
            "晚安建议时间不应硬编码 22 点"
        # 应从偏好值动态计算
        assert "sleep_hour" in after_sleep or "pval" in after_sleep, \
            "应从偏好值动态计算提醒时间"
