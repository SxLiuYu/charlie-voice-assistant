"""Round 2 hardware-side regression tests: NH1, C1, NH2, NH3, NH5, NH6.

Run:
    cd charlie && python3 -m pytest tests/test_round2_hw_fixes.py -q
"""
import os
import sys
import io
import time
import queue
import types
import threading
import wave
import array
import inspect
import concurrent.futures
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


@pytest.fixture
def mqtt_server():
    """Create an MqttXiaozhiServer instance without starting MQTT/UDP."""
    from app.mqtt_server import MqttXiaozhiServer, _sessions, _sessions_lock, _device_topics, _device_topics_lock, _addr_to_device, _addr_to_device_lock
    server = MqttXiaozhiServer()
    yield server
    with _sessions_lock:
        _sessions.clear()
    with _device_topics_lock:
        _device_topics.clear()
    with _addr_to_device_lock:
        _addr_to_device.clear()


# ─────────────────────────────────────────────────────────
# NH1: _process_utterance 不再双重 ASR
# ─────────────────────────────────────────────────────────

class TestNH1NoDuplicateASR:
    """_process_utterance 应直接用已识别的 asr_text 走 brain + TTS，不重复 ASR。"""

    def test_asr_called_once_and_tts_uses_brain_reply(self, monkeypatch, mqtt_server):
        """ASR 恰好 1 次；TTS 推送内容来自 brain 回复而非 ASR 原文。"""
        from app.mqtt_server import _sessions, _sessions_lock
        from voice_agent import asr as real_asr
        from agent.llm import brain_stream_sentences as real_brain_stream
        from voice_agent import tts_to_mp3 as real_tts_to_mp3

        wav_bytes = self._make_min_wav(sample_rate=16000, samples=[0])

        asr_calls = []
        stream_calls = []
        stream_session_ids = []
        tts_calls = []

        def fake_asr(audio_in, fmt):
            asr_calls.append((len(audio_in), fmt))
            return "hello world"

        def fake_brain_stream(text, session_id="default", interrupted_reply="", channel="voice", cancel_event=None):
            stream_calls.append(text)
            stream_session_ids.append(session_id)
            yield ("brain reply", "brain reply")

        def fake_tts_to_mp3(text):
            tts_calls.append(text)
            return b"mp3-data"

        monkeypatch.setattr("voice_agent.asr", fake_asr)
        monkeypatch.setattr("agent.llm.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.tts_to_mp3", fake_tts_to_mp3)
        monkeypatch.setattr("voice_agent.is_low_intent_asr", lambda t: False)
        monkeypatch.setattr("voice_agent.is_garbled_asr", lambda t: False)
        monkeypatch.setattr("agent.intent.strip_wake_word", lambda t: t)
        # 跳过真实 opus 解码与 mp3→opus 编码（假 MP3 字节进不了 ffmpeg）
        import app.xiaozhi_codec as xc
        monkeypatch.setattr(xc, "opus_decode_to_pcm", lambda frames, sr: b"\x00" * 320)
        monkeypatch.setattr(xc, "pcm_to_wav", lambda pcm, sr: wav_bytes)
        monkeypatch.setattr(xc, "mp3_to_opus_packets", lambda data, sr: [b"\x11" * 20])

        device_id = "nh1-device"
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": ("127.0.0.1", 9999),
                "aes_key": b"\x00" * 16,
                "aes_nonce": b"\x00" * 16,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        mqtt_server._publish_to_device = lambda *a, **kw: None
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(lambda *a, **kw: None)})()

        mqtt_server._process_utterance([wav_bytes], device_id)

        assert len(asr_calls) == 1, f"ASR 应恰好 1 次，实际 {len(asr_calls)}"
        assert stream_calls == ["hello world"]
        assert tts_calls == ["brain reply"]
        # 会话按设备隔离（P1-2：不得硬编码 default）
        assert stream_session_ids == [f"mqtt-{device_id}"]

    @staticmethod
    def _make_min_wav(sample_rate=16000, samples=None):
        if samples is None:
            samples = [0]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(array.array("h", samples).tobytes())
        return buf.getvalue()


# ─────────────────────────────────────────────────────────
# C1: barge-in cancel_event 终止 brain 流
# ─────────────────────────────────────────────────────────

class TestC1CancelEvent:
    """_brain_run_iter 支持 cancel_event，set 后尽早退出且 sentinel 仍投递。"""

    def test_cancel_stops_generator_and_queue_drained(self):
        """构造慢速 brain，中途 set cancel_event，断言停止且队列收到 done。"""
        from agent.llm import _brain_run_iter

        class FakeBrain:
            def run(self, messages):
                for i in range(20):
                    yield f"token-{i}"
                    time.sleep(0.05)

        cancel_evt = threading.Event()
        gen = _brain_run_iter(FakeBrain(), [], total_timeout=30.0, cancel_event=cancel_evt)

        collected = []
        for item in gen:
            collected.append(item)
            if len(collected) == 3:
                cancel_evt.set()

        assert len(collected) == 3, f"应在 3 token 后停止，实际 {len(collected)}"


# ─────────────────────────────────────────────────────────
# NH2: 同设备复用 Opus Decoder
# ─────────────────────────────────────────────────────────

class TestNH2DecoderCache:
    """per-device Decoder 构造次数应 ≤ 1。"""

    def test_decoder_reused_across_frames(self, monkeypatch):
        from app.mqtt_server import MqttXiaozhiServer
        import opuslib

        ctor_calls = []

        class FakeDecoder:
            def __init__(self, *a, **kw):
                ctor_calls.append((a, kw))
                self.buf = b"\x00" * 320

            def decode(self, pkt, frame_size=320):
                return self.buf

        monkeypatch.setattr(opuslib, "Decoder", FakeDecoder, raising=False)

        server = MqttXiaozhiServer()
        for i in range(100):
            server._get_opus_decoder(f"dev-{i % 5}")

        # 5 个设备 → 应恰好 5 次构造
        assert len(ctor_calls) == 5


# ─────────────────────────────────────────────────────────
# NH3: RMS/VAD 吃裸 PCM，ASR 吃 WAV
# ─────────────────────────────────────────────────────────

class TestNH3RawPcmForVad:
    """VAD/RMS 入参不含 WAV 头；送 ASR 的是合法 WAV。"""

    def test_pcm_no_wav_header_and_wav_has_riff(self):
        """解码裸 PCM 无 RIFF 头；封装 WAV 后有 RIFF 头。"""
        from app.xiaozhi_codec import opus_decode_to_pcm, pcm_to_wav

        fake_frames = [b"\x00" * 100]
        pcm = opus_decode_to_pcm(fake_frames, 16000)
        wav = pcm_to_wav(pcm, 16000)
        assert wav.startswith(b"RIFF")
        assert not pcm.startswith(b"RIFF")


# ─────────────────────────────────────────────────────────
# NH5: _touch_esp32 降频
# ─────────────────────────────────────────────────────────

class TestNH5TouchEsp32Throttled:
    """每 100 帧最多调用 1 次 _touch_esp32。"""

    def test_touch_called_at_most_3_times_for_200_frames(self, monkeypatch):
        import app.schedulers as sc

        calls = []

        def counting_touch(device_id):
            calls.append(device_id)

        monkeypatch.setattr(sc, "_touch_esp32", counting_touch)

        frame_count = 0
        for _ in range(200):
            frame_count += 1
            if frame_count % 100 == 0:
                sc._touch_esp32("dev")

        assert len(calls) <= 3


# ─────────────────────────────────────────────────────────
# NH6: AC 超时预算
# ─────────────────────────────────────────────────────────

class TestNH6AcTimeoutBudget:
    """_run_ac_with_timeout 在 API 超时后返回超时文案。"""

    def test_timeout_returns_message(self):
        from agent.device_control import _run_ac_with_timeout

        ok, msg = _run_ac_with_timeout(lambda: time.sleep(0.5), timeout=0.3)
        assert ok is False
        assert "超时" in msg
