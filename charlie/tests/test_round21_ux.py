"""Round 21: TTS 流结束音频交错竞态 + 工作记忆跨通道串台 修复回归测试。"""
import os
import sys
import inspect
import threading

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestTtsStreamStopOrder:
    """P1-A: _set_xiaozhi_streaming(False) 应在 tts stop 发送之后，消除音频交错窗口。"""

    def test_streaming_false_after_tts_stop(self):
        """streaming 标志应在 tts stop 之后才解除，不应在 trailing_wait 期间就解除。"""
        ws_path = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
        src = open(ws_path).read()
        # 定位 stream_tts_reply 的正常结束路径
        # 旧代码：_set_xiaozhi_streaming(False) → sleep → tts stop
        # 新代码：sleep → tts stop → _set_xiaozhi_streaming(False)
        trailing_idx = src.find("_trailing_wait = min(0.8")
        assert trailing_idx > 0
        # 从 trailing_wait 到 _set_xiaozhi_streaming 的距离
        block = src[trailing_idx:trailing_idx + 600]
        streaming_idx = block.find("_set_xiaozhi_streaming(False)")
        stop_idx = block.find('"type": "tts", "state": "stop"')
        assert streaming_idx > 0 and stop_idx > 0, \
            "正常结束路径应包含 _set_xiaozhi_streaming(False) 和 tts stop"
        assert stop_idx < streaming_idx, \
            "tts stop 应在 _set_xiaozhi_streaming(False) 之前发送，消除音频交错窗口"

    def test_finally_block_still_resets_streaming(self):
        """异常路径 finally 块仍应复位 streaming 标志。"""
        ws_path = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
        src = open(ws_path).read()
        # finally 块中的 _set_xiaozhi_streaming(False) 应保留
        finally_idx = src.find("finally:")
        # 找到 stream_tts_reply 的 finally
        while finally_idx > 0:
            block = src[finally_idx:finally_idx + 200]
            if "_set_xiaozhi_streaming(False)" in block:
                break
            finally_idx = src.find("finally:", finally_idx + 1)
        assert finally_idx > 0, \
            "异常路径 finally 块应仍包含 _set_xiaozhi_streaming(False)"

    def test_flush_after_streaming_false(self):
        """积压提醒 flush 应在 streaming 标志解除后执行。"""
        ws_path = os.path.join(PROJECT_DIR, "app", "xiaozhi_ws.py")
        src = open(ws_path).read()
        trailing_idx = src.find("_trailing_wait = min(0.8")
        block = src[trailing_idx:trailing_idx + 800]
        streaming_idx = block.find("_set_xiaozhi_streaming(False)")
        flush_idx = block.find("flush_xiaozhi_pending")
        assert streaming_idx > 0 and flush_idx > 0, \
            "正常结束路径应包含 streaming 解除和 flush"
        assert flush_idx > streaming_idx, \
            "flush 应在 streaming 标志解除之后执行"


class TestWorkingMemoryThreadIsolation:
    """P1-B: 工作记忆应使用 thread-local session 隔离，跨通道并发不串台。"""

    def test_session_local_is_threading_local(self):
        """WM 应使用 threading.local 实现每线程独立 session。"""
        import agent.working_memory as wm
        assert hasattr(wm, "_session_local"), \
            "WM 模块应有 _session_local thread-local 变量"
        assert isinstance(wm._session_local, threading.local), \
            "_session_local 应为 threading.local 实例"

    def test_switch_session_sets_thread_local(self):
        """switch_session 应设置当前线程的 active session。"""
        import agent.working_memory as wm
        wm.switch_session("test_session_a")
        assert wm._get_session() == "test_session_a"

    def test_concurrent_sessions_isolated(self):
        """两个线程各自操作不同会话，WM 数据不应串台。"""
        import agent.working_memory as wm
        results = {}
        barrier = threading.Barrier(2)

        def worker(session_id, entity, delay):
            wm.switch_session(session_id)
            barrier.wait()  # 确保两线程同时开始
            wm.update(entity=entity)
            import time
            time.sleep(delay)  # 让另一线程在此期间也操作
            snap = wm.get_all()
            results[session_id] = snap["last_entity"]

        t1 = threading.Thread(target=worker, args=("session_x", "apple", 0.05))
        t2 = threading.Thread(target=worker, args=("session_y", "banana", 0.05))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results.get("session_x") == "apple", \
            f"session_x 的 entity 应为 apple，实际 {results.get('session_x')}（串台了）"
        assert results.get("session_y") == "banana", \
            f"session_y 的 entity 应为 banana，实际 {results.get('session_y')}（串台了）"

    def test_llm_uses_thread_local_session(self):
        """llm.py 应使用 thread-local 的 session 跟踪，不再用全局变量。"""
        llm_path = os.path.join(PROJECT_DIR, "agent", "llm.py")
        src = open(llm_path).read()
        assert "_wm_local" in src, \
            "llm.py 应使用 _wm_local (threading.local) 替代全局 _last_wm_session_id"
        assert "_get_last_wm_session" in src, \
            "应有 _get_last_wm_session() 函数获取当前线程 session"
        assert "_set_last_wm_session" in src, \
            "应有 _set_last_wm_session() 函数设置当前线程 session"
        # 不应再使用 global _last_wm_session_id
        assert "global _last_wm_session_id" not in src, \
            "不应再使用 global _last_wm_session_id（已改为 thread-local）"

    def test_wm_no_global_single_slot(self):
        """WM 不应再使用单槽 _working_memory 全局变量。"""
        wm_path = os.path.join(PROJECT_DIR, "agent", "working_memory.py")
        src = open(wm_path).read()
        # 旧的单槽设计不应存在
        assert "_working_memory = {" not in src, \
            "不应再使用单槽 _working_memory 全局变量（已改为按 session_id 隔离的 _session_data）"
        assert "_session_data" in src, \
            "应使用 _session_data 字典按 session_id 隔离存储"
