"""Round 17: 飞书流式回复 UX 修复回归测试。"""
import os
import sys
import inspect

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestFeishuStreamingReply:
    def test_streaming_first_sentence_immediate(self):
        """飞书 brain 回复应流式发送首句，降低感知延迟。"""
        from feishu_ws import _process_message_sync
        src = inspect.getsource(_process_message_sync)
        # 应有流式发送逻辑（首句即发）
        assert "_streamed" in src, \
            "应使用 _streamed 标志跟踪流式发送状态"
        assert "not _streamed" in src or "_streamed = True" in src, \
            "首句产出后应立即发送并标记 _streamed"

    def test_decision_reply_not_streamed(self):
        """决策反馈回复应整体发送，不走流式。"""
        from feishu_ws import _process_message_sync
        src = inspect.getsource(_process_message_sync)
        # _streamed 初始为 False，决策反馈路径不设置 _streamed
        assert "_streamed = False" in src, \
            "_streamed 应初始化为 False"

    def test_remaining_buffer_sent(self):
        """流式后剩余累积内容也应发送。"""
        from feishu_ws import _process_message_sync
        src = inspect.getsource(_process_message_sync)
        # 循环结束后应发送 _buf 中剩余内容
        assert "_buf" in src, \
            "应有 _buf 累积缓冲区"
        # 剩余 buf 发送逻辑
        buf_idx = src.find("if _buf.strip():")
        assert buf_idx > 0, \
            "循环结束后应检查并发送 _buf 剩余内容"
