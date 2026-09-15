"""R3-A: system_msg 动静态拆分回归测试。

覆盖 FIX_PLAN_ROUND3.md 第一节测试设计三项：
1. session_facts 单一来源：system_msg 不含工作记忆块；_build_wm_anaphor_prompt() 含该 fact。
2. static 命中且 dynamic 即时刷新：同参数两次调用 static 段一致；改变偏好/摘要后 dynamic 段即时刷新。
3. NB6 兼容：摘要仍按 session_id 注入，不同 session 不串台。
"""
import os
import sys
import copy

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from agent.system_msg import (
    _build_system_msg,
    _build_system_msg_static,
    _build_system_msg_dynamic,
    _SYSTEM_MSG_CACHE,
    _SYSTEM_MSG_LOCK,
    _SYSTEM_MSG_TTL,
    invalidate_system_msg_cache,
)
from agent.llm import _build_wm_anaphor_prompt
from agent import working_memory as wm_mod
from agent import preferences as pref_mod
import agent.history as hist_mod


# ===== fixtures =====

@pytest.fixture(autouse=True)
def _isolate_system_msg_cache():
    """每个用例前后快照/恢复 system_msg TTL 缓存，防止跨用例污染。"""
    with _SYSTEM_MSG_LOCK:
        snapshot = copy.deepcopy(_SYSTEM_MSG_CACHE)
    yield
    with _SYSTEM_MSG_LOCK:
        _SYSTEM_MSG_CACHE.clear()
        _SYSTEM_MSG_CACHE.update(snapshot)


@pytest.fixture()
def _clean_wm_and_prefs():
    """快照/恢复 working_memory 和 preferences 模块级状态。"""
    wm_snapshot = wm_mod.get()
    # 深拷贝 session_facts / intent_stack 等 mutable 字段
    wm_saved = {
        "session_facts": dict(wm_snapshot.get("session_facts", {})),
        "intent_stack": list(wm_snapshot.get("intent_stack", [])),
        "last_topic": wm_snapshot.get("last_topic", ""),
        "last_entity": wm_snapshot.get("last_entity", ""),
        "turn_count": wm_snapshot.get("turn_count", 0),
        "clarification_count": wm_snapshot.get("clarification_count", 0),
    }
    # 深拷贝 preferences 缓存
    _pref_cache = getattr(pref_mod, "_pref_cache", None)
    pref_cache_snapshot = copy.deepcopy(_pref_cache) if isinstance(_pref_cache, dict) else None
    yield
    # 恢复 working_memory
    cur = wm_mod.get()
    for k, v in wm_saved.items():
        cur[k] = v
    # 恢复 preferences 缓存
    if pref_cache_snapshot is not None and isinstance(getattr(pref_mod, "_pref_cache", None), dict):
        pref_mod._pref_cache.clear()
        pref_mod._pref_cache.update(pref_cache_snapshot)


# ===== 测试 1: session_facts 单一来源 =====

class TestSessionFactsSingleSource:
    """system_msg 内不再注入 working_memory session_facts；_build_wm_anaphor_prompt() 是唯一来源。"""

    def test_system_msg_excludes_session_facts_block(self):
        """system_msg 不含 '🧠 工作记忆' 块（无论 session_facts 是否 populated）。"""
        # 凑够 turn_count > 2 以覆盖旧代码的条件分支
        for _ in range(3):
            wm_mod.update_working_memory(facts={"user_city": "北京", "user_name": "小明"})
        try:
            msg = _build_system_msg("none", session_id="s1")
            assert "🧠 工作记忆" not in msg
            assert "user_city" not in msg
            assert "user_name" not in msg
        finally:
            wm_mod.reset()

    def test_wm_anaphor_prompt_is_single_source_for_session_facts(self):
        """_build_wm_anaphor_prompt() 包含 session_facts，证明单一来源未丢。"""
        for _ in range(3):
            wm_mod.update_working_memory(facts={"user_city": "北京", "topic": "天气"})
        try:
            prompt = _build_wm_anaphor_prompt()
            assert prompt is not None
            assert "已确认事实" in prompt
            assert "user_city" in prompt
        finally:
            wm_mod.reset()

    def test_system_msg_no_facts_but_wm_prompt_has_facts_same_turn(self, _clean_wm_and_prefs):
        """同一轮内：system_msg 无 facts 块，wm anaphor prompt 有 facts——两者互补而非重复。"""
        for _ in range(3):
            wm_mod.update_working_memory(facts={"city": "上海"})
        try:
            sys_msg = _build_system_msg("none", session_id="turn-x")
            wm_prompt = _build_wm_anaphor_prompt()
            assert "city" not in sys_msg
            assert wm_prompt is not None
            assert "city" in wm_prompt
        finally:
            wm_mod.reset()


# ===== 测试 2: static 命中 + dynamic 即时刷新 =====

class TestStaticCacheDynamicLive:
    """static 段进 TTL 缓存，dynamic 段每轮实时拼不进缓存。"""

    def test_static_section_is_cached_same_output(self):
        """同参数两次调用，static 段内容完全一致（缓存命中）；dynamic 段因 _context_summaries 为空也一致。"""
        invalidate_system_msg_cache()
        msg1 = _build_system_msg("none", session_id="cache-test")
        msg2 = _build_system_msg("none", session_id="cache-test")
        # 两者应完全相同（无动态数据源 populated）
        assert msg1 == msg2
        # 缓存中应存在 (mcp_set="none", current_role) 键
        from agent.roles import get_current_role
        cache_key = ("none", get_current_role())
        with _SYSTEM_MSG_LOCK:
            assert cache_key in _SYSTEM_MSG_CACHE

    def test_dynamic_section_refreshes_after_summary_change(self):
        """摘要写入后再次调用，dynamic 段含新摘要（不冻结）。"""
        invalidate_system_msg_cache()
        msg_before = _build_system_msg("none", session_id="dyn-session")
        assert "历史对话摘要" not in msg_before

        hist_mod._context_summaries["dyn-session"] = "dyn-session 动态摘要"
        try:
            msg_after = _build_system_msg("none", session_id="dyn-session")
            assert "dyn-session 动态摘要" in msg_after
        finally:
            hist_mod._context_summaries.pop("dyn-session", None)

    def test_static_cache_does_not_include_dynamic_data(self):
        """缓存中的 static 段不含摘要等动态数据；完整 msg 才含动态数据。"""
        from agent.roles import get_current_role
        invalidate_system_msg_cache()
        # 先缓存
        _build_system_msg("none", session_id="cache-check")
        cache_key = ("none", get_current_role())
        with _SYSTEM_MSG_LOCK:
            cached_static = _SYSTEM_MSG_CACHE[cache_key][1]
        # 缓存值不应含摘要
        assert "历史对话摘要" not in cached_static
        assert "用户偏好" not in cached_static

    def test_invalidate_clears_static_cache(self):
        """invalidate_system_msg_cache 清空 static 缓存，之后重新构建。"""
        from agent.roles import get_current_role
        invalidate_system_msg_cache()
        _build_system_msg("none", session_id="inv-session")
        cache_key = ("none", get_current_role())
        with _SYSTEM_MSG_LOCK:
            assert cache_key in _SYSTEM_MSG_CACHE
        invalidate_system_msg_cache()
        with _SYSTEM_MSG_LOCK:
            assert len(_SYSTEM_MSG_CACHE) == 0


# ===== 测试 3: NB6 兼容 —— 摘要按 session_id 注入 =====

class TestNB6SummaryPerSession:
    """不同 session_id 对应不同摘要，不串台；同一 session_id 摘要稳定。"""

    def test_summary_injected_for_specific_session(self):
        hist_mod._context_summaries["s-A"] = "A 摘要内容"
        hist_mod._context_summaries["s-B"] = "B 摘要内容"
        try:
            msg_a = _build_system_msg("none", session_id="s-A")
            msg_b = _build_system_msg("none", session_id="s-B")
            assert "A 摘要内容" in msg_a
            assert "B 摘要内容" in msg_b
            assert "B 摘要内容" not in msg_a
            assert "A 摘要内容" not in msg_b
        finally:
            hist_mod._context_summaries.pop("s-A", None)
            hist_mod._context_summaries.pop("s-B", None)

    def test_static_section_unchanged_across_sessions(self):
        """同一时刻不同 session 的 static 段一致（与 NB6 摘要隔离不冲突）。"""
        from agent.roles import get_current_role
        invalidate_system_msg_cache()
        msg_a = _build_system_msg_static("none")
        invalidate_system_msg_cache()
        msg_b = _build_system_msg_static("none")
        assert msg_a == msg_b

    def test_dynamic_section_varies_by_session(self):
        """dynamic 段严格按 session_id 取值，不同 session 不共享。"""
        hist_mod._context_summaries["only-A"] = "仅 A 可见"
        try:
            dyn_a = _build_system_msg_dynamic(session_id="only-A")
            dyn_default = _build_system_msg_dynamic(session_id="default")
            assert "仅 A 可见" in dyn_a
            assert "仅 A 可见" not in dyn_default
        finally:
            hist_mod._context_summaries.pop("only-A", None)


# ===== 附加：静态段内容完整性（角色/规则/MCP/persona 均保留） =====

class TestStaticSectionCompleteness:
    """拆分后 static 段关键标记全部保留，行为面无退步。"""

    def test_static_contains_role_prompt(self):
        from agent.roles import switch_role
        try:
            switch_role("jarvis")
            invalidate_system_msg_cache()
            static = _build_system_msg_static("none")
            assert "Sir" in static or "J.A.R.V.I.S." in static
        finally:
            switch_role("charlie")

    def test_static_contains_voice_rule_placeholder(self):
        invalidate_system_msg_cache()
        static = _build_system_msg_static("none")
        assert "<<<VOICE_RULE>>>" in static

    def test_static_contains_behavior_rules(self):
        invalidate_system_msg_cache()
        static = _build_system_msg_static("none")
        assert "行为准则" in static
        assert "角色切换" in static

    def test_full_system_msg_unchanged_behavior(self):
        """完整调用 _build_system_msg 后行为与拆分前一致：含角色、规则、渠道替换后可用。"""
        invalidate_system_msg_cache()
        msg = _build_system_msg("none", session_id="behavior-check")
        assert "Charlie" in msg
        assert "行为准则" in msg
        assert "<<<VOICE_RULE>>>" in msg
