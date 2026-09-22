"""Decider-2B 意图决策头（agent/decide_intent.py）+ _classify_intent 接线。

红线回归钉：热路径安全（永不抛异常）、弃权落回 LLM、服务挂了冷却、
context 自愈重注册、越界选项拒收。
"""
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from agent import decide_intent as di  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    di._reset_for_tests()
    yield
    di._reset_for_tests()


def _resp(status=200, choice="ac-control", conf=0.9):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = {"answers": [{"choice": choice, "confidence": conf,
                                        "probs": {}}], "mode": "state_first_cached"}
    m.text = "ok"
    return m


class TestDecideIntent:
    def test_disabled_returns_none(self):
        with patch.dict(os.environ, {"DECIDE_ENABLED": "0"}):
            assert di.decide_intent("打开空调") is None

    def test_hit_returns_domain(self):
        """conf ≥ 阈值 → 返回域名；且不抛异常。"""
        fake = MagicMock()
        fake.post.return_value = _resp(choice="ac-control", conf=0.89)
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            assert di.decide_intent("打开空调") == "ac-control"

    def test_abstain_returns_none_not_choice(self):
        """低于阈值弃权返回 None（调用方落回 LLM），绝不能返回低置信选项。"""
        fake = MagicMock()
        fake.post.return_value = _resp(choice="ac-control", conf=0.3)
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            assert di.decide_intent("打开空调") is None

    def test_out_of_range_choice_rejected(self):
        """越界选项（模型/服务异常）拒收。"""
        fake = MagicMock()
        fake.post.return_value = _resp(choice="magic-nonexistent", conf=0.99)
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            assert di.decide_intent("打开空调") is None

    def test_never_raises_on_connection_error(self):
        """服务挂了 → None（热路径安全），连续 3 次后进入冷却。"""
        fake = MagicMock()
        fake.post.side_effect = ConnectionError("refused")
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            for _ in range(3):
                assert di.decide_intent("打开空调") is None
            st = di.status()
            assert st["cooldown_left_s"] > 0
            # 冷却中：连请求都不发
            assert di.decide_intent("打开空调") is None
            assert fake.post.call_count == 3

    def test_success_resets_fail_streak(self):
        fake = MagicMock()
        fake.post.side_effect = [ConnectionError("x"), _resp(), _resp()]
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            assert di.decide_intent("打开空调") is None   # fail 1
            assert di.decide_intent("打开空调") == "ac-control"  # ok → 清零
            assert di.status()["fail_streak"] == 0

    def test_unknown_context_self_heals(self):
        """服务重启丢缓存：decide 400 → 重注册 → 重试成功（decide_reuse 自愈语义）。

        注意自愈只对 HTTP 400 触发；网络错误不计入（走失败计数冷却）。
        post 序列：注册(200) → 决策(400) → 重注册(200) → 决策(200 命中)。
        """
        fake = MagicMock()
        bad400 = _resp()
        bad400.status_code = 400
        bad400.text = '{"error": "unknown context_id"}'
        fake.post.side_effect = [_resp(), bad400, _resp(),
                                 _resp(choice="magic-reminder", conf=0.9)]
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", False):
            assert di.decide_intent("设个提醒") == "magic-reminder"
        assert fake.post.call_count == 4
        # 第1、3次是注册调用
        for idx in (0, 2):
            assert "/v1/contexts" in fake.post.call_args_list[idx].args[0]
        for idx in (1, 3):
            assert "/v1/decide" in fake.post.call_args_list[idx].args[0]

    def test_register_context_sends_nonempty_body(self):
        """红线：context 必须非空（服务端 400）。"""
        fake = MagicMock()
        fake.post.return_value = _resp()
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake):
            assert di._register_context() is True
        body = fake.post.call_args.kwargs["json"]
        assert body["context_id"]
        assert len(body["context"].strip()) > 50

    def test_decide_uses_context_id_path(self):
        """必须走 state_first 缓存路径（context_id），不是全量 prefill。"""
        fake = MagicMock()
        fake.post.return_value = _resp()
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "_get_session", return_value=fake), \
             patch.object(di, "_registered", True):
            di.decide_intent("打开空调")
        body = fake.post.call_args.kwargs["json"]
        assert body["context_id"] == di._DECIDE_CTX_ID
        assert len(body["questions"][0]["options"]) == len(di.DOMAINS)

    def test_domain_list_matches_llm_prompt_set(self):
        """decider 的域集合必须是 _classify_intent prompt 域集合的子集（防两处漂移）。

        2026-09-20 起 decider 停用（DECIDE_ENABLED=0，故障形态比收益贵），
        LLM 分类 prompt 成为路由唯一真源，可以先于 decider 加新域（如
        magic-roles）。方向性不变量：**decider 选得出的每个域都必须在 prompt
        里存在**（否则命中后无对应 brain），反过来不要求——prompt 可以领先。
        将来恢复 decider 时：同步 DOMAINS + _DECIDE_CTX 域说明 + 换
        _DECIDE_CTX_ID（同 id = 同文本，服务端缓存），届时可改回相等断言。
        """
        import re
        import agent.llm as llm
        src = open(llm.__file__, encoding="utf-8").read()
        m = re.search(r'"选项: (.*?)\\n"', src)
        assert m, "prompt 选项行没找到"
        prompt_domains = set(m.group(1).split(" | "))
        missing = set(di.DOMAINS) - prompt_domains
        assert not missing, (
            f"decider 有这些域而 prompt 没有（命中即断链）: {missing}")


class TestClassifyIntentWiring:
    def test_decider_hit_skips_llm(self):
        """decider 命中 → 直接返回，LLM 分类请求一次都不发。"""
        import agent.llm as llm
        llm._st.intent_cache.clear()
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "decide_intent", return_value="magic-reminder"), \
             patch.object(llm._session, "post", side_effect=AssertionError("LLM 不该被调")):
            assert llm._classify_intent("设个明天八点的提醒") == "magic-reminder"

    def test_decider_abstain_falls_back_to_llm(self):
        """decider 弃权 → 落回 LLM 分类（mock 返回 none）。"""
        import agent.llm as llm
        llm._st.intent_cache.clear()
        llm_obj = MagicMock()
        llm_obj.json.return_value = {"choices": [{"message": {"content": "none"},
                                                  "finish_reason": "stop"}]}
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "decide_intent", return_value=None), \
             patch.object(llm._session, "post", return_value=llm_obj) as mock_post:
            assert llm._classify_intent("讲个笑话") == "none"
            assert mock_post.called

    def test_decider_unavailable_falls_back_to_llm(self):
        import agent.llm as llm
        llm._st.intent_cache.clear()
        llm_obj = MagicMock()
        llm_obj.json.return_value = {"choices": [{"message": {"content": "none"},
                                                  "finish_reason": "stop"}]}
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "decide_intent", side_effect=RuntimeError("down")), \
             patch.object(llm._session, "post", return_value=llm_obj):
            # 模块内部已兜住异常；即使泄漏，外层 try 也必须兜住
            assert llm._classify_intent("你好呀") == "none"

    def test_keyword_match_still_wins_over_decider(self):
        """关键词快路径优先级不变：decider 不参与关键词命中的句子。"""
        import agent.llm as llm
        llm._st.intent_cache.clear()
        with patch.dict(os.environ, {"DECIDE_ENABLED": "1"}), \
             patch.object(di, "decide_intent", side_effect=AssertionError("不该问 decider")):
            assert llm._classify_intent("打开空调") == "ac-control"
