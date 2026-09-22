"""统一日志配置 —— 面向问题排查与调优。

设计目标
--------
1. **可调**：不用改代码就能把某个模块的日志调细（排查 ESP32 连接问题时尤其需要）。
2. **可读**：启动时打印生效配置，避免"以为开了 DEBUG 其实没开"。
3. **可查**：轮转文件 + 可选 JSON，便于事后 grep 与结构化分析。
4. **不打扰**：默认 INFO；逐帧/逐消息级别的噪声日志一律走 DEBUG。

环境变量
--------
===================  ==========================================================
LOG_LEVEL            全局级别，默认 ``INFO``
LOG_LEVELS           按 logger 覆盖，逗号分隔 ``名称=级别``，支持点分层次继承
                     例：``LOG_LEVELS="app.xiaozhi_ws=DEBUG,agent.llm=DEBUG"``
LOG_FORMAT           ``text``（默认）或 ``json``
LOG_DIR              日志目录；优先读 ``ASSISTANT_KID_LOG_DIR``
LOG_FILE_DISABLE     ``1`` = 不写文件（HTTPS 双进程时避免同写 app.log）
LOG_MAX_BYTES        单文件上限，默认 5MB
LOG_BACKUP_COUNT     轮转份数，默认 3
LOG_CONSOLE_LEVEL    控制台级别，默认跟随 LOG_LEVEL
===================  ==========================================================

用法
----
    from app.logging_setup import setup_logging, set_level, describe_logging

    setup_logging()                 # 幂等；在服务启动最早期调用
    set_level("DEBUG", "app.xiaozhi_ws")   # 运行时调细某个模块
    print(describe_logging())       # 人类可读的当前配置

排查提示
--------
想排查 ESP32 连接问题，推荐：

    LOG_LEVELS="app.xiaozhi_ws=DEBUG,app.mqtt_server=DEBUG" python voice_server.py

或运行时不重启直接调：

    curl -X POST localhost:8000/api/logs/level -d '{"level":"DEBUG","logger":"app.xiaozhi_ws"}'
"""
import os
import sys
import json
import logging
import logging.handlers
import threading

_lock = threading.RLock()
_configured = False
_effective: dict = {}

# 这些 logger 平时很吵，默认压到 WARNING（可被 LOG_LEVELS 覆盖）
_NOISY_DEFAULTS = {
    "urllib3": "WARNING",
    "requests": "WARNING",
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "asyncio": "WARNING",
    "multipart": "WARNING",
    "uvicorn.access": "WARNING",
    "websockets.client": "WARNING",
    "charset_normalizer": "WARNING",
    "filelock": "WARNING",
    "matplotlib": "WARNING",
}

_LEVEL_ALIASES = {
    "TRACE": logging.DEBUG,
    "VERBOSE": logging.DEBUG,
    "WARN": logging.WARNING,
    "FATAL": logging.CRITICAL,
}


def _parse_level(value: str, default: int = logging.INFO) -> int:
    if not value:
        return default
    name = value.strip().upper()
    if name in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[name]
    return getattr(logging, name, default)


def _parse_overrides(raw: str) -> dict[str, int]:
    """解析 ``a.b=DEBUG,c=INFO`` 形式的覆盖表。"""
    result: dict[str, int] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, level = item.partition("=")
        name = name.strip()
        if name:
            result[name] = _parse_level(level, logging.INFO)
    return result


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志（便于 ELK / jq 分析）。"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
            "msg": record.getMessage(),
        }
        # trace_id 由 app/trace.py 注入（若已启用）
        tid = getattr(record, "trace_id", None)
        if tid:
            entry["trace_id"] = tid
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        return json.dumps(entry, ensure_ascii=False)


class TraceFormatter(logging.Formatter):
    """文本格式，有 trace_id 时自动带上，便于串联一次对话。"""

    def __init__(self, fmt: str, datefmt: str | None = None):
        super().__init__(fmt, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        tid = getattr(record, "trace_id", None)
        if tid and not getattr(record, "_charlie_tid_done", False):
            record.msg = f"[{tid}] {record.msg}"
            record._charlie_tid_done = True
        return super().format(record)


def _log_dir() -> str:
    return (
        os.environ.get("ASSISTANT_KID_LOG_DIR")
        or os.environ.get("LOG_DIR")
        or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    )


def setup_logging(*, force: bool = False) -> dict:
    """初始化日志。幂等（除非 force=True）。返回生效配置字典。"""
    global _configured, _effective
    with _lock:
        if _configured and not force:
            return _effective

        level = _parse_level(os.environ.get("LOG_LEVEL", "INFO"), logging.INFO)
        console_level = _parse_level(os.environ.get("LOG_CONSOLE_LEVEL", ""), level)
        fmt = (os.environ.get("LOG_FORMAT", "text") or "text").strip().lower()
        overrides = _parse_overrides(os.environ.get("LOG_LEVELS", ""))

        # 1) 根 logger：只挂控制台，避免重复输出
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.setLevel(logging.DEBUG)  # 交给各 handler 的级别过滤

        console = logging.StreamHandler(sys.stderr)
        if fmt == "json":
            console.setFormatter(JsonFormatter())
        else:
            console.setFormatter(TraceFormatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
        console.setLevel(console_level)
        root.addHandler(console)

        # 2) 文件 handler（轮转）
        file_path = ""
        file_enabled = os.environ.get("LOG_FILE_DISABLE") != "1"
        if file_enabled:
            log_dir = _log_dir()
            try:
                os.makedirs(log_dir, exist_ok=True)
                file_path = os.path.join(log_dir, "app.log")
                max_bytes = int(os.environ.get("LOG_MAX_BYTES", str(5_000_000)))
                backups = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
                fh = logging.handlers.RotatingFileHandler(
                    file_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
                fh.setLevel(level)
                if fmt == "json":
                    fh.setFormatter(JsonFormatter())
                else:
                    fh.setFormatter(TraceFormatter(
                        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
                root.addHandler(fh)
            except Exception as e:  # 目录不可写等不应阻断启动
                file_enabled = False
                file_path = ""
                logging.getLogger(__name__).warning("文件日志不可用: %s", e)

        # 3) 默认静音嘈杂的第三方 logger（LOG_LEVELS 可覆盖）
        for name, lvl in _NOISY_DEFAULTS.items():
            logging.getLogger(name).setLevel(lvl)

        # 4) 全局级别 + 显式覆盖
        logging.getLogger().setLevel(level)
        for name, lvl in overrides.items():
            logging.getLogger(name).setLevel(lvl)

        _configured = True
        _effective = {
            "level": logging.getLevelName(level),
            "console_level": logging.getLevelName(console_level),
            "format": fmt,
            "file_enabled": file_enabled,
            "file": file_path,
            "overrides": {k: logging.getLevelName(v) for k, v in overrides.items()},
        }

    # 5) 挂上 trace_id 注入过滤器（放在锁外，避免与 trace 模块相互导入时死锁）
    try:
        from app.trace import install_filter
        install_filter()
    except Exception as e:  # 追踪不可用不应阻断日志
        logging.getLogger(__name__).debug("trace 过滤器安装失败: %s", e)

    return _effective


def set_level(level: str | int, logger_name: str | None = None) -> str:
    """运行时调整日志级别（不重启生效）。

    level: 级别名或 logging 常量；logger_name 为空时调整根 logger。
    返回生效后的级别名。
    """
    lvl = level if isinstance(level, int) else _parse_level(str(level), logging.INFO)
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target.setLevel(lvl)
    with _lock:
        if logger_name:
            _effective.setdefault("overrides", {})[logger_name] = logging.getLevelName(lvl)
        else:
            _effective["level"] = logging.getLevelName(lvl)
    logging.getLogger(__name__).info(
        "日志级别已调整: %s=%s", logger_name or "<root>", logging.getLevelName(lvl))
    return logging.getLevelName(lvl)


def get_level(logger_name: str | None = None) -> str:
    """读取指定 logger 的**生效**级别（含继承）。"""
    lg = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    return logging.getLevelName(lg.getEffectiveLevel())


def describe_logging() -> str:
    """人类可读的当前配置，用于启动日志与诊断接口。"""
    setup_logging()
    lines = [
        f"级别={_effective.get('level')} 格式={_effective.get('format')}",
        f"控制台={_effective.get('console_level')}",
        f"文件={'禁用' if not _effective.get('file_enabled') else _effective.get('file')}",
    ]
    ov = _effective.get("overrides") or {}
    lines.append("覆盖=" + (", ".join(f"{k}={v}" for k, v in ov.items()) if ov else "无"))
    return " | ".join(lines)


def effective_config() -> dict:
    """返回生效配置的副本（供 /api/logs 等诊断接口使用）。"""
    setup_logging()
    with _lock:
        return json.loads(json.dumps(_effective))


def is_configured() -> bool:
    return _configured
