"""Round 14: LLM 路径 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestLiteStreamWorkingMemory:
    def test_no_intent_none_before_lite(self):
        """lite 流前不应再用 intent='none' 更新 working_memory。"""
        from agent import llm
        src = inspect.getsource(llm)
        # 找到 lite stream 入口附近的代码
        lite_idx = src.find("_chat_lite_stream(text, session_id, channel=channel)")
        assert lite_idx > 0
        # 检查 lite 入口前 500 字符内不应有 intent="none"
        before_lite = src[max(0, lite_idx - 500):lite_idx]
        assert 'intent="none"' not in before_lite, \
            "lite 流前不应再用 intent='none' 更新 working_memory"

    def test_lite_updates_working_memory_after_success(self):
        """lite 流成功后应更新 working_memory，intent 非 'none'。"""
        from agent import llm
        src = inspect.getsource(llm)
        # 找到 lite 成功后的 return 语句附近
        lite_return_idx = src.find("if _lite_yielded:")
        assert lite_return_idx > 0
        after_lite = src[lite_return_idx:lite_return_idx + 500]
        assert "update_working_memory" in after_lite, \
            "lite 成功后应调用 update_working_memory"
        assert 'intent="none"' not in after_lite, \
            "lite 成功后的 intent 不应为 'none'"


class TestChunkNaturalSplit:
    def test_chunk_split_searches_separator(self):
        """长句切分应搜索自然停顿点（标点），而非硬截断。"""
        from agent import llm
        src = inspect.getsource(llm)
        # 找到 _MAX_CHUNK 截断逻辑
        chunk_idx = src.find("len(unsent) >= _MAX_CHUNK")
        assert chunk_idx > 0
        nearby = src[chunk_idx:chunk_idx + 400]
        assert "rfind" in nearby or "sep" in nearby, \
            "长句切分应搜索标点作为自然停顿点"
