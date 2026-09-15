"""fcntl 兼容垫片(Windows / 非 Unix 平台).

Unix 平台正常 ``import fcntl``;Windows 无此模块,这里提供同名的
``flock``/``LOCK_SH``/``LOCK_EX``/``LOCK_UN``/``LOCK_NB`` 符号,
使文件锁代码可在 Windows 运行。

语义:Windows 下文件锁退化为 no-op(不阻塞、不报错)。调用方的
非阻塞获取已用 try/except 包裹 OSError/BlockingIOError,不会崩溃。
"""
import os
import logging

log = logging.getLogger("magic")

LOCK_SH = 1
LOCK_EX = 2
LOCK_UN = 8
LOCK_NB = 4


def flock(file, op):  # 兼容 fcntl.flock 签名
    """Windows 下 no-op,仅 flush 文件缓冲避免未写盘。"""
    try:
        file.flush()
    except Exception as e:
        log.debug(f"[fcntl_compat] error at line 22: {e}")
        pass
    return 0
