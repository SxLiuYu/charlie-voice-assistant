"""请求级追踪：trace_id + 阶段耗时。

为什么需要
----------
一次语音交互会横跨多个模块：WebSocket 收帧 → VAD 端点 → ASR → 意图分类 →
LLM → MCP 工具 → TTS 合成 → Opus 编码 → 下发。默认日志里这些行是散的，
排查"这次为什么慢/为什么没响应"时很难把它们串起来。

本模块提供：
1. ``trace_id``：一次交互一个短 ID，通过 ContextVar 传递，并由 logging
   过滤器自动注入到该执行流产生的每条日志。
2. ``stage()``：阶段计时上下文管理器，自动按级别输出耗时（慢则升级为 WARNING）。

用法
----
    from app.trace import new_trace, stage, current_trace

    tid = new_trace()                     # 或在入口 with bind_trace(tid):
    log.info("收到语音")                   # 输出: [a1b2c3d4] 收到语音
    with stage("asr"):
        text = asr(audio)
    with stage("brain"):
        reply = brain(text)

注意
----
ContextVar 在 asyncio 任务内自动继承，但 ``ThreadPoolExecutor`` 提交的任务
**不会**继承。跨线程池时需 ``contextvars.copy_context().run(...)``
或显式传 trace_id。
"""
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger("magic")

_trace_var: ContextVar[str | None] = ContextVar("charlie_trace_id", default=None)

# 阶段耗时超过该阈值时，日志级别从 DEBUG 升到 WARNING（便于发现性能退化）
SLOW_STAGE_MS = float(os.environ.get("TRACE_SLOW_STAGE_MS", "3000"))

# 阶段日志是否输出（默认关闭，避免正常运行时刷屏；排查时设 TRACE_STAGES=1）
STAGES_ENABLED = os.environ.get("TRACE_STAGES", "0") == "1"


def new_trace(prefix: str = "") -> str:
    """生成并绑定一个新的 trace_id（8 位十六进制）。"""
    tid = uuid.uuid4().hex[:8]
    if prefix:
        tid = f"{prefix}:{tid}"
    _trace_var.set(tid)
    return tid


def set_trace(tid: str | None) -> None:
    _trace_var.set(tid)


def current_trace() -> str | None:
    """当前执行流的 trace_id；未绑定时返回 None。"""
    return _trace_var.get()


class TraceFilter(logging.Filter):
    """把当前 trace_id 注入到 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "trace_id", None) is None:
            record.trace_id = _trace_var.get()
        return True


@contextmanager
def bind_trace(tid: str | None = None, prefix: str = ""):
    """在上下文内绑定 trace_id；退出后恢复上一层。"""
    if tid is None:
        tid = new_trace(prefix)
    token = _trace_var.set(tid)
    try:
        yield tid
    finally:
        _trace_var.reset(token)


@contextmanager
def stage(name: str, *, slow_ms: float | None = None, extra: str = ""):
    """阶段计时。默认不输出（TRACE_STAGES=1 开启），超时则强制 WARNING。"""
    threshold = SLOW_STAGE_MS if slow_ms is None else slow_ms
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        suffix = f" {extra}" if extra else ""
        if elapsed_ms >= threshold:
            log.warning("[stage] %s=%.0fms (超过阈值 %.0fms)%s",
                        name, elapsed_ms, threshold, suffix)
        elif STAGES_ENABLED:
            log.info("[stage] %s=%.0fms%s", name, elapsed_ms, suffix)


def install_filter() -> None:
    """把 TraceFilter 挂到根 logger 的所有 handler 上（幂等）。

    logging_setup.setup_logging() 会在建立 handler 后调用它。
    """
    flt = TraceFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, TraceFilter) for f in handler.filters):
            handler.addFilter(flt)
