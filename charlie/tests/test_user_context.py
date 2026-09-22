"""FR-14 多人场景 — 会话级用户上下文测试。

覆盖 agent/user_context.py 的三层解析优先级与并发隔离：
  显式 bind_user → session 注册表 → 环境变量兜底

同时验证 ContextVar 在 ThreadPoolExecutor 中的行为（改造方案 §4 第 4 步的
已知陷阱：线程池任务不继承 ContextVar）。
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from agent import user_context as uc


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空会话注册表，避免跨用例污染。"""
    uc.clear_all_session_users()
    yield
    uc.clear_all_session_users()


# ─── 第 1 层：环境变量兜底 ────────────────────────────────────────────────

class TestEnvFallback:
    def test_no_binding_no_session_uses_env(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "laoyu"}):
            assert uc.resolve_user() == "laoyu"
            assert uc.resolve_user("sess-1") == "laoyu"

    def test_missing_env_falls_back_to_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CHARLIE_USER_ID"}
        with patch.dict(os.environ, env, clear=True):
            assert uc.resolve_user() == uc.DEFAULT_USER

    def test_empty_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": ""}):
            assert uc.resolve_user() == uc.DEFAULT_USER

    def test_unbound_session_does_not_leak_other_session(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            uc.set_session_user("sess-A", "laoyu")
            # 另一个会话未绑定 → 回落到 env，不串到 sess-A 的用户
            assert uc.resolve_user("sess-B") == "default"


# ─── 第 2 层：session 注册表 ──────────────────────────────────────────────

class TestSessionRegistry:
    def test_session_binding_wins_over_env(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            uc.set_session_user("sess-A", "laoyu")
            assert uc.resolve_user("sess-A") == "laoyu"

    def test_two_sessions_resolve_independently(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            uc.set_session_user("sess-A", "laoyu")
            uc.set_session_user("sess-B", "jiaren1")
            assert uc.resolve_user("sess-A") == "laoyu"
            assert uc.resolve_user("sess-B") == "jiaren1"

    def test_default_user_is_not_registered(self):
        uc.set_session_user("sess-A", uc.DEFAULT_USER)
        assert uc.get_session_user("sess-A") is None
        assert uc.session_user_count() == 0

    def test_empty_user_clears_binding(self):
        uc.set_session_user("sess-A", "laoyu")
        assert uc.get_session_user("sess-A") == "laoyu"
        uc.set_session_user("sess-A", "")
        assert uc.get_session_user("sess-A") is None

    def test_none_user_clears_binding(self):
        uc.set_session_user("sess-A", "laoyu")
        uc.set_session_user("sess-A", None)
        assert uc.get_session_user("sess-A") is None

    def test_empty_session_id_is_noop(self):
        uc.set_session_user("", "laoyu")
        assert uc.session_user_count() == 0
        assert uc.get_session_user("") is None
        assert uc.get_session_user(None) is None

    def test_clear_session_user(self):
        uc.set_session_user("sess-A", "laoyu")
        uc.clear_session_user("sess-A")
        assert uc.get_session_user("sess-A") is None

    def test_clear_all_session_users(self):
        uc.set_session_user("sess-A", "laoyu")
        uc.set_session_user("sess-B", "jiaren1")
        assert uc.session_user_count() == 2
        uc.clear_all_session_users()
        assert uc.session_user_count() == 0


# ─── 第 3 层：显式绑定（最高优先级） ──────────────────────────────────────

class TestExplicitBinding:
    def test_bind_wins_over_session_and_env(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            uc.set_session_user("sess-A", "laoyu")
            with uc.bind_user("jiaren1"):
                assert uc.resolve_user("sess-A") == "jiaren1"

    def test_bind_restores_after_exit(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            uc.set_session_user("sess-A", "laoyu")
            with uc.bind_user("jiaren1"):
                assert uc.resolve_user("sess-A") == "jiaren1"
            # 退出后回到 session 层
            assert uc.resolve_user("sess-A") == "laoyu"

    def test_nested_bind_restores_inner_then_outer(self):
        with uc.bind_user("outer"):
            assert uc.resolve_user() == "outer"
            with uc.bind_user("inner"):
                assert uc.resolve_user() == "inner"
            assert uc.resolve_user() == "outer"

    def test_bind_empty_falls_back_to_default(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            with uc.bind_user(""):
                assert uc.resolve_user() == uc.DEFAULT_USER
            with uc.bind_user(None):
                assert uc.resolve_user() == uc.DEFAULT_USER

    def test_bind_exits_on_exception(self):
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            with pytest.raises(ValueError):
                with uc.bind_user("laoyu"):
                    assert uc.resolve_user() == "laoyu"
                    raise ValueError("boom")
            # 异常路径也必须恢复
            assert uc.current_binding() is None
            assert uc.resolve_user() == uc.DEFAULT_USER

    def test_current_binding_reflects_state(self):
        assert uc.current_binding() is None
        with uc.bind_user("laoyu"):
            assert uc.current_binding() == "laoyu"
        assert uc.current_binding() is None

    def test_bind_does_not_write_environ(self):
        """本模块不应产生进程级副作用（改造方案核心约束）。"""
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            with uc.bind_user("laoyu"):
                assert os.environ["CHARLIE_USER_ID"] == "default"


# ─── 并发隔离 ─────────────────────────────────────────────────────────────

class TestConcurrency:
    def test_threads_do_not_share_binding(self):
        """每个线程各自 bind_user，互不干扰。"""
        results: dict[str, str] = {}
        barrier = threading.Barrier(4)

        def worker(uid: str):
            with uc.bind_user(uid):
                barrier.wait(timeout=5)   # 确保四个线程同时在绑定状态内
                results[uid] = uc.resolve_user()

        threads = [threading.Thread(target=worker, args=(f"user{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == {f"user{i}": f"user{i}" for i in range(4)}

    def test_session_registry_is_thread_safe(self):
        """并发写注册表不丢绑定。"""
        def writer(i: int):
            uc.set_session_user(f"sess-{i}", f"user{i}")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(writer, range(50)))

        assert uc.session_user_count() == 50
        for i in range(50):
            assert uc.get_session_user(f"sess-{i}") == f"user{i}"

    def test_pool_does_not_inherit_binding_without_copy_context(self):
        """记录已知陷阱：ThreadPoolExecutor 不继承 ContextVar。

        这是改造方案 §4 第 4 步的重点排查项。此处固化当前行为，
        以便将来若改为 copy_context() 注入，测试能及时暴露差异。
        """
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            with uc.bind_user("laoyu"):
                with ThreadPoolExecutor(max_workers=1) as ex:
                    inherited = ex.submit(uc.resolve_user).result()
            # 未显式 copy_context → 池内看不到绑定，回落到 env
            assert inherited == "default"

    def test_pool_inherits_binding_with_copy_context(self):
        """显式 copy_context() 可把绑定带入线程池（推荐做法）。"""
        import contextvars

        with patch.dict(os.environ, {"CHARLIE_USER_ID": "default"}):
            with uc.bind_user("laoyu"):
                ctx = contextvars.copy_context()
                with ThreadPoolExecutor(max_workers=1) as ex:
                    inherited = ex.submit(ctx.run, uc.resolve_user).result()
            assert inherited == "laoyu"


# ─── 与其他模块的契约 ─────────────────────────────────────────────────────

class TestContract:
    def test_resolve_user_default_signature(self):
        """resolve_user() 必须可不带参数调用（兼容现有调用点）。"""
        with patch.dict(os.environ, {"CHARLIE_USER_ID": "laoyu"}):
            assert uc.resolve_user() == "laoyu"

    def test_default_user_constant(self):
        assert uc.DEFAULT_USER == "default"

    def test_does_not_import_heavy_modules(self):
        """本模块应保持轻量，避免拖慢启动或引入循环依赖。"""
        src = uc.__file__
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        for forbidden in ("import requests", "import torch", "import fastapi"):
            assert forbidden not in content
