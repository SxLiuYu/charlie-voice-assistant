"""会话级用户上下文 — 用户身份的唯一解析入口。

背景（FR-14 多人场景）：
    改造前用户身份存放在三个进程级全局位置：
      1. os.environ["CHARLIE_USER_ID"]（且被 MCP 子进程继承）
      2. agent.llm_state.current_user_id（模块级全局）
      3. agent.history.HISTORY_FILE / HISTORY_LOCK_FILE（模块级静态导出）
    任何并发会话都会互相覆盖 —— A 用户切换身份后，正在服务的 B 用户也被改掉。

本模块把"当前用户"从全局单例改为按会话解析，三层优先级：

    显式 ContextVar（本次执行流绑定）
            ↓ 未命中
    session 注册表（session_id → user_id）
            ↓ 未命中
    环境变量 CHARLIE_USER_ID（向后兼容，默认 "default"）

用法：
    # 1) 入口处绑定整个处理流程（推荐，等价写法见 bind_session_user）
    with bind_user(resolve_user(session_id)):
        ...  # 整个 ASR→brain→TTS 流程

    # 1b) 或者直接用封装好的上下文管理器
    with bind_session_user(f"xiaozhi-{device_key}"):
        ...

    # 2) 为会话建立持久映射（设备/房间 → 用户）
    set_session_user(session_id, "laoyu")

    # 3) 数据层函数统一调用
    uid = resolve_user(session_id)

    # 4) 设备身份 → 用户：见 agent/device_users.py
    from agent.device_users import resolve_device_user
    with bind_user(resolve_device_user(device_key)):
        ...

注意：
    - ContextVar 在 asyncio 任务内自动继承，但 ThreadPoolExecutor 提交的任务
      **不会**继承。跨线程池时需显式 contextvars.copy_context() 或直接传 uid。
      重点检查：agent/tts_player.py、app/xiaozhi_ws.py 的 _vad_pool、
      agent/background_task.py。
    - 本模块不写 os.environ，避免进程级副作用。
"""
import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger("magic")

DEFAULT_USER = "default"

# 显式绑定（线程/协程安全）：优先于 session 注册表
_uid_var: ContextVar[str | None] = ContextVar("charlie_user_id", default=None)

# session_id → user_id 注册表（跨请求持久，如设备/房间绑定）
_session_users: dict[str, str] = {}
_lock = threading.RLock()


def set_session_user(session_id: str, user_id: str | None) -> None:
    """绑定会话 → 用户。user_id 为空或 DEFAULT_USER 时清除绑定。"""
    if not session_id:
        return
    with _lock:
        if user_id and user_id != DEFAULT_USER:
            _session_users[session_id] = user_id
        else:
            _session_users.pop(session_id, None)


def get_session_user(session_id: str | None) -> str | None:
    """查询会话绑定的用户；未绑定返回 None。"""
    if not session_id:
        return None
    with _lock:
        return _session_users.get(session_id)


def clear_session_user(session_id: str) -> None:
    """清除单个会话的用户绑定。"""
    with _lock:
        _session_users.pop(session_id, None)


def clear_all_session_users() -> None:
    """清除全部会话绑定（测试与用户切换时使用）。"""
    with _lock:
        _session_users.clear()


def session_user_count() -> int:
    """当前已绑定的会话数（用于诊断/测试）。"""
    with _lock:
        return len(_session_users)


def resolve_user(session_id: str | None = None) -> str:
    """用户身份唯一入口。

    所有需要 uid 的地方都应调用它，不要直接读 os.environ["CHARLIE_USER_ID"]。
    """
    explicit = _uid_var.get()
    if explicit:
        return explicit
    sid_user = get_session_user(session_id)
    if sid_user:
        return sid_user
    return os.environ.get("CHARLIE_USER_ID", DEFAULT_USER) or DEFAULT_USER


class bind_user:
    """显式绑定当前执行流的用户（可嵌套、可重入）。

        with bind_user("laoyu"):
            assert resolve_user() == "laoyu"
        # 退出后自动恢复上一层
    """

    __slots__ = ("_uid", "_token")

    def __init__(self, user_id: str | None):
        self._uid = user_id or DEFAULT_USER
        self._token = None

    def __enter__(self) -> str:
        self._token = _uid_var.set(self._uid)
        return self._uid

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._token is not None:
            _uid_var.reset(self._token)
            self._token = None
        return False


def current_binding() -> str | None:
    """当前执行流的显式绑定值；未绑定时返回 None（用于诊断）。"""
    return _uid_var.get()


@contextmanager
def bind_session_user(session_id: str | None):
    """按 `session_id` 解析用户并绑定当前执行流（入口层标准写法）。

        with bind_session_user(f"xiaozhi-{device_key}"):
            ...  # 整轮 ASR→brain→TTS，数据层自动落到该用户的分片

    解析顺序与 `resolve_user(session_id)` 完全一致（ContextVar → session 注册表 → env），
    所以调用方只要先用 `set_session_user()` 建立映射，这里就能拿到正确 uid。

    ⚠️ ContextVar 会被 `asyncio.create_task()` / `copy_context()` 继承，但
    **不会**被裸 `ThreadPoolExecutor.submit()` 继承。跨线程池时需在提交处
    `ctx = contextvars.copy_context()` 再 `ctx.run(fn)`（`app/xiaozhi_ws.py:885`
    已有此写法，可直接复用）。
    """
    with bind_user(resolve_user(session_id)):
        yield resolve_user(session_id)
