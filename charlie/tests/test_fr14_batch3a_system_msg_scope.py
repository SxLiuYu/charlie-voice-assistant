"""FR-14 批次 3A：用户/会话相关内容移出共享的 ``brain.system_message``。

方案依据：``docs/PLAN_FR14_MULTI_USER.md`` §3A

改造前：``_get_brain()`` 每次把 ``_build_system_msg(mcp_set, session_id)``
（静态段 + 动态段）写进**共享**的 ``brain.system_message``，而 Brain 只按
``mcp_set`` 缓存 —— A 会话的动态内容（用户偏好、记忆、会话摘要）会留在共享
对象上被 B 会话读到，即"内容串台"。

改造后：共享对象只保留**静态段**；动态段由每次调用注入 ``messages[0]``。
qwen_agent 会把 ``brain.system_message`` 前置合并进已是 system 的
``messages[0]``（``qwen_agent/agent.py:110-121``），所以最终仍是**单条**
system 消息 —— 双 system 会触发 400（历史 P0：ACCEPTANCE_REPORT bug 14）。

本文件的断言在改造前会失败（已逐条确认），属于真实回归测试而非快照测试。
"""
import copy
import json
import os
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.llm as llm_mod
import agent.history as hist_mod
import agent.preferences as pref_mod
import agent.system_msg as sysmsg_mod
from agent import user_context as uc

STATIC_SENTINEL = "STATIC_SEGMENT_SENTINEL"
LITE_SENTINEL = "LITE_SYSTEM_SENTINEL"

# uid → 偏好（用于断言"含自己的、不含对方的"）
FAKE_PREFS = {
    "laoyu": {"city": "北京", "wake_word": "老于专属唤醒词"},
    "jiaren1": {"city": "上海", "wake_word": "家人一专属唤醒词"},
}
# session_id → 历史摘要（用于断言会话间不串台）
FAKE_SUMMARIES = {
    "s-A": "A会话摘要：讨论过爬香山",
    "s-B": "B会话摘要：讨论过订机票",
}


class _CapturingBrain:
    """最小 brain 桩：记录每次 run 收到的 messages。"""

    def __init__(self, system_message: str = STATIC_SENTINEL):
        self.system_message = system_message
        self.calls: list[list] = []

    def run(self, messages):
        self.calls.append(copy.deepcopy(messages))
        yield [{"role": "assistant", "content": "收到。"}]


def _merge_like_qwen_agent(brain_sys: str, messages: list) -> list:
    """复刻 qwen_agent/agent.py:110-121 的 system 合并语义。

    这是"双 system 必 400"这一约束的关键：qwen_agent 不新增第二条 system，
    而是把 ``self.system_message`` 前置拼到 ``messages[0]`` 的 content 上。
    """
    out = copy.deepcopy(messages)
    if brain_sys:
        if not out or out[0].get("role") != "system":
            out.insert(0, {"role": "system", "content": brain_sys})
        else:
            out[0]["content"] = brain_sys + "\n\n" + out[0]["content"]
    return out


def _system_contents(messages: list) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "system"]


def _all_text(messages: list) -> str:
    return "\n".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))


# ======================================================================
# fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def _clean_user_bindings():
    """user_context 的 session 注册表不在 conftest 快照列表里，自己清干净。"""
    uc.clear_all_session_users()
    yield
    uc.clear_all_session_users()


@pytest.fixture(autouse=True)
def _hermetic_dynamic(monkeypatch):
    """把动态段的 IO 依赖换成内存实现，让断言只依赖 uid / session_id。

    刻意**不** patch ``_build_system_msg_dynamic`` 本身 —— 那正是被测对象。
    """
    monkeypatch.setattr(pref_mod, "list_preferences",
                        lambda uid=None: dict(FAKE_PREFS.get(uid or "default", {})))
    monkeypatch.setattr(sysmsg_mod, "_build_user_profile",
                        lambda uid=None: f"用户画像[{uid}]")
    monkeypatch.setattr(hist_mod, "_context_summaries", dict(FAKE_SUMMARIES))
    # 记忆检索 / 实时上下文：与本轮断言无关，置空避免真实 IO
    import agent.context as ctx_mod
    monkeypatch.setattr(ctx_mod, "format_context_for_prompt",
                        lambda session_id="default", uid=None: "")
    import agent.memory_store as ms_mod
    monkeypatch.setattr(ms_mod, "format_memories_for_prompt", lambda *a, **k: "")
    import app as app_mod
    monkeypatch.setattr(app_mod, "load_magic_module", lambda *a, **k: None)
    try:
        import agent.persona as persona_mod
        monkeypatch.setattr(persona_mod, "get_current_mood", lambda uid=None: "neutral")
        # FR-14 批次 5：persona 段已从 static 段移到 dynamic 段（按 uid 隔离）。
        # 本 fixture 的职责是"把动态段的 IO 依赖换成内存实现"，所以这两个也要置空 ——
        # 否则断言会随真实 `persona_state_{uid}.json` 的 relationship_level 漂移
        # （实测：仓库 `data/persona_state.json` 是 friend ⇒ 凭空多出
        # `🎭 人设风格：回复可以轻松一点…`，"动态段为空"用例直接失败）。
        monkeypatch.setattr(persona_mod, "get_persona_prompt", lambda uid=None: "")
        monkeypatch.setattr(persona_mod, "contextual_response_style",
                            lambda level="", uid=None: "")
    except Exception:
        pass
    sysmsg_mod.invalidate_system_msg_cache()


@pytest.fixture()
def _clean_brains():
    """清空 Brain 缓存，保证 _get_brain 走真实构建路径。"""
    from agent.llm_state import brains
    saved = dict(brains)
    brains.clear()
    yield brains
    brains.clear()
    brains.update(saved)


# ======================================================================
# 1. 共享 Brain 只承载静态段
# ======================================================================

class TestSharedBrainHoldsStaticOnly:
    """``_get_brain`` 写进共享对象的必须是静态段。"""

    def test_brain_system_message_has_no_user_content(self, monkeypatch, _clean_brains):
        """绑定用户后，共享 brain.system_message 不含该用户的偏好/摘要。

        改造前：_build_system_msg(mcp_set, session_id) 会把动态段一起写进去，
        断言 "北京" not in ... 直接失败。
        """
        uc.set_session_user("s-A", "laoyu")
        fake = _CapturingBrain(system_message="")
        monkeypatch.setattr(llm_mod, "_build_brain",
                            lambda mcp_set="all", session_id="default": fake)

        brain = llm_mod._get_brain("none", session_id="s-A")

        assert brain.system_message, "静态段不应为空"
        assert "北京" not in brain.system_message, "共享 brain 不该含用户偏好"
        assert "老于专属唤醒词" not in brain.system_message
        assert "A会话摘要" not in brain.system_message
        assert "用户画像[laoyu]" not in brain.system_message

    def test_two_sessions_share_one_brain_but_no_cross_talk(self, monkeypatch, _clean_brains):
        """两个会话拿到的是**同一个** Brain 对象（3A 不改分键），但 system 内容相同且干净。"""
        uc.set_session_user("s-A", "laoyu")
        uc.set_session_user("s-B", "jiaren1")
        monkeypatch.setattr(llm_mod, "_build_brain",
                            lambda mcp_set="all", session_id="default": _CapturingBrain(system_message=""))

        brain_a = llm_mod._get_brain("none", session_id="s-A")
        msg_a = brain_a.system_message
        brain_b = llm_mod._get_brain("none", session_id="s-B")
        msg_b = brain_b.system_message

        assert brain_a is brain_b, "3A 阶段 Brain 仍按 mcp_set 共享（分键是 3B）"
        assert msg_a == msg_b, "静态段与会话/用户无关，两次刷新结果必须一致"
        for marker in ("北京", "上海", "老于专属唤醒词", "家人一专属唤醒词"):
            assert marker not in msg_b

    def test_brain_build_uses_static_segment(self, monkeypatch, _clean_brains):
        """Brain 构建入参也必须是静态段（缓存 miss 时的第一条路径）。"""
        captured = {}

        class _FakeAssistant:
            def __init__(self, llm=None, name=None, system_message=None, function_list=None):
                captured["system_message"] = system_message
                self.system_message = system_message

        uc.set_session_user("s-A", "laoyu")
        monkeypatch.setattr(llm_mod, "_build_system_msg_static",
                            lambda mcp_set="none": STATIC_SENTINEL)
        import qwen_agent.agents as qa_agents
        monkeypatch.setattr(qa_agents, "Assistant", _FakeAssistant)
        monkeypatch.setenv("MCP_INPROC", "1")

        llm_mod._build_brain_inner("none", session_id="s-A")

        assert captured.get("system_message") == STATIC_SENTINEL


# ======================================================================
# 2. 流式路径：每个会话注入自己的动态段
# ======================================================================

def _patch_stream_deps(monkeypatch, brain: _CapturingBrain):
    monkeypatch.setattr(llm_mod, "_get_brain", lambda *a, **k: brain)
    monkeypatch.setattr(llm_mod, "_classify_intent", lambda text: "magic-info")
    monkeypatch.setattr(llm_mod, "_cache_get", lambda text, session_id="default": None)
    monkeypatch.setattr(llm_mod, "_cache_get_interrupted", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod, "_get_history", lambda session_id="default": [])
    monkeypatch.setattr(llm_mod, "_append_history", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod, "_remember_conversation_async", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod, "_build_wm_anaphor_prompt", lambda: None)
    monkeypatch.setattr(llm_mod, "_recall_memory_prompt", lambda text: None)
    monkeypatch.setattr(llm_mod, "_clean_for_tts", lambda s: s)
    monkeypatch.setattr(llm_mod, "_is_filler_word", lambda s: False)
    monkeypatch.setattr(llm_mod, "_demo_mode_active", lambda: False)
    monkeypatch.setattr(llm_mod, "_get_max_sentences", lambda: 0)
    import voice_agent
    monkeypatch.setattr(voice_agent, "FAST_PATHS", [])


class TestStreamPathInjectsPerSessionDynamic:
    """``brain_stream_sentences`` 的 messages[0] 必须携带本会话/本用户的动态段。"""

    def test_each_session_gets_own_uid_content(self, monkeypatch):
        uc.set_session_user("s-A", "laoyu")
        uc.set_session_user("s-B", "jiaren1")
        brain = _CapturingBrain()
        _patch_stream_deps(monkeypatch, brain)

        list(llm_mod.brain_stream_sentences("今天怎么样", session_id="s-A"))
        list(llm_mod.brain_stream_sentences("今天怎么样", session_id="s-B"))

        assert len(brain.calls) == 2
        sys_a = _system_contents(brain.calls[0])
        sys_b = _system_contents(brain.calls[1])

        assert len(sys_a) == 1, f"应只有 1 条 system，实际 {len(sys_a)} 条"
        assert len(sys_b) == 1, f"应只有 1 条 system，实际 {len(sys_b)} 条"

        assert "北京" in sys_a[0]
        assert "用户画像[laoyu]" in sys_a[0]
        assert "A会话摘要" in sys_a[0]
        # 对方的内容一个字都不能出现
        assert "上海" not in sys_a[0]
        assert "家人一专属唤醒词" not in sys_a[0]
        assert "B会话摘要" not in sys_a[0]

        assert "上海" in sys_b[0]
        assert "用户画像[jiaren1]" in sys_b[0]
        assert "B会话摘要" in sys_b[0]
        assert "北京" not in sys_b[0]
        assert "老于专属唤醒词" not in sys_b[0]
        assert "A会话摘要" not in sys_b[0]

    def test_merged_system_message_is_single_and_static_first(self, monkeypatch):
        """合并后只有 1 条 system，且静态段在前、动态段在后（防 400 + 保持原有顺序）。"""
        uc.set_session_user("s-A", "laoyu")
        brain = _CapturingBrain()
        _patch_stream_deps(monkeypatch, brain)

        list(llm_mod.brain_stream_sentences("今天怎么样", session_id="s-A"))

        merged = _merge_like_qwen_agent(brain.system_message, brain.calls[0])
        sys_msgs = [m for m in merged if m.get("role") == "system"]
        assert len(sys_msgs) == 1, "双 system 会触发 400（ACCEPTANCE_REPORT bug 14）"
        content = sys_msgs[0]["content"]
        assert content.index(STATIC_SENTINEL) < content.index("北京")
        assert STATIC_SENTINEL in content and "北京" in content

    def test_no_dynamic_when_nothing_to_inject(self, monkeypatch):
        """动态段为空时，注入的 system 里**只有静态段**，不凭空造内容。

        注：流式路径按既有设计始终把（通道改写后的）静态段注入 messages[0]
        （因为静态段里的 ``<<<VOICE_RULE>>>`` 占位符必须由通道层渲染）。
        本用例锁的是"动态段为空 ⇒ 不追加任何用户/会话内容"。
        """
        uc.clear_all_session_users()
        monkeypatch.setattr(pref_mod, "list_preferences", lambda uid=None: {})
        monkeypatch.setattr(sysmsg_mod, "_build_user_profile", lambda uid=None: "")
        monkeypatch.setattr(hist_mod, "_context_summaries", {})
        brain = _CapturingBrain()
        _patch_stream_deps(monkeypatch, brain)

        list(llm_mod.brain_stream_sentences("今天怎么样", session_id="s-X"))

        assert _system_contents(brain.calls[0]) == [STATIC_SENTINEL]
        assert "用户偏好" not in _all_text(brain.calls[0])
        assert "用户画像" not in _all_text(brain.calls[0])
        assert "历史对话摘要" not in _all_text(brain.calls[0])


# ======================================================================
# 3. 非流式路径：同样按会话注入
# ======================================================================

class TestNonStreamPathInjectsPerSessionDynamic:
    """``_brain_llm``（飞书文本 / 内部调用）与流式路径行为一致。"""

    def test_non_stream_injects_own_uid_content(self, monkeypatch):
        uc.set_session_user("s-A", "laoyu")
        uc.set_session_user("s-B", "jiaren1")
        brain = _CapturingBrain()
        monkeypatch.setattr(llm_mod, "_get_brain", lambda *a, **k: brain)
        monkeypatch.setattr(llm_mod, "_classify_intent", lambda text: "magic-info")
        monkeypatch.setattr(llm_mod, "_get_history", lambda session_id="default": [])
        monkeypatch.setattr(llm_mod, "_append_history", lambda *a, **k: None)
        monkeypatch.setattr(llm_mod, "_cache_set", lambda *a, **k: None)
        monkeypatch.setattr(llm_mod, "_remember_conversation_async", lambda *a, **k: None)
        monkeypatch.setattr(llm_mod, "_build_wm_anaphor_prompt", lambda: None)
        monkeypatch.setattr(llm_mod, "_recall_memory_prompt", lambda text: None)
        monkeypatch.setattr(llm_mod, "_demo_mode_active", lambda: False)
        monkeypatch.setattr(llm_mod, "_chat_lite_stream", lambda *a, **k: iter([]))
        import agent.context as ctx_mod
        monkeypatch.setattr(ctx_mod, "invalidate_context_cache", lambda: None)

        llm_mod._brain_llm("今天怎么样", session_id="s-A")
        llm_mod._brain_llm("今天怎么样", session_id="s-B")

        assert len(brain.calls) == 2
        sys_a = _system_contents(brain.calls[0])
        sys_b = _system_contents(brain.calls[1])
        assert len(sys_a) == 1 and len(sys_b) == 1
        assert "北京" in sys_a[0] and "上海" not in sys_a[0]
        assert "上海" in sys_b[0] and "北京" not in sys_b[0]


# ======================================================================
# 4. _resolve_uid：会话 → 用户解析优先级
# ======================================================================

class TestResolveUid:
    def test_session_registry_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", "env_user")
        uc.set_session_user("s-A", "laoyu")
        assert llm_mod._resolve_uid("s-A") == "laoyu"
        assert llm_mod._resolve_uid("s-unknown") == "env_user"

    def test_explicit_binding_wins_over_session(self, monkeypatch):
        monkeypatch.setenv("CHARLIE_USER_ID", "env_user")
        uc.set_session_user("s-A", "laoyu")
        with uc.bind_user("jiaren1"):
            assert llm_mod._resolve_uid("s-A") == "jiaren1"
        assert llm_mod._resolve_uid("s-A") == "laoyu"

    def test_fallback_is_default(self, monkeypatch):
        env = {k: v for k, v in os.environ.items() if k != "CHARLIE_USER_ID"}
        monkeypatch.setattr(os, "environ", env)
        assert llm_mod._resolve_uid("s-none") == "default"


# ======================================================================
# 5. lite 路径：裁剪历史时不再连 system 一起丢
# ======================================================================

class _FakeResponse:
    def __init__(self, lines, status=200):
        self.status_code = status
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self, chunk_size=1024, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        pass


def _build_fake_response(chunks):
    lines = []
    for c in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": c}}]})
        lines.append(f"data: {payload}\r\n".encode("utf-8"))
    lines.append(b"data: [DONE]\r\n")
    return _FakeResponse(lines)


class TestLiteTrimKeepsSystem:
    """lite 通道超长裁剪只裁历史，system 必须保留。

    旧实现 ``messages = messages[-4:]`` 在历史稍长时会把 system 消息一起丢掉，
    模型随即丢失人格与规则（自报 provider 身份）。
    """

    def _run(self, monkeypatch, history):
        import agent.llm as llm
        from unittest.mock import MagicMock
        fake_session = MagicMock()
        fake_session.post.return_value = _build_fake_response(["好的。"])
        monkeypatch.setattr(llm, "_session", fake_session)
        monkeypatch.setattr(llm, "active_chat_endpoint",
                            lambda: ("http://test.local", "k", "m"))
        monkeypatch.setattr(llm, "_get_history", lambda sid: list(history))
        monkeypatch.setattr(llm, "_build_lite_system_msg",
                            lambda mcp="none", session_id="default": LITE_SENTINEL)
        monkeypatch.setattr(sysmsg_mod, "_replace_system_msg_for_channel",
                            lambda msg, channel: msg)
        monkeypatch.setattr(llm, "_build_wm_anaphor_prompt", lambda: None)
        monkeypatch.setattr(llm, "_recall_memory_prompt", lambda text: None)
        monkeypatch.setattr(llm, "_clean_for_tts", lambda s: s)
        monkeypatch.setattr(llm, "_is_filler_word", lambda s: False)
        monkeypatch.setattr(llm, "_append_history", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_cache_set", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_remember_conversation_async", lambda *a, **k: None)

        list(llm._chat_lite_stream("hello", session_id="s-long"))
        payload = fake_session.post.call_args.kwargs["json"]
        return payload["messages"]

    def test_system_survives_trim(self, monkeypatch):
        history = []
        for i in range(6):
            history.append({"role": "user" if i % 2 == 0 else "assistant",
                            "content": "长历史" * 300})
        messages = self._run(monkeypatch, history)

        sys_msgs = _system_contents(messages)
        assert sys_msgs == [LITE_SENTINEL], (
            f"裁剪后 system 必须保留，实际 system 内容: {sys_msgs}"
        )
        non_sys = [m for m in messages if m.get("role") != "system"]
        assert len(non_sys) == 3, "应保留最近 1 轮（2 条）+ 本轮 user 消息"

    def test_short_history_untouched(self, monkeypatch):
        history = [{"role": "user", "content": "你好"},
                   {"role": "assistant", "content": "你好呀"}]
        messages = self._run(monkeypatch, history)
        assert _system_contents(messages) == [LITE_SENTINEL]
        assert len([m for m in messages if m.get("role") != "system"]) == 3


# ======================================================================
# 6. lite 路径刻意不注入动态段（固化设计决策，防被"顺手加上"）
# ======================================================================

class TestLiteDoesNotInjectDynamic:
    def test_lite_payload_has_no_user_prefs(self, monkeypatch):
        """lite 是低延迟闲聊快路径，不注入用户偏好/记忆（决策见 llm.py 注释）。"""
        uc.set_session_user("s-lite", "laoyu")
        import agent.llm as llm
        from unittest.mock import MagicMock
        fake_session = MagicMock()
        fake_session.post.return_value = _build_fake_response(["好的。"])
        monkeypatch.setattr(llm, "_session", fake_session)
        monkeypatch.setattr(llm, "active_chat_endpoint", lambda: ("http://test.local", "k", "m"))
        monkeypatch.setattr(llm, "_get_history", lambda sid: [])
        monkeypatch.setattr(llm, "_build_lite_system_msg", lambda mcp="none", session_id="default": LITE_SENTINEL)
        monkeypatch.setattr(llm, "_build_wm_anaphor_prompt", lambda: None)
        monkeypatch.setattr(llm, "_recall_memory_prompt", lambda text: None)
        monkeypatch.setattr(llm, "_clean_for_tts", lambda s: s)
        monkeypatch.setattr(llm, "_is_filler_word", lambda s: False)
        monkeypatch.setattr(llm, "_append_history", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_cache_set", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_remember_conversation_async", lambda *a, **k: None)

        list(llm._chat_lite_stream("hello", session_id="s-lite"))
        messages = fake_session.post.call_args.kwargs["json"]["messages"]

        assert "北京" not in _all_text(messages)
        assert "用户画像[laoyu]" not in _all_text(messages)
