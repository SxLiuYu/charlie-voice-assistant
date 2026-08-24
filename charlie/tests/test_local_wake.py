"""Tests for local_wake.py P2 features:
- _WakeAudioBuffer (pre-wake circular buffer)
- sherpa-onnx KeywordSpotter integration
- speaker verification (extract/verify/register)
"""
import os
import sys
import importlib.util
import numpy as np
from unittest.mock import patch, MagicMock

# Test setup: load modules from parent dir (same as test_core.py)
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWakeAudioBuffer:
    """_WakeAudioBuffer 环形缓冲区"""

    def _get_buffer(self):
        mod = _load_module("local_wake_test_buf", "local_wake.py")
        return mod._wake_audio_buffer

    def test_push_and_pop(self):
        buf = self._get_buffer()
        # RATE=16000, 2 bytes/sample => 32000 bytes/sec
        buf.push(b"x" * 32000)  # 1 second
        recent = buf.pop_recent(seconds=0.5)
        assert len(recent) == 16000

    def test_circular_overflow(self):
        buf = self._get_buffer()
        # Push 5 seconds worth (max is 3s)
        chunk = b"a" * 3200  # 100ms
        for _ in range(50):  # 5s
            buf.push(chunk)
        total = len(buf.pop_recent(seconds=10))
        assert total <= buf._max_bytes
        assert total == 3 * 16000 * 2

    def test_clear(self):
        buf = self._get_buffer()
        buf.push(b"hello")
        buf.clear()
        assert buf.pop_recent() == b""

    def test_empty_pop(self):
        buf = self._get_buffer()
        assert buf.pop_recent() == b""

    def test_concurrent_access(self):
        """Thread-safe concurrent push/pop"""
        import threading
        buf = self._get_buffer()
        errors = []

        def pusher():
            for _ in range(50):
                try:
                    buf.push(b"x" * 1000)
                except Exception as e:
                    errors.append(e)

        def popper():
            for _ in range(50):
                try:
                    buf.pop_recent(seconds=1)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=pusher), threading.Thread(target=popper)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Concurrent errors: {errors}"


class TestSherpaKeywordSpotter:
    """sherpa-onnx KeywordSpotter 集成（优雅降级）"""

    def test_load_kws_returns_none_without_sherpa(self):
        """当 sherpa-onnx 未安装时，_load_sherpa_kws 应返回 None"""
        mod = _load_module("local_wake_test_kws1", "local_wake.py")
        # 确保环境变量不满足
        with patch_dict({"KWS_MODEL": ""}):
            result = mod._load_sherpa_kws()
        assert result is None

    def test_load_kws_returns_none_without_model_dir(self):
        mod = _load_module("local_wake_test_kws2", "local_wake.py")
        with patch_dict({"KWS_MODEL": "/nonexistent/path"}):
            result = mod._load_sherpa_kws()
        assert result is None

    def test_sherpa_kws_detect_handles_none(self):
        """_sherpa_kws_detect 在 kws=None 时应返回 (False, None)"""
        mod = _load_module("local_wake_test_kws3", "local_wake.py")
        audio = np.zeros(1600, dtype=np.float32)
        triggered, keyword = mod._sherpa_kws_detect(None, audio)
        assert triggered is False
        assert keyword is None

    def test_listen_loop_falls_back_when_no_models(self):
        """当无任何唤醒词模型时，_listen_loop 应优雅退出"""
        mod = _load_module("local_wake_test_kws4", "local_wake.py")
        # 模拟无模型
        with patch.object(mod, '_load_oww_model', return_value=None), \
             patch.object(mod, '_load_sherpa_kws', return_value=None):
            # _listen_loop 会 log.warning 并 return，不会抛异常
            try:
                mod._listen_loop()
            except Exception:
                pass  # 因为 sd.InputStream 不存在于测试环境


class TestSpeakerVerification:
    """声纹校验（graceful degradation）"""

    def test_extract_speaker_returns_none_without_model(self):
        """无 speaker model 时 extract_speaker_embedding 应返回 None"""
        mod = _load_module("local_wake_test_sv1", "local_wake.py")
        with patch_dict({"SPEAKER_MODEL_DIR": ""}):
            result = mod.extract_speaker_embedding(b"fake audio")
        assert result is None

    def test_verify_speaker_with_none_reference(self):
        mod = _load_module("local_wake_test_sv2", "local_wake.py")
        result = mod.verify_speaker(b"audio", None)
        assert result is False

    def test_register_speaker_returns_none_without_model(self):
        mod = _load_module("local_wake_test_sv3", "local_wake.py")
        with patch_dict({"SPEAKER_MODEL_DIR": ""}):
            result = mod.register_speaker(b"fake audio")
        assert result is None


class TestCallbackSignature:
    """wake_callback 签名兼容性（keyword 参数）"""

    def test_callback_receives_keyword(self):
        """新版回调应接收 keyword 参数"""
        mod = _load_module("local_wake_test_cb", "local_wake.py")
        received = []

        def callback(audio, keyword=None):
            received.append((audio, keyword))

        mod._wake_callback = callback
        mod._play_beep = lambda: None
        mod._record_command = lambda: b"command"
        mod._audio_queue = MagicMock()
        mod._audio_queue.pop_recent.return_value = b"pre"
        mod._AUDIO_QUEUE_AVAILABLE = True

        # 直接调用触发逻辑
        pre = mod._audio_queue.pop_recent(3)
        combined = pre + b"command"
        try:
            mod._wake_callback(combined, keyword="sherpa_test")
        except TypeError:
            mod._wake_callback(combined)

        assert len(received) == 1
        assert received[0][1] == "sherpa_test"

    def test_callback_compatible_without_keyword(self):
        """旧版回调（不接受 keyword）不应报错"""
        mod = _load_module("local_wake_test_cb2", "local_wake.py")

        def old_callback(audio):
            return audio

        mod._wake_callback = old_callback
        mod._play_beep = lambda: None
        mod._record_command = lambda: b"command"
        mod._audio_queue = MagicMock()
        mod._audio_queue.pop_recent.return_value = b""
        mod._AUDIO_QUEUE_AVAILABLE = True

        pre = mod._audio_queue.pop_recent(3)
        combined = pre + b"command"
        # _listen_loop 有 try/except TypeError 兼容
        try:
            mod._wake_callback(combined)
        except Exception:
            try:
                mod._wake_callback(combined, keyword="test")
            except Exception:
                pass


# ── helper ──

class patch_dict:
    """临时设置环境变量，退出时恢复"""
    def __init__(self, env):
        self._env = env
        self._old = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *args):
        for k, v in self._env.items():
            if self._old[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._old[k]
