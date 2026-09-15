"""Round 3 MQTT streaming fixes: R3-B streaming sentences, R3-C monotonic seq.

Run:
    cd charlie && python3 -m pytest tests/test_round3_mqtt_streaming.py -q
"""
import json
import os
import sys
import io
import time
import struct
import threading
import wave


def _wait_flag(flag, want_set, timeout=2.0):
    """轮询等待 flag 达到期望状态（固定 sleep 在满载下会漂移）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if flag.is_set() == want_set:
            return
        time.sleep(0.02)
    # 超时不在此处抛错，由调用方断言真实状态
import array

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from app.mqtt_server import (
    MqttXiaozhiServer,
    _sessions,
    _sessions_lock,
    _device_topics,
    _device_topics_lock,
    _addr_to_device,
    _addr_to_device_lock,
    _build_audio_packet,
    _generate_aes_key_nonce,
    _ABORT_AUTO_CLEAR_DELAY,
)


@pytest.fixture
def mqtt_server():
    server = MqttXiaozhiServer()
    yield server
    with _sessions_lock:
        _sessions.clear()
    with _device_topics_lock:
        _device_topics.clear()
    with _addr_to_device_lock:
        _addr_to_device.clear()


# ─────────────────────────────────────────────────────────
# R3-B: 流式分句
# ─────────────────────────────────────────────────────────

class TestStreamingSentences:
    """_process_utterance 流式分句推送：tts start×1 / sentence_start×N / stop×1。"""

    def test_streaming_two_sentences(self, monkeypatch, mqtt_server):
        from voice_agent import asr as real_asr
        from agent.llm import brain_stream_sentences as real_brain_stream
        from voice_agent import tts_to_mp3 as real_tts_to_mp3
        import app.xiaozhi_codec as xc

        wav_bytes = self._make_min_wav(sample_rate=16000, samples=[0])
        publishes = []
        sendto_calls = []

        def fake_asr(audio_in, fmt):
            return "你好世界"

        def fake_brain_stream(text, session_id="default", interrupted_reply="", channel="voice", cancel_event=None):
            assert session_id == "mqtt-dev-1"
            yield ("第一句。", "第一句。完整回复")
            yield ("第二句。", "第一句。完整回复第二句。")

        def fake_tts_to_mp3(text):
            return b"mp3-" + text.encode()

        def fake_publish(device_id, payload):
            publishes.append(json.loads(payload))

        def fake_sendto(data, addr):
            sendto_calls.append((data, addr))

        monkeypatch.setattr("voice_agent.asr", fake_asr)
        monkeypatch.setattr("agent.llm.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.tts_to_mp3", fake_tts_to_mp3)
        monkeypatch.setattr("voice_agent.is_low_intent_asr", lambda t: False)
        monkeypatch.setattr("voice_agent.is_garbled_asr", lambda t: False)
        monkeypatch.setattr("agent.intent.strip_wake_word", lambda t: t)
        monkeypatch.setattr(xc, "opus_decode_to_pcm", lambda frames, sr: b"\x00" * 320)
        monkeypatch.setattr(xc, "pcm_to_wav", lambda pcm, sr: wav_bytes)
        monkeypatch.setattr(xc, "mp3_to_opus_packets", lambda data, sr: [b"\x11" * 20, b"\x22" * 20])

        device_id = "dev-1"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": ("127.0.0.1", 9999),
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        mqtt_server._publish_to_device = fake_publish
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()

        mqtt_server._process_utterance([wav_bytes], device_id)

        types = [(p.get("type"), p.get("state"), p.get("text", "")) for p in publishes]
        # 整段一次 tts start
        starts = [t for t in types if t[0] == "tts" and t[1] == "start"]
        assert len(starts) == 1, f"expected 1 tts start, got {types}"
        # 每句 sentence_start
        sentence_starts = [t for t in types if t[0] == "tts" and t[1] == "sentence_start"]
        assert len(sentence_starts) == 2, f"expected 2 sentence_start, got {types}"
        assert sentence_starts[0][2] == "第一句。"
        assert sentence_starts[1][2] == "第二句。"
        # 一次 tts stop
        stops = [t for t in types if t[0] == "tts" and t[1] == "stop"]
        assert len(stops) == 1, f"expected 1 tts stop, got {types}"
        # UDP sendto 连续（两句都有 opus 包）
        assert len(sendto_calls) >= 2

    def test_abort_mid_stream_sends_stop(self, monkeypatch, mqtt_server):
        """abort 中途 set → 推送停止但 tts stop 已发。"""
        from voice_agent import asr as real_asr
        from agent.llm import brain_stream_sentences as real_brain_stream
        from voice_agent import tts_to_mp3 as real_tts_to_mp3
        import app.xiaozhi_codec as xc

        wav_bytes = self._make_min_wav(sample_rate=16000, samples=[0])
        publishes = []
        sendto_calls = []

        abort_flag = threading.Event()

        def fake_asr(audio_in, fmt):
            return "你好世界"

        def fake_brain_stream(text, session_id="default", interrupted_reply="", channel="voice", cancel_event=None):
            assert cancel_event is abort_flag
            yield ("第一句。", "第一句。")
            # 模拟第二句到来前 abort
            abort_flag.set()
            yield ("第二句。", "第一句。第二句。")

        def fake_tts_to_mp3(text):
            return b"mp3-" + text.encode()

        def fake_publish(device_id, payload):
            publishes.append(json.loads(payload))

        def fake_sendto(data, addr):
            sendto_calls.append((data, addr))

        monkeypatch.setattr("voice_agent.asr", fake_asr)
        monkeypatch.setattr("agent.llm.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.tts_to_mp3", fake_tts_to_mp3)
        monkeypatch.setattr("voice_agent.is_low_intent_asr", lambda t: False)
        monkeypatch.setattr("voice_agent.is_garbled_asr", lambda t: False)
        monkeypatch.setattr("agent.intent.strip_wake_word", lambda t: t)
        monkeypatch.setattr(xc, "opus_decode_to_pcm", lambda frames, sr: b"\x00" * 320)
        monkeypatch.setattr(xc, "pcm_to_wav", lambda pcm, sr: wav_bytes)
        monkeypatch.setattr(xc, "mp3_to_opus_packets", lambda data, sr: [b"\x11" * 20, b"\x22" * 20])

        device_id = "dev-abort"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": ("127.0.0.1", 9999),
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        mqtt_server._get_abort_flag = lambda did: abort_flag
        mqtt_server._publish_to_device = fake_publish
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()

        mqtt_server._process_utterance([wav_bytes], device_id)

        # 只推送了第一句
        sentence_starts = [p for p in publishes if p.get("type") == "tts" and p.get("state") == "sentence_start"]
        assert len(sentence_starts) == 1
        assert sentence_starts[0]["text"] == "第一句。"
        # 但 tts stop 必须发出
        stops = [p for p in publishes if p.get("type") == "tts" and p.get("state") == "stop"]
        assert len(stops) >= 1

    def test_single_tts_failure_skips_sentence(self, monkeypatch, mqtt_server):
        """单句 TTS 失败跳过该句，后续照常。"""
        from voice_agent import asr as real_asr
        from agent.llm import brain_stream_sentences as real_brain_stream
        from voice_agent import tts_to_mp3 as real_tts_to_mp3
        import app.xiaozhi_codec as xc

        wav_bytes = self._make_min_wav(sample_rate=16000, samples=[0])
        publishes = []
        sendto_calls = []

        def fake_asr(audio_in, fmt):
            return "你好世界"

        def fake_brain_stream(text, session_id="default", interrupted_reply="", channel="voice", cancel_event=None):
            yield ("第一句。", "第一句。第二句。")
            yield ("第二句。", "第一句。第二句。")

        def fake_tts_to_mp3(text):
            if text == "第一句。":
                raise RuntimeError("tts boom")
            return b"mp3-" + text.encode()

        def fake_publish(device_id, payload):
            publishes.append(json.loads(payload))

        def fake_sendto(data, addr):
            sendto_calls.append((data, addr))

        monkeypatch.setattr("voice_agent.asr", fake_asr)
        monkeypatch.setattr("agent.llm.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.tts_to_mp3", fake_tts_to_mp3)
        monkeypatch.setattr("voice_agent.is_low_intent_asr", lambda t: False)
        monkeypatch.setattr("voice_agent.is_garbled_asr", lambda t: False)
        monkeypatch.setattr("agent.intent.strip_wake_word", lambda t: t)
        monkeypatch.setattr(xc, "opus_decode_to_pcm", lambda frames, sr: b"\x00" * 320)
        monkeypatch.setattr(xc, "pcm_to_wav", lambda pcm, sr: wav_bytes)
        monkeypatch.setattr(xc, "mp3_to_opus_packets", lambda data, sr: [b"\x11" * 20, b"\x22" * 20])

        device_id = "dev-skip"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": ("127.0.0.1", 9999),
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        mqtt_server._publish_to_device = fake_publish
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()

        mqtt_server._process_utterance([wav_bytes], device_id)

        sentence_starts = [p for p in publishes if p.get("type") == "tts" and p.get("state") == "sentence_start"]
        assert len(sentence_starts) == 1
        assert sentence_starts[0]["text"] == "第二句。"

    def test_no_sentences_pushed_uses_fallback(self, monkeypatch, mqtt_server):
        """brain 流式异常且一句未推过 → 语音处理失败兜底。"""
        from voice_agent import asr as real_asr
        from voice_agent import tts_to_mp3 as real_tts_to_mp3
        import app.xiaozhi_codec as xc

        wav_bytes = self._make_min_wav(sample_rate=16000, samples=[0])
        publishes = []

        def fake_asr(audio_in, fmt):
            return "你好世界"

        def fake_brain_stream(text, session_id="default", interrupted_reply="", channel="voice", cancel_event=None):
            raise RuntimeError("brain boom")

        def fake_tts_to_mp3(text):
            return b"mp3"

        def fake_publish(device_id, payload):
            publishes.append(json.loads(payload))

        monkeypatch.setattr("voice_agent.asr", fake_asr)
        monkeypatch.setattr("agent.llm.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.brain_stream_sentences", fake_brain_stream)
        monkeypatch.setattr("voice_agent.tts_to_mp3", fake_tts_to_mp3)
        monkeypatch.setattr("voice_agent.is_low_intent_asr", lambda t: False)
        monkeypatch.setattr("voice_agent.is_garbled_asr", lambda t: False)
        monkeypatch.setattr("agent.intent.strip_wake_word", lambda t: t)
        monkeypatch.setattr(xc, "opus_decode_to_pcm", lambda frames, sr: b"\x00" * 320)
        monkeypatch.setattr(xc, "pcm_to_wav", lambda pcm, sr: wav_bytes)
        monkeypatch.setattr(xc, "mp3_to_opus_packets", lambda data, sr: [b"\x11" * 20, b"\x22" * 20])

        device_id = "dev-fallback"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": ("127.0.0.1", 9999),
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        mqtt_server._publish_to_device = fake_publish
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(lambda *a, **kw: None)})()

        mqtt_server._process_utterance([wav_bytes], device_id)

        # 兜底播报（经 _push_text_tts → tts start 携带文本）必须出现
        fallback_texts = [p.get("text", "") for p in publishes]
        assert any("失败" in t for t in fallback_texts), f"expected fallback, got {publishes}"

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
# R3-C: 下行 seq 单调递增 + per-device 锁
# ─────────────────────────────────────────────────────────

class TestMonotonicSeq:
    """下行音频 seq 单调递增且 per-device 隔离。"""

    def test_consecutive_pushes_continuous_seq(self, mqtt_server):
        device_id = "seq-dev"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        addr = ("127.0.0.1", 9999)
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": addr,
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        collected = []

        def fake_sendto(data, addr):
            if len(data) >= 16:
                collected.append(struct.unpack_from("!I", data, 12)[0])

        # 先装收集器再推送（push_tts_single 用后台线程发包，sleep 等待）
        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()

        pkts1 = [b"\x00" * 20, b"\x01" * 20]
        pkts2 = [b"\x02" * 20, b"\x03" * 20, b"\x04" * 20]

        mqtt_server.push_tts_single("first", pkts1, device_id)
        mqtt_server.push_tts_single("second", pkts2, device_id)
        deadline = time.time() + 5
        while len(collected) < 5 and time.time() < deadline:
            time.sleep(0.05)

        assert len(collected) == 5
        # 第一段 seq: 0, 1；第二段首帧 = 第一段末帧 + 1（跨段连续）
        assert collected[0] == 0
        assert collected[1] == 1
        assert collected[2] == 2
        assert collected[3] == 3
        assert collected[4] == 4

    def test_concurrent_pushes_no_duplicate_seq(self, mqtt_server):
        """Barrier 双线程并发推送同设备：seq 无重复。"""
        device_id = "seq-concurrent"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        addr = ("127.0.0.1", 9999)
        with _sessions_lock:
            _sessions[device_id] = {
                "addr": addr,
                "aes_key": aes_key,
                "aes_nonce": aes_nonce,
                "timestamp": 0,
                "seq": 0,
                "seq_lock": threading.Lock(),
            }

        barrier = threading.Barrier(2)
        collected = []
        lock = threading.Lock()

        def fake_sendto(data, addr):
            if len(data) >= 16:
                seq = struct.unpack_from("!I", data, 12)[0]
                with lock:
                    collected.append(seq)

        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()

        def push_burst(text, pkts):
            barrier.wait()
            mqtt_server.push_tts_single(text, pkts, device_id)

        t1 = threading.Thread(target=push_burst, args=("a", [b"\x00" * 20] * 3))
        t2 = threading.Thread(target=push_burst, args=("b", [b"\x01" * 20] * 3))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        # push_tts_single 的发送线程是异步的，等全部 6 包落地
        deadline = time.time() + 5
        while len(collected) < 6 and time.time() < deadline:
            time.sleep(0.05)

        assert len(collected) == 6
        assert len(set(collected)) == 6, f"seq 重复: {collected}"

    def test_hello_goodbye_resets_seq(self, mqtt_server):
        """hello→push→goodbye→hello：seq 从 0 重新计。"""
        device_id = "seq-reset"
        aes_key, aes_nonce = _generate_aes_key_nonce()
        addr = ("127.0.0.1", 9999)
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = type("C", (), {"publish": lambda *a, **kw: None})()

        mqtt_server._handle_hello({}, device_id, up_topic)
        with _sessions_lock:
            assert _sessions[device_id]["seq"] == 0
            # hello 阶段尚无 UDP 地址（addr 由首个上行包学习），补上才能推送音频
            _sessions[device_id]["addr"] = addr

        collected = []

        def fake_sendto(data, addr):
            if len(data) >= 16:
                collected.append(struct.unpack_from("!I", data, 12)[0])

        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()
        mqtt_server.push_tts_single("first", [b"\x00" * 20, b"\x01" * 20], device_id)
        # 等两包都落地（seq 在分配期即置位，不能作为收包完成信号）
        deadline = time.time() + 5
        while len(collected) < 2 and time.time() < deadline:
            time.sleep(0.05)

        mqtt_server._handle_goodbye({"session_id": "x"}, device_id)
        with _sessions_lock:
            assert device_id not in _sessions

        mqtt_server._handle_hello({}, device_id, up_topic)
        with _sessions_lock:
            assert _sessions[device_id]["seq"] == 0
            _sessions[device_id]["addr"] = addr

        mqtt_server._udp_sock = type("Sock", (), {"sendto": staticmethod(fake_sendto)})()
        mqtt_server.push_tts_single("after", [b"\x02" * 20], device_id)
        deadline = time.time() + 5
        while len(collected) < 3 and time.time() < deadline:
            time.sleep(0.05)

        assert collected == [0, 1, 0], f"seq 未重置: {collected}"


# ─────────────────────────────────────────────────────────
# R3-B: _handle_abort 与 abort flag 生命周期
# ─────────────────────────────────────────────────────────

class TestAbortFlag:
    """per-device abort flag 世代化 auto-clear。"""

    def test_abort_sets_flag_and_auto_clears_when_no_new_activity(self, mqtt_server, monkeypatch):
        """无新一轮活动时，auto-clear 在延迟后生效。"""
        monkeypatch.setattr("app.mqtt_server._ABORT_AUTO_CLEAR_DELAY", 0.2)
        device_id = "abort-life"
        flag = mqtt_server._get_abort_flag(device_id)
        assert not flag.is_set()
        mqtt_server._handle_abort(device_id)
        assert flag.is_set()
        time.sleep(0.3)
        assert not flag.is_set()

    def test_new_utterance_invalidates_old_auto_clear(self, mqtt_server, monkeypatch):
        """新 utterance 启动（clear + 世代+1）使旧的 auto-clear 线程失效。"""
        monkeypatch.setattr("app.mqtt_server._ABORT_AUTO_CLEAR_DELAY", 0.3)
        device_id = "abort-invalidate"
        flag = mqtt_server._get_abort_flag(device_id)
        mqtt_server._handle_abort(device_id)
        assert flag.is_set()
        # 模拟新 utterance 启动（不实际跑 ASR/TTS）
        with mqtt_server._abort_generations_lock:
            old_gen = mqtt_server._abort_generations.get(device_id, 0)
        abort_flag = mqtt_server._get_abort_flag(device_id)
        with mqtt_server._abort_generations_lock:
            mqtt_server._abort_generations[device_id] = old_gen + 1
        abort_flag.clear()
        # 等待旧的 auto-clear 线程醒来
        time.sleep(0.4)
        # 旧 clearer 发现世代已变，不应 clear；新 utterance 已 clear，所以 flag 仍为未设置
        assert not flag.is_set()
        # 再发一次 abort：应能正常 set（证明旧 clearer 没有破坏 flag 语义）
        mqtt_server._handle_abort(device_id)
        assert flag.is_set()

    def test_generation_monotonic_and_second_abort_survives_first_clearer(self, mqtt_server, monkeypatch):
        """t0 abort → t1 新 utterance → t2 再次 abort：世代单调递增，第二代 set 后，
        第一代 clearer 到期时不应再清除 flag。"""
        monkeypatch.setattr("app.mqtt_server._ABORT_AUTO_CLEAR_DELAY", 0.3)
        device_id = "abort-gen"
        flag = mqtt_server._get_abort_flag(device_id)
        # t0: 第一次 abort (gen=1, flag set, clearer T0 0.3s 后到期)
        mqtt_server._handle_abort(device_id)
        with mqtt_server._abort_generations_lock:
            gen0 = mqtt_server._abort_generations[device_id]
        assert flag.is_set()
        # t1 (0.1s 后): 新 utterance 启动（世代 +1，clear flag）
        time.sleep(0.1)
        with mqtt_server._abort_generations_lock:
            mqtt_server._abort_generations[device_id] = gen0 + 1
            gen1 = mqtt_server._abort_generations[device_id]
        flag.clear()
        assert gen1 == gen0 + 1
        # t2 (0.1s 后 = 0.2s 后 t0): 第二次 abort (gen=3, flag set, clearer T2 0.3s 后到期)
        time.sleep(0.1)
        mqtt_server._handle_abort(device_id)
        with mqtt_server._abort_generations_lock:
            gen2 = mqtt_server._abort_generations[device_id]
        assert gen2 == gen1 + 1
        assert flag.is_set()
        # t3: 第一代 clearer T0 到期（t0+0.3s）。它应发现 gen=3 != 1，不执行 clear。
        # 轮询等待至 T0 应到期之后：若 T0 误清，poll 立即退出且断言失败；
        # 正常情况下等满窗口后 flag 仍为 set（固定 sleep 在满载下会漂移）。
        _wait_flag(flag, want_set=True, timeout=0.6)
        assert flag.is_set(), "第一代 clearer 到期时，第二代 abort 的 flag 不应被清除"
        # t4: 第二代 clearer T2 到期（t2+0.3s = t0+0.5s），应正常 clear
        _wait_flag(flag, want_set=False, timeout=2.0)

    def test_abort_is_per_device(self, mqtt_server):
        d1, d2 = "abort-a", "abort-b"
        f1 = mqtt_server._get_abort_flag(d1)
        f2 = mqtt_server._get_abort_flag(d2)
        mqtt_server._handle_abort(d1)
        assert f1.is_set()
        assert not f2.is_set()
