"""Round 10 tests: edge-tts free fallback + StepFun short-audio guard."""
import asyncio
import os
import sys
import struct
import time
from unittest.mock import patch, MagicMock

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# ======================================================================
# Helpers
# ======================================================================

def _make_wav_bytes(duration_s: float) -> bytes:
    """Build a minimal 16kHz mono 16-bit PCM WAV in memory."""
    sample_rate = 16000
    num_samples = int(sample_rate * duration_s)
    data = b"\x00\x00" * num_samples  # silence
    header = b"RIFF"
    header += struct.pack("<I", 36 + len(data))
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 1, 16)
    header += b"data"
    header += struct.pack("<I", len(data))
    return header + data


# ======================================================================
# 改造2: edge-tts 免费降级
# ======================================================================

class TestTTSEdgeProvider:
    """_tts_edge 合成与异常处理"""

    def test_tts_edge_returns_mp3_bytes(self):
        """_tts_edge 成功时返回非空 bytes（mock edge_tts）"""
        from agent import asr_tts

        mp3_bytes = b"ID3\x04\x00\x00\x00\x00\x00\x01" + b"\x00" * 100

        class _FakeStream:
            def __init__(self, data):
                self._data = data
                self._iter = iter([{"type": "audio", "data": data}])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        class _FakeCommunicate:
            def __init__(self, text, voice):
                pass

            def stream(self):
                return _FakeStream(mp3_bytes)

        fake_edge = MagicMock()
        fake_edge.Communicate = _FakeCommunicate

        with patch.dict(sys.modules, {"edge_tts": fake_edge}):
            result = asr_tts._tts_edge("你好世界")

        assert isinstance(result, bytes)
        assert len(result) > 0
        assert result == mp3_bytes

    def test_tts_edge_raises_runtime_error_on_exception(self):
        """edge_tts 合成异常时 _tts_edge 抛 RuntimeError，由 tts() 主链正常降级"""
        from agent import asr_tts
        from agent.asr_tts import TTSUnavailableError

        class _FakeCommunicate:
            def __init__(self, text, voice):
                pass

            def stream(self):
                raise RuntimeError("edge network error")

        fake_edge = MagicMock()
        fake_edge.Communicate = _FakeCommunicate

        with patch.dict(sys.modules, {"edge_tts": fake_edge}):
            with pytest.raises(RuntimeError, match="edge network error"):
                asr_tts._tts_edge("你好")

        # tts() 主链应将 RuntimeError 视为普通失败，尝试下一个 provider
        fake_fn = MagicMock(return_value=b"ok-finna")
        with patch.dict(asr_tts._TTS_PROVIDERS, {
            "baidu": MagicMock(side_effect=RuntimeError("bd down")),
            "edge": MagicMock(side_effect=RuntimeError("edge down")),
            "stepfun": MagicMock(side_effect=RuntimeError("sf down")),
            "finna": fake_fn,
        }):
            result = asr_tts.tts("你好")
        assert result == b"ok-finna"

    def test_tts_edge_not_installed_raises_runtime_error(self):
        """edge-tts 包未安装时 _tts_edge 抛 RuntimeError"""
        from agent import asr_tts

        # 移除 edge_tts 模块
        original = sys.modules.get("edge_tts")
        if "edge_tts" in sys.modules:
            del sys.modules["edge_tts"]

        # 构造一个假的 import 行为
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "edge_tts":
                raise ImportError("No module named 'edge_tts'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(RuntimeError, match="edge-tts 未安装"):
                asr_tts._tts_edge("你好")

        if original is not None:
            sys.modules["edge_tts"] = original

    def test_tts_edge_registered_in_providers(self):
        """_TTS_PROVIDERS 含 edge 键"""
        from agent.asr_tts import _TTS_PROVIDERS
        assert "edge" in _TTS_PROVIDERS
        assert callable(_TTS_PROVIDERS["edge"])

    def test_tts_edge_timeout_raises_runtime_error(self):
        """edge-tts 合成超时时 _tts_edge 抛 RuntimeError"""
        from agent import asr_tts

        async def _slow_stream():
            await asyncio.sleep(20)
            yield {"type": "audio", "data": b""}

        class _FakeCommunicate:
            def __init__(self, text, voice):
                pass

            def stream(self):
                return _slow_stream()

        fake_edge = MagicMock()
        fake_edge.Communicate = _FakeCommunicate

        with patch.dict(sys.modules, {"edge_tts": fake_edge}):
            with pytest.raises(RuntimeError, match="超时"):
                asr_tts._tts_edge("你好")


# ======================================================================
# 改造3: StepFun ASR 短音频守卫
# ======================================================================

class TestStepfunShortAudioGuard:
    """<0.7s 短音频快速失败，不发起网络请求"""

    def test_short_audio_raises_without_network_call(self):
        """0.3s 静音 wav → 抛 RuntimeError，_session.post 从未被调用"""
        from agent import asr_tts

        short_wav = _make_wav_bytes(0.3)  # 0.3s < 0.7s 阈值

        call_count = 0

        def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise AssertionError("_session.post 不应被短音频触发")

        with patch.dict(os.environ, {"STEPFUN_KEY": "test-key"}):
            with patch("voice_agent._session.post", side_effect=fake_post):
                with pytest.raises(RuntimeError, match="音频过短"):
                    asr_tts._asr_stepfun(short_wav, fmt="wav")

        assert call_count == 0, f"短音频不应触发网络请求，实际调用 {call_count} 次"

    def test_normal_audio_goes_through_network(self):
        """1.0s wav → 通过守卫，正常发网络请求"""
        from agent import asr_tts

        normal_wav = _make_wav_bytes(1.0)  # 1.0s > 0.7s 阈值

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "测试识别结果"}

        with patch.dict(os.environ, {"STEPFUN_KEY": "test-key"}):
            with patch("voice_agent._session.post", return_value=mock_resp) as mock_post:
                result = asr_tts._asr_stepfun(normal_wav, fmt="wav")

        assert result == "测试识别结果"
        assert mock_post.call_count == 1

    def test_boundary_0_7s_does_not_trigger_guard(self):
        """0.7s wav（边界值）→ 应通过守卫发网络请求"""
        from agent import asr_tts

        boundary_wav = _make_wav_bytes(0.7)  # 0.7s == 阈值

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "边界"}

        with patch.dict(os.environ, {"STEPFUN_KEY": "test-key"}):
            with patch("voice_agent._session.post", return_value=mock_resp) as mock_post:
                result = asr_tts._asr_stepfun(boundary_wav, fmt="wav")

        assert result == "边界"
        assert mock_post.call_count == 1

    def test_non_wav_short_audio_also_guarded(self):
        """短 mp3 先 ffmpeg 转 wav，转出后仍被短音频守卫拦截"""
        from agent import asr_tts

        # 构造短 mp3 (任意短字节)
        short_mp3 = b"\xff\xfb\x90\x00" * 10
        # ffmpeg 将返回小 wav
        converted_wav = _make_wav_bytes(0.2)  # 转出后 0.2s

        # subprocess.run 直接返回这个 mock；属性设在其自身上，而非 return_value
        mock_run = MagicMock()
        mock_run.stdout = converted_wav
        mock_run.returncode = 0

        with patch.dict(os.environ, {"STEPFUN_KEY": "test-key"}):
            with patch("subprocess.run", return_value=mock_run) as mock_sub:
                with patch("voice_agent._session.post") as mock_post:
                    with pytest.raises(RuntimeError, match="音频过短"):
                        asr_tts._asr_stepfun(short_mp3, fmt="mp3")

        assert mock_post.call_count == 0
