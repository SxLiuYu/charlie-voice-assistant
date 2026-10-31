"""fcntl 兼容垫片(Windows / 非 Unix 平台).

Unix 平台正常 ``import fcntl``;Windows 无此模块,这里提供同名的
``flock``/``LOCK_SH``/``LOCK_EX``/``LOCK_UN``/``LOCK_NB`` 符号,
使文件锁代码可在 Windows 运行。

语义（msvcrt.locking 实现，**真锁**，不是 no-op）：

- LOCK_EX | LOCK_NB  → msvcrt.LK_NBLCK，失败抛 BlockingIOError（调用方
  用 try/except BlockingIOError 包裹非阻塞获取）
- LOCK_EX（阻塞）    → msvcrt.LK_LOCK 每 10 秒超时一次，循环重试直到拿到
- LOCK_SH            → Windows 没有共享锁，退化为排他锁（损失读并发，不损正确性）
- LOCK_UN            → 解锁；未持锁时静默忽略

锁区域固定为文件开头 1 字节（文件为空也能锁，区域可超出文件尾）。
同进程对同一区域的重复加锁在 Windows 上是允许的，与 flock 的
open-file-description 语义一致。
"""
import os
import time
import logging
import msvcrt

log = logging.getLogger("magic")

LOCK_SH = 1
LOCK_EX = 2
LOCK_UN = 8
LOCK_NB = 4

_LOCK_BYTES = 1


def flock(file, op):  # 兼容 fcntl.flock 签名
    """Windows 下用 msvcrt.locking 提供跨进程互斥。

    `file` 既接受文件对象也接受裸 fd（int）—— Unix 的 fcntl.flock 两者都收，
    调用方（如 state_sync）传的是 os.open() 的 int。
    """
    if hasattr(file, "fileno"):
        fd = file.fileno()
        try:
            file.flush()
        except Exception as e:
            log.debug(f"[fcntl_compat] flush 失败: {e}")
            pass
        try:
            cur = file.tell()
            file.seek(0)
        except Exception as e:
            log.debug(f"[fcntl_compat] seek 失败: {e}")
            cur = None
    else:
        fd = file
        cur = None

    def _restore():
        if cur is not None:
            try:
                file.seek(cur)
            except Exception:
                pass

    if op & LOCK_UN:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass  # 未持锁/已释放：与 flock 语义一致，静默
        _restore()
        return 0

    blocking = not (op & LOCK_NB)
    msvcrt_mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    while True:
        try:
            msvcrt.locking(fd, msvcrt_mode, _LOCK_BYTES)
            _restore()
            return 0
        except OSError as e:
            if not blocking:
                _restore()
                # 调用方统一 except BlockingIOError 判定"拿不到锁"
                raise BlockingIOError(
                    getattr(os, "EAGAIN", 11), "Resource temporarily unavailable") from e
            # LK_LOCK 每 10 秒超时抛 OSError → 继续等，直到拿到锁
            time.sleep(0.05)
