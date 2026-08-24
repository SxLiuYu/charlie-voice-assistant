"""Always-listening audio queue (inspired by gitee assistant-x-openclaw)

Provides a circular audio buffer that accepts microphone input even during
TTS playback. This enables:
1. Wake word detection during idle (continuous listening)
2. Wake word detection during TTS playback (interruption)
3. Pre-wake audio capture for speaker verification (optional)

Thread-safe, with configurable buffer size and wake-word callback.
"""
import os, time, threading, logging, queue
from typing import Optional, Callable, Tuple

log = logging.getLogger("magic")

# 配置
_BUFFER_MAX_SECONDS = int(os.getenv("WAKE_BUFFER_MAX_SECONDS", "3"))  # 保留最近 3 秒音频
_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16bit
_CHUNK_FRAMES = 480  # 30ms @ 16kHz (匹配 gitee 的 _CHUNK_480)


class AudioQueue:
    """线程安全的环形音频缓冲区，支持 always-listening 和 wake-during-TTS"""

    def __init__(self, max_seconds: int = _BUFFER_MAX_SECONDS):
        self.max_seconds = max_seconds
        self.max_bytes = max_seconds * _SAMPLE_RATE * _SAMPLE_WIDTH
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._wake_callback: Optional[Callable[[bytes], None]] = None
        self._enabled = True

    def set_wake_callback(self, callback: Callable[[bytes], None]) -> None:
        """设置唤醒词检测到后的回调函数（接收唤醒前 3 秒音频）"""
        self._wake_callback = callback

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def is_enabled(self) -> bool:
        return self._enabled

    def push(self, audio_chunk: bytes) -> None:
        """压入一段原始音频（16kHz mono 16bit PCM）

        始终执行，即使 TTS 播放中也不阻塞。
        如果缓冲区超过最大长度，丢弃最老的音频。
        """
        if not self._enabled or not audio_chunk:
            return
        with self._lock:
            self._buffer.extend(audio_chunk)
            # 环形裁剪
            overflow = len(self._buffer) - self.max_bytes
            if overflow > 0:
                self._buffer = self._buffer[overflow:]

    def pop_recent(self, seconds: float = _BUFFER_MAX_SECONDS) -> bytes:
        """弹出最近 N 秒的音频（用于唤醒前音频捕获）"""
        with self._lock:
            nbytes = int(seconds * _SAMPLE_RATE * _SAMPLE_WIDTH)
            chunk = bytes(self._buffer[-nbytes:]) if nbytes > 0 else b""
            return chunk

    def clear(self) -> None:
        """清空缓冲区"""
        with self._lock:
            self._buffer.clear()

    def trigger_wake(self) -> bytes:
        """触发唤醒：返回最近音频并调用回调"""
        recent = self.pop_recent()
        if self._wake_callback:
            try:
                self._wake_callback(recent)
            except Exception as e:
                log.warning(f"[audio_queue] wake_callback error: {e}")
        return recent


# 全局单例
_audio_queue = AudioQueue()


def get_audio_queue() -> AudioQueue:
    """获取全局音频队列单例"""
    return _audio_queue


def start_audio_queue_listener() -> None:
    """启动音频队列监听器（用于本地唤醒词检测）

    这是一个轻量封装，实际音频输入由 local_wake.py 的 sounddevice 回调提供。
    这里只暴露队列接口，供外部音频源 push 数据。
    """
    _audio_queue.enable()
    log.info("[audio_queue] listener ready (circular buffer=%ds)", _BUFFER_MAX_SECONDS)


def stop_audio_queue_listener() -> None:
    """停止音频队列监听器"""
    _audio_queue.disable()
    _audio_queue.clear()
    log.info("[audio_queue] listener stopped")
