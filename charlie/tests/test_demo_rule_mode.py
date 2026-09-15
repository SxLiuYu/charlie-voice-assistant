"""
T1 — Demo 规则模式测试

Demo 规则模式：无 LLM/无 key 时，brain() 走分类+引导提示，
不调 _get_brain（避免 RuntimeError）。角色切换/社交仍走快路径。

Seam: voice_agent.brain() 公共接口（Demo 规则模式 unit seam）
"""
import datetime
from unittest.mock import patch, MagicMock

import pytest

import voice_agent


class TestDemoRuleMode:
    """Demo 规则模式：无 LLM 时的回退。"""

    def _demo_mode(self):
        """进入 Demo 模式（ARK_KEY 空 + Ollama 离线）的 patch 上下文"""
        return (
            patch("agent.llm._demo_mode_active", return_value=True),
            patch("agent.llm._ollama_online", return_value=False),
        )

    def test_demo_mode_time_query_returns_hint(self):
        """Demo 模式下'几点了'不再有时间快路径，返回 Demo 引导提示。"""
        p1, p2 = self._demo_mode()
        with p1, p2, patch("agent.llm._get_brain") as mock_brain, \
             patch("agent.llm._classify_intent", return_value="magic-info"):
            reply = voice_agent.brain("几点了")
        assert ("Demo" in reply or "配置" in reply or "key" in reply.lower()), \
            f"应返回引导配置提示，实际: {reply}"
        mock_brain.assert_not_called()

    def test_demo_mode_time_query_returns_hint_format(self):
        """Demo 模式下'几点啦'返回引导提示而非时间。"""
        p1, p2 = self._demo_mode()
        with p1, p2, patch("agent.llm._get_brain"), \
             patch("agent.llm._classify_intent", return_value="magic-info"):
            reply = voice_agent.brain("几点啦")
        assert ("Demo" in reply or "配置" in reply or "key" in reply.lower()), \
            f"应返回引导配置提示，实际: {reply}"

    def test_demo_mode_scene_trigger_goodnight(self):
        """Demo 模式下'晚安'不再有场景快路径，返回 Demo 拦截提示。"""
        p1, p2 = self._demo_mode()
        with p1, p2, patch("agent.llm._get_brain") as mock_brain, \
             patch("agent.llm._classify_intent", return_value="magic-scenes"):
            reply = voice_agent.brain("晚安")
        assert "Demo" in reply or "配置" in reply or "key" in reply.lower(), \
            f"应返回引导配置提示，实际: {reply}"
        mock_brain.assert_not_called()

    def test_demo_mode_unmatched_returns_demo_hint(self):
        """Demo 模式下未命中快路径时返回引导配置的提示，不调 LLM。

        '讲个笑话'不命中角色切换/社交快路径，
        Demo 模式应返回含'Demo'或'配置'的提示串，而非'大脑启动失败'。
        """
        p1, p2 = self._demo_mode()
        with p1, p2, patch("agent.llm._get_brain") as mock_brain, \
             patch("agent.llm._classify_intent", return_value="none"):
            reply = voice_agent.brain("讲个笑话")
        assert ("Demo" in reply or "配置" in reply or "key" in reply.lower()), \
            f"应返回引导配置提示，实际: {reply}"
        mock_brain.assert_not_called()

    def test_non_demo_mode_unmatched_calls_brain(self):
        """非 Demo 模式（ARK_KEY 已配）下未命中快路径应正常调 LLM。"""
        with patch("agent.llm._demo_mode_active", return_value=False), \
             patch("agent.llm._classify_intent", return_value="none"), \
             patch("agent.llm._chat_lite_stream", return_value=iter([])), \
             patch("agent.llm._get_brain") as mock_brain:
            mock_brain.return_value = MagicMock()
            mock_brain.return_value.run = MagicMock(return_value=["笑话来了"])
            try:
                voice_agent.brain("讲个笑话")
            except Exception:
                pass  # brain 内部逻辑可能抛，这里只验证 _get_brain 被调
        mock_brain.assert_called()
