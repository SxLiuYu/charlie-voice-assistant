"""Tests for always-listening audio queue (gitee assistant-x-openclaw pattern)."""
import os
import threading
import time
import pytest
from unittest.mock import patch, MagicMock


class TestAudioQueue:
    def test_push_and_pop_recent(self):
        from agent.audio_queue import AudioQueue
        q = AudioQueue(max_seconds=1)
        # Push some chunks (each 480 frames * 2 bytes = 960 bytes @ 16kHz = 30ms)
        q.push(b"x" * 960)
        q.push(b"y" * 960)
        q.push(b"z" * 960)
        recent = q.pop_recent(seconds=0.03)  # last 30ms
        assert b"z" in recent

    def test_circular_buffer_overflow(self):
        """Buffer should drop oldest data when exceeding max size"""
        from agent.audio_queue import AudioQueue
        q = AudioQueue(max_seconds=1)
        # Fill beyond capacity (1s @ 16kHz 16bit = 32000 bytes)
        chunk = b"a" * 960  # 30ms
        for _ in range(100):  # 3s worth
            q.push(chunk)
        total = len(q.pop_recent(seconds=10))
        assert total <= q.max_bytes

    def test_clear(self):
        from agent.audio_queue import AudioQueue
        q = AudioQueue()
        q.push(b"hello")
        q.clear()
        assert q.pop_recent() == b""

    def test_wake_callback_triggered(self):
        from agent.audio_queue import AudioQueue
        called = []
        q = AudioQueue()
        q.set_wake_callback(lambda audio: called.append(audio))
        q.push(b"preamble")
        result = q.trigger_wake()
        assert len(called) == 1
        assert b"preamble" in called[0]

    def test_disable_prevents_push(self):
        from agent.audio_queue import AudioQueue
        q = AudioQueue()
        q.disable()
        q.push(b"silent")
        assert q.pop_recent() == b""

    def test_enable_after_disable(self):
        from agent.audio_queue import AudioQueue
        q = AudioQueue()
        q.disable()
        q.enable()
        q.push(b"audio")
        assert b"audio" in q.pop_recent()

    def test_concurrent_push_pop(self):
        """Thread-safe concurrent access"""
        from agent.audio_queue import AudioQueue
        q = AudioQueue()
        errors = []

        def pusher():
            for _ in range(100):
                try:
                    q.push(b"x" * 100)
                except Exception as e:
                    errors.append(e)

        def popper():
            for _ in range(100):
                try:
                    q.pop_recent()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=pusher), threading.Thread(target=popper)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Concurrent errors: {errors}"


class TestGlobalAudioQueue:
    def test_get_audio_queue_singleton(self):
        from agent.audio_queue import get_audio_queue, AudioQueue
        q1 = get_audio_queue()
        q2 = get_audio_queue()
        assert q1 is q2
        assert isinstance(q1, AudioQueue)
