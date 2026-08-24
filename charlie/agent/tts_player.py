"""TTS parallel synthesis + sequential playback (inspired by gitee assistant-x-openclaw)

Improves first-audio latency by:
1. Parallel synthesis: multiple TTS workers synthesize sentences concurrently
2. Sequential playback: monotonically increasing seq counter guarantees ordered output
3. Efficient wait/notify: Condition variable instead of busy polling
4. Async-friendly: asyncio bridge for integration with async streaming paths

This replaces the simple thread-per-sentence pattern in _stream_brain_tts.
"""
import os, time, threading, logging, queue, asyncio
from typing import Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger("magic")

# 配置
_MAX_WORKERS = int(os.getenv("TTS_MAX_WORKERS", "2"))  # 并行 TTS 工作者数
_QUEUE_MAX_SIZE = int(os.getenv("TTS_QUEUE_MAX_SIZE", "20"))


class TTSParallelPlayer:
    """TTS 并行合成 + 顺序播放

    使用 ThreadPoolExecutor 并行合成多个句子，
    使用 monotonically increasing seq counter 保证播放顺序。
    """

    def __init__(self, max_workers: int = _MAX_WORKERS):
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tts_worker")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0  # monotonically increasing sequence counter
        self._results: dict[int, tuple[str, Optional[bytes], Optional[dict]]] = {}  # seq -> (text, audio, warning)
        self._pending: set[int] = set()  # seq numbers being synthesized
        self._completed_count = 0  # total completed synthesis count
        self._completed_up_to = -1  # highest seq that has been played (starts at -1 so first seq is 0)
        self._stopped = False
        # Async bridge
        self._async_queue: Optional[asyncio.Queue] = None
        self._async_bridge_thread: Optional[threading.Thread] = None

    def _init_async_bridge(self) -> None:
        """Initialize async bridge (called from async context)"""
        if self._async_queue is None:
            self._async_queue = asyncio.Queue()
            self._async_bridge_thread = threading.Thread(target=self._async_bridge_loop, daemon=True)
            self._async_bridge_thread.start()

    def _async_bridge_loop(self) -> None:
        """Background thread that pumps results to async queue"""
        while not self._stopped:
            result = self.get_next_ready(timeout=0.05)
            if result is None:
                continue
            if self._async_queue is not None:
                try:
                    self._async_queue.put_nowait(result)
                except Exception:
                    pass

    async def async_get_next(self, timeout: float = 1.0) -> Optional[tuple[int, str, Optional[bytes], Optional[dict]]]:
        """Async version of get_next_ready - uses asyncio.Queue for event-driven consumption"""
        if self._async_queue is None:
            self._init_async_bridge()
        try:
            return await asyncio.wait_for(self._async_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def submit(self, text: str, synth_fn: Callable[[str], bytes]) -> int:
        """提交一个句子进行并行合成，返回 seq number"""
        if self._stopped:
            return -1
        with self._lock:
            seq = self._seq
            self._seq += 1
            self._pending.add(seq)
        future = self._executor.submit(self._synth_and_store, seq, text, synth_fn)
        return seq

    def _synth_and_store(self, seq: int, text: str, synth_fn: Callable[[str], bytes]) -> None:
        """在 worker 线程中执行合成，存储结果并通知"""
        try:
            audio = synth_fn(text)
            with self._lock:
                self._results[seq] = (text, audio, None)
                self._pending.discard(seq)
                self._completed_count += 1
                self._cond.notify_all()
        except Exception as e:
            with self._lock:
                self._results[seq] = (text, None, {"type": "error", "message": str(e)[:120]})
                self._pending.discard(seq)
                self._completed_count += 1
                self._cond.notify_all()

    def get_next_ready(self, timeout: float = 0.1) -> Optional[tuple[int, str, Optional[bytes], Optional[dict]]]:
        """获取下一个可以播放的结果（seq 顺序）

        如果下一个 seq 还没完成，等待 timeout 秒。
        返回 (seq, text, audio, warning) 或 None
        """
        deadline = time.time() + timeout
        with self._lock:
            while not self._stopped:
                next_seq = self._completed_up_to + 1
                if next_seq in self._results:
                    result = self._results.pop(next_seq)
                    self._completed_up_to = next_seq
                    return (next_seq, result[0], result[1], result[2])
                if self._pending and time.time() < deadline:
                    self._cond.wait(deadline - time.time())
                    continue
                if not self._pending and self._completed_up_to + 1 >= self._seq:
                    # All done
                    return None
                if time.time() >= deadline:
                    return None
        return None

    def stop(self) -> None:
        """停止播放器，取消未完成的合成"""
        self._stopped = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self._pending.clear()
            self._results.clear()
            # 将 seq 前进到 completed_up_to + 1，这样 is_done() 会返回 True
            self._seq = self._completed_up_to + 1
            self._cond.notify_all()
        # Drain async queue and stop bridge thread
        if self._async_bridge_thread is not None and self._async_bridge_thread.is_alive():
            self._async_bridge_thread.join(timeout=1.0)
        if self._async_queue is not None:
            while not self._async_queue.empty():
                try:
                    self._async_queue.get_nowait()
                except Exception:
                    break

    def is_done(self) -> bool:
        """检查是否所有任务都已完成合成（不管是否已播放）"""
        with self._lock:
            return self._completed_count >= self._seq and not self._pending

    def pending_count(self) -> int:
        """获取待合成数量"""
        with self._lock:
            return len(self._pending)
