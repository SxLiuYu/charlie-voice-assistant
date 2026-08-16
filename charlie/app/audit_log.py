"""功能调用审计日志

记录所有功能调用的完整轨迹，区分用户调用 vs 测试调用。
- 用户调用: logs/calls_user.log (JSON Lines)
- 测试调用: logs/calls_test.log (JSON Lines)

每条记录: timestamp, source(user/test/system), feature, action, input, output, duration_ms, success, error, session_id, caller

使用方式:
    from app.audit_log import audit_call
    with audit_call("feature_name", input_data, session_id="default"):
        # do work
        result = ...
    # 或直接调用:
    audit_log("feature_name", input="...", output="...", success=True)
"""
import os
import sys
import json
import time
import functools
import threading
import logging
from datetime import datetime
from contextlib import contextmanager

log = logging.getLogger("magic")

_LOG_DIR = os.environ.get("ASSISTANT_KID_LOG_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"))
os.makedirs(_LOG_DIR, exist_ok=True)

_USER_LOG_FILE = os.path.join(_LOG_DIR, "calls_user.log")
_TEST_LOG_FILE = os.path.join(_LOG_DIR, "calls_test.log")
_SYSTEM_LOG_FILE = os.path.join(_LOG_DIR, "calls_system.log")

_lock = threading.Lock()

# 线程局部: 记录当前调用上下文 (用于嵌套调用溯源)
_local = threading.local()


def _detect_source() -> str:
    """检测当前调用来源: test / user / system"""
    # 1. pytest 环境
    if "pytest" in sys.modules:
        test_name = os.environ.get("PYTEST_CURRENT_TEST", "")
        if test_name:
            return "test"
        # pytest 已导入但没有当前测试 → 可能是 conftest 加载阶段
        return "test"
    # 2. 显式标记 (audit_call 传入 source 参数)
    ctx_source = getattr(_local, "source", None)
    if ctx_source:
        return ctx_source
    # 3. 默认用户调用
    return "user"


def _get_log_file(source: str) -> str:
    if source == "test":
        return _TEST_LOG_FILE
    elif source == "system":
        return _SYSTEM_LOG_FILE
    return _USER_LOG_FILE


def _truncate(s, maxlen=200):
    if s is None:
        return ""
    s = str(s)
    if len(s) > maxlen:
        return s[:maxlen] + f"...({len(s)}chars)"
    return s


def _get_caller(depth=3):
    """获取调用栈信息"""
    try:
        frame = sys._getframe(depth)
        return f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}:{frame.f_code.co_name}"
    except Exception:
        return "unknown"


def audit_log(feature: str, input_data=None, output_data=None, success=True,
              error=None, duration_ms=0.0, session_id="default",
              source: str = None, action: str = None):
    """记录一条功能调用日志

    Args:
        feature: 功能名称 (如 'voice_loop', 'brain', 'fast_path:ac', 'mcp:magic-recipe')
        input_data: 输入数据 (会截断到200字符)
        output_data: 输出数据 (会截断到200字符)
        success: 是否成功
        error: 错误信息
        duration_ms: 耗时(毫秒)
        session_id: 会话ID
        source: 调用来源 (user/test/system), None=自动检测
        action: 具体动作 (如 'search_recipe', 'ac_control')
    """
    src = source or _detect_source()
    caller = _get_caller(depth=3)
    entry = {
        "ts": datetime.now().isoformat(),
        "source": src,
        "feature": feature,
        "action": action or "",
        "input": _truncate(input_data),
        "output": _truncate(output_data),
        "success": success,
        "error": _truncate(error, 500) if error else "",
        "duration_ms": round(duration_ms, 1),
        "session_id": session_id,
        "caller": caller,
        "pid": os.getpid(),
    }
    log_file = _get_log_file(src)
    try:
        with _lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug(f"[audit] 写日志失败: {e}")


@contextmanager
def audit_call(feature: str, input_data=None, session_id: str = "default",
               source: str = None, action: str = None):
    """上下文管理器: 自动计时 + 异常捕获

    用法:
        with audit_call("voice_loop", input_data=audio_len, session_id=sid):
            text, reply, audio = voice_loop(wav)
            # 上下文管理器自动记录成功
    """
    start = time.time()
    prev_source = getattr(_local, "source", None)
    if source:
        _local.source = source
    try:
        yield
        duration = (time.time() - start) * 1000
        audit_log(feature, input_data, None, success=True,
                  duration_ms=duration, session_id=session_id,
                  source=source, action=action)
    except Exception as e:
        duration = (time.time() - start) * 1000
        audit_log(feature, input_data, None, success=False,
                  error=str(e), duration_ms=duration, session_id=session_id,
                  source=source, action=action)
        raise
    finally:
        _local.source = prev_source


def audit_decorator(feature: str, action: str = None):
    """装饰器: 自动记录函数调用

    用法:
        @audit_decorator("brain", action="llm")
        def brain(text, session_id="default"):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            sid = kwargs.get("session_id", "default")
            inp = str(args[0])[:100] if args else str(kwargs)[:100]
            try:
                result = func(*args, **kwargs)
                duration = (time.time() - start) * 1000
                audit_log(feature, inp, result, success=True,
                          duration_ms=duration, session_id=sid, action=action)
                return result
            except Exception as e:
                duration = (time.time() - start) * 1000
                audit_log(feature, inp, None, success=False,
                          error=str(e), duration_ms=duration, session_id=sid, action=action)
                raise
        return wrapper
    return decorator


def get_call_stats(source: str = None) -> dict:
    """读取调用统计"""
    files = []
    if source is None or source == "user":
        files.append(("user", _USER_LOG_FILE))
    if source is None or source == "test":
        files.append(("test", _TEST_LOG_FILE))
    if source is None or source == "system":
        files.append(("system", _SYSTEM_LOG_FILE))

    stats = {}
    for src, fpath in files:
        if not os.path.exists(fpath):
            continue
        feature_stats = {}
        total = 0
        success = 0
        fail = 0
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        feat = entry.get("feature", "?")
                        if feat not in feature_stats:
                            feature_stats[feat] = {"count": 0, "success": 0, "fail": 0, "avg_ms": 0, "total_ms": 0}
                        feature_stats[feat]["count"] += 1
                        feature_stats[feat]["total_ms"] += entry.get("duration_ms", 0)
                        if entry.get("success"):
                            feature_stats[feat]["success"] += 1
                            success += 1
                        else:
                            feature_stats[feat]["fail"] += 1
                            fail += 1
                        total += 1
                    except json.JSONDecodeError:
                        continue
            for fs in feature_stats.values():
                if fs["count"] > 0:
                    fs["avg_ms"] = round(fs["total_ms"] / fs["count"], 1)
                del fs["total_ms"]
            stats[src] = {"total": total, "success": success, "fail": fail, "features": feature_stats}
        except Exception as e:
            stats[src] = {"error": str(e)}
    return stats
