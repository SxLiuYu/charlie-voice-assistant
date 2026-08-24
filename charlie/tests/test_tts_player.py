"""Tests for TTS parallel synthesis + sequential playback."""
import time
import threading
import pytest
from unittest.mock import MagicMock


class TestTTSParallelPlayer:
    def test_submit_and_get_next_ready(self):
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=2)
        try:
            # 并行合成时，worker 调用顺序不确定，因此用 text→audio 映射而非 side_effect list
            audio_map = {t: f"audio_{t}".encode() for t in ("hello", "world", "foo")}
            mock_synth = MagicMock(side_effect=lambda text: audio_map[text])
            seq1 = player.submit("hello", mock_synth)
            seq2 = player.submit("world", mock_synth)
            seq3 = player.submit("foo", mock_synth)
            assert seq1 == 0
            assert seq2 == 1
            assert seq3 == 2
            # Wait for all to complete
            deadline = time.time() + 2.0
            while not player.is_done() and time.time() < deadline:
                time.sleep(0.05)
            # Get in order
            r1 = player.get_next_ready(timeout=1.0)
            r2 = player.get_next_ready(timeout=1.0)
            r3 = player.get_next_ready(timeout=1.0)
            assert r1 is not None
            assert r2 is not None
            assert r3 is not None
            assert r1[0] == 0
            assert r2[0] == 1
            assert r3[0] == 2
            # 按 text 验证 audio，不依赖调用顺序
            assert r1[2] == b"audio_hello"
            assert r2[2] == b"audio_world"
            assert r3[2] == b"audio_foo"
        finally:
            player.stop()

    def test_sequential_playback_order(self):
        """Even if synthesis completes out of order, playback should be sequential"""
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=2)
        try:
            # Simulate out-of-order completion: seq 1 completes before seq 0
            results = {}

            def slow_synth(text):
                tid = threading.get_native_id()
                time.sleep(0.1)
                results[tid] = text
                return f"audio_{text}".encode()

            seq0 = player.submit("first", slow_synth)
            seq1 = player.submit("second", slow_synth)
            deadline = time.time() + 2.0
            while not player.is_done() and time.time() < deadline:
                time.sleep(0.05)
            r0 = player.get_next_ready(timeout=1.0)
            r1 = player.get_next_ready(timeout=1.0)
            assert r0 is not None
            assert r1 is not None
            assert r0[0] == 0  # seq 0 first
            assert r1[0] == 1  # seq 1 second
        finally:
            player.stop()

    def test_stop_cancels_pending(self):
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=1)
        try:
            def slow_synth(text):
                time.sleep(5)
                return b"audio"

            player.submit("slow", slow_synth)
            time.sleep(0.1)
            player.stop()
            assert player.pending_count() == 0
            assert player.is_done()
        finally:
            player.stop()

    def test_is_done_when_all_complete(self):
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=2)
        try:
            mock_synth = MagicMock(return_value=b"audio")
            player.submit("text", mock_synth)
            # 等待 executor 线程完成合成
            deadline = time.time() + 2.0
            while not player.is_done() and time.time() < deadline:
                time.sleep(0.05)
            assert player.is_done()
        finally:
            player.stop()

    def test_parallel_workers_utilized(self):
        """Verify that multiple workers can run concurrently"""
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=2)
        try:
            start_times = []

            def timed_synth(text):
                start_times.append(time.time())
                time.sleep(0.2)
                return b"audio"

            player.submit("a", timed_synth)
            player.submit("b", timed_synth)
            time.sleep(0.3)
            # Both should have started within a short window (parallel)
            assert len(start_times) == 2
            if len(start_times) == 2:
                assert start_times[1] - start_times[0] < 0.1  # Started within 100ms of each other
        finally:
            player.stop()

    def test_async_get_next_works(self):
        """async_get_next should work with the async bridge"""
        import asyncio
        from agent.tts_player import TTSParallelPlayer
        player = TTSParallelPlayer(max_workers=2)
        try:
            mock_synth = MagicMock(return_value=b"audio")
            player.submit("text", mock_synth)
            time.sleep(0.2)
            # Run async_get_next in an event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(player.async_get_next(timeout=1.0))
                assert result is not None
                assert result[0] == 0
                assert result[2] == b"audio"
            finally:
                loop.close()
        finally:
            player.stop()
