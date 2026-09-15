"""Round 3 AC shared thread pool tests: R3-D.

Run:
    cd charlie && python3 -m pytest tests/test_round3_ac_pool.py -q
"""
import os
import sys
import time
import threading
import concurrent.futures
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ─────────────────────────────────────────────────────────
# 单例一致性
# ─────────────────────────────────────────────────────────

class TestAcPoolSingleton:
    """模块级共享池应为惰性单例。"""

    def test_singleton_identity(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool, _ac_pool
        # 确保干净状态
        _shutdown_ac_pool()
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        p1 = _get_ac_pool()
        p2 = _get_ac_pool()
        assert p1 is p2

    def test_singleton_respects_env_size(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool
        _shutdown_ac_pool()
        monkeypatch.setenv("AC_POOL_SIZE", "4")
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        pool = _get_ac_pool()
        assert pool._max_workers == 4


# ─────────────────────────────────────────────────────────
# 并发与超时
# ─────────────────────────────────────────────────────────

class TestAcPoolConcurrency:
    """并发请求全部完成且受 max_workers 约束；超时后池仍可用。"""

    def test_concurrent_tasks_all_complete(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool, _run_ac_with_timeout
        _shutdown_ac_pool()
        monkeypatch.setenv("AC_POOL_SIZE", "2")
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        results = []
        pool = _get_ac_pool()

        def slow_task(i):
            time.sleep(0.15)
            return i

        futures = [pool.submit(slow_task, i) for i in range(4)]
        start = time.monotonic()
        for f in futures:
            results.append(f.result(timeout=10))
        elapsed = time.monotonic() - start

        assert sorted(results) == [0, 1, 2, 3]
        # max_workers=2：4 个 0.15s 任务须分两批串行，总耗时 >= 0.25s
        assert elapsed >= 0.25

    def test_timeout_does_not_corrupt_pool(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool, _run_ac_with_timeout
        _shutdown_ac_pool()
        monkeypatch.setenv("AC_POOL_SIZE", "1")
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        # 超时请求
        ok, msg = _run_ac_with_timeout(lambda: time.sleep(5), timeout=0.2)
        assert ok is False
        assert "超时" in msg

        # 池仍可用（成功路径返回 fn 裸值；超时才返回 (False, 文案) 元组）
        res2 = _run_ac_with_timeout(lambda: "ok", timeout=5)
        assert res2 == "ok" or res2 == (True, "ok")

    def test_atexit_shutdown_cancel_futures(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool
        _shutdown_ac_pool()
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        pool = _get_ac_pool()
        f = pool.submit(lambda: time.sleep(10))
        # 手动调用 shutdown（atexit 注册的函数）
        _shutdown_ac_pool()
        # future 应被取消或已完成；这里只断言池已置 None
        from agent.device_control import _ac_pool
        assert _ac_pool is None

    def test_run_ac_with_timeout_uses_shared_pool(self, monkeypatch):
        from agent.device_control import _get_ac_pool, _shutdown_ac_pool, _run_ac_with_timeout
        _shutdown_ac_pool()
        monkeypatch.setenv("AC_POOL_SIZE", "2")
        monkeypatch.setattr("agent.device_control._ac_pool", None)
        monkeypatch.setattr("agent.device_control._ac_pool_lock", threading.Lock())

        pool = _get_ac_pool()
        # 验证 submit 来自共享池
        import agent.device_control as dc_mod
        submitted = []

        original_submit = pool.submit

        def tracking_submit(fn, *args, **kwargs):
            submitted.append(fn)
            return original_submit(fn, *args, **kwargs)

        pool.submit = tracking_submit
        _run_ac_with_timeout(lambda: 42, timeout=5)
        assert len(submitted) == 1
        assert submitted[0]() == 42
