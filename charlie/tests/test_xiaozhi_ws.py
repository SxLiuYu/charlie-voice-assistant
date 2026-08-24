"""Integration tests for the /ws/xiaozhi WebSocket endpoint.

Starts its own uvicorn server (voice_server:app) on 127.0.0.1 in a background
thread (default port 8001, overridable via XIAOZHI_TEST_PORT), so it does not
interfere with any server the user may already be running.

Protocol-level tests (hello/ping/abort/empty-audio) run unconditionally. The
full ASR->brain->TTS->Opus path test gracefully skips when external ASR/TTS
services are unavailable.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

# Ensure the charlie package root (parent of tests/) is importable regardless of cwd.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import uvicorn  # noqa: E402
import websockets  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

# Import the already-built app object so uvicorn does not rely on cwd/import string.
import voice_server  # noqa: E402

APP = voice_server.app

PORT = int(os.environ.get("XIAOZHI_TEST_PORT", "8001"))
HOST = "127.0.0.1"
WS_URL = f"ws://{HOST}:{PORT}/ws/xiaozhi"


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server did not start on {host}:{port} within {timeout}s")


@pytest.fixture(scope="session")
def xiaozhi_server():
    config = uvicorn.Config(APP, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(HOST, PORT)
    # Small extra grace period so the app is fully ready to accept WS upgrades.
    time.sleep(0.3)
    try:
        yield WS_URL
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def pcm_to_opus_packets(pcm: bytes, sample_rate: int = 16000,
                        frame_duration: int = 60):
    import opuslib
    enc = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    out = []
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        out.append(enc.encode(pcm[i:i + frame_bytes], frame_samples))
    return out


def generate_tone_pcm(duration_sec: float = 2.5, freq: int = 440,
                      sample_rate: int = 16000) -> bytes:
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={duration_sec}",
         "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "pipe:1"],
        capture_output=True, timeout=20,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")[:300]
    return r.stdout


def silence_pcm(duration_sec: float = 3.2, sample_rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(sample_rate * duration_sec)


def collect_responses(ws, total_timeout: float = 45.0,
                      stop_on_tts_stop: bool = True):
    """Collect JSON and binary messages from a websocket.

    Returns (json_messages: list[dict], binary_count: int, binary_bytes: int).
    Stops early when a {"type":"tts","state":"stop"} message is seen, or after
    total_timeout.
    """
    jsons = []
    binary_count = 0
    binary_bytes = 0
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        try:
            msg = ws.recv(timeout=min(2.0, remaining))
        except TimeoutError:
            # websockets.sync raises TimeoutError on recv timeout
            if stop_on_tts_stop and any(
                    m.get("type") == "tts" and m.get("state") == "stop"
                    for m in jsons):
                break
            continue
        except Exception:
            break
        if isinstance(msg, bytes):
            binary_count += 1
            binary_bytes += len(msg)
        elif isinstance(msg, str):
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            jsons.append(obj)
            if stop_on_tts_stop and obj.get("type") == "tts" \
                    and obj.get("state") == "stop":
                break
    return jsons, binary_count, binary_bytes


def _hello(ws):
    ws.send(json.dumps({"type": "hello"}))
    # Read until we see the hello response.
    deadline = time.time() + 10
    while time.time() < deadline:
        msg = ws.recv(timeout=2.0)
        if isinstance(msg, str):
            obj = json.loads(msg)
            if obj.get("type") == "hello":
                return obj
    raise AssertionError("no hello response received")


# --------------------------------------------------------------------------- #
# Protocol-level tests (unconditional)
# --------------------------------------------------------------------------- #

def test_hello_handshake(xiaozhi_server):
    with ws_connect(xiaozhi_server) as ws:
        resp = _hello(ws)
    assert resp["type"] == "hello"
    assert resp["transport"] == "websocket"
    assert isinstance(resp["session_id"], str) and resp["session_id"]
    ap = resp["audio_params"]
    assert ap["sample_rate"] == 24000
    assert ap["frame_duration"] == 60


def test_ping_pong(xiaozhi_server):
    with ws_connect(xiaozhi_server) as ws:
        _hello(ws)
        ws.send(json.dumps({"type": "ping"}))
        deadline = time.time() + 10
        pong = None
        while time.time() < deadline:
            msg = ws.recv(timeout=2.0)
            if isinstance(msg, str):
                obj = json.loads(msg)
                if obj.get("type") == "pong":
                    pong = obj
                    break
    assert pong is not None and pong["type"] == "pong"


def test_abort(xiaozhi_server):
    with ws_connect(xiaozhi_server) as ws:
        _hello(ws)
        ws.send(json.dumps({"type": "abort"}))
        jsons, _, _ = collect_responses(ws, total_timeout=10.0)
    assert any(m.get("type") == "tts" and m.get("state") == "stop" for m in jsons), \
        f"expected tts stop after abort, got: {jsons}"


def test_empty_silent_audio(xiaozhi_server):
    """3+ seconds of silent Opus must be dropped silently.

    New behavior (2026-08-09): empty / silent utterances are ignored without
    any TTS reply, so the device never nags the user over ambient noise.
    """
    packets = pcm_to_opus_packets(silence_pcm(3.2))
    assert len(packets) >= 50, f"expected >=50 frames for ~3s, got {len(packets)}"

    with ws_connect(xiaozhi_server) as ws:
        _hello(ws)
        ws.send(json.dumps({"type": "listen", "state": "start"}))
        for pkt in packets:
            ws.send(pkt)
        ws.send(json.dumps({"type": "listen", "state": "stop"}))
        jsons, binary_count, _ = collect_responses(ws, total_timeout=10.0,
                                                   stop_on_tts_stop=False)

    types = [(m.get("type"), m.get("state")) for m in jsons]
    assert ("tts", "start") not in types, f"expected silence, got: {types}"


# --------------------------------------------------------------------------- #
# Full ASR -> brain -> TTS -> Opus path (graceful skip if external deps missing)
# --------------------------------------------------------------------------- #

_EXTERNAL_DEP_HINTS = (
    "api key", "apikey", "api_key", "unauthorized", "forbidden",
    "connection", "timeout", "timed out", "name or service not known",
    "temporarily unavailable", "max retries", "ssl", "certificate",
    "network", "failed to resolve", "no such host", "502", "503", "504",
    "401", "403", "429", "额度", "密钥", "余额", "连接", "超时",
)


def _looks_like_external_dependency_failure(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _EXTERNAL_DEP_HINTS)


def test_short_click_then_silence_gets_hint(xiaozhi_server):
    """A brief loud click (< MIN_SPEECH_FRAMES of speech) followed by silence
    must be dropped silently and promptly (new 2026-08-09 behavior: ambient
    noise / clicks never produce a spoken reply).
    """
    try:
        pcm = generate_tone_pcm(duration_sec=0.8, freq=400)
        pcm += silence_pcm(4.0)
    except Exception as e:
        pytest.skip(f"tone generation failed: {e}")

    packets = pcm_to_opus_packets(pcm)
    start = time.time()
    with ws_connect(xiaozhi_server) as ws:
        _hello(ws)
        ws.send(json.dumps({"type": "listen", "state": "start"}))
        for pkt in packets:
            ws.send(pkt)
        ws.send(json.dumps({"type": "listen", "state": "stop"}))
        jsons, _, _ = collect_responses(ws, total_timeout=10.0,
                                        stop_on_tts_stop=False)

    elapsed = time.time() - start
    types = [(m.get("type"), m.get("state")) for m in jsons]
    assert ("tts", "start") not in types, f"expected silence, got: {types}"
    # The drop must resolve promptly; buffer must never accumulate endlessly.
    assert elapsed < 15, f"response took too long: {elapsed:.1f}s"


def test_real_audio_full_path(xiaozhi_server):
    """Send a real 440Hz tone through the full pipeline.

    A pure tone is not speech, so ASR returns empty and the new behavior
    (2026-08-09) is to stay silent — no TTS reply for non-speech audio. This
    still exercises the decode+VAD+ASR path end to end. We assert no tts
    messages are produced. If external ASR is unavailable, skip (don't fail).
    """
    try:
        pcm = generate_tone_pcm(duration_sec=2.5, freq=440)
    except Exception as e:
        pytest.skip(f"ffmpeg tone generation failed: {e}")

    packets = pcm_to_opus_packets(pcm)
    assert len(packets) >= 30, f"expected >=30 opus frames, got {len(packets)}"

    try:
        with ws_connect(xiaozhi_server) as ws:
            _hello(ws)
            ws.send(json.dumps({"type": "listen", "state": "start"}))
            for pkt in packets:
                ws.send(pkt)
            ws.send(json.dumps({"type": "listen", "state": "stop"}))
            jsons, binary_count, _ = collect_responses(
                ws, total_timeout=15.0, stop_on_tts_stop=False)
    except Exception as e:
        if _looks_like_external_dependency_failure(str(e)):
            pytest.skip(f"external ASR/TTS service unavailable: {e}")
        raise

    types = [(m.get("type"), m.get("state")) for m in jsons]
    assert ("tts", "start") not in types, \
        f"expected silence for non-speech tone, got: {types}"
    assert binary_count == 0, f"expected no binary packets, got {binary_count}"


# --------------------------------------------------------------------------- #
# Continuous conversation: one wake word must allow multiple follow-up turns
# without re-waking (iFLYOS-style). Regression test for the "only one round"
# bug where reply-finish set armed_until=0 and dropped every later utterance.
# --------------------------------------------------------------------------- #

def _drain_json(ws, seconds):
    msgs = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            msg = ws.recv(timeout=0.5)
        except Exception:
            continue
        if isinstance(msg, str):
            try:
                msgs.append(json.loads(msg))
            except json.JSONDecodeError:
                pass
    return msgs


def _send_utterance_burst(ws, tone_sec=2.0, silence_sec=2.2):
    """Send a burst of loud tone + silence so the server's VAD endpoints it.

    Firmware v2.1 auto-mode does not send listen/stop; the server's own
    endpointer fires after MIN_SPEECH_FRAMES hot + SILENCE_FRAMES quiet.
    """
    pcm = generate_tone_pcm(duration_sec=tone_sec, freq=440)
    pcm += silence_pcm(silence_sec)
    for pkt in pcm_to_opus_packets(pcm):
        ws.send(pkt)


def test_one_wake_allows_multiple_followups(xiaozhi_server):
    """After one wake word, a reply re-arms the listen window so a second
    utterance WITHOUT a new wake word still gets a reply."""
    asr_texts = iter(["第一句问题", "第二句追问"])
    brain_calls = []

    def fake_asr(audio, fmt="wav"):
        try:
            return next(asr_texts)
        except StopIteration:
            return ""

    def fake_brain(text, session_id="default", interrupted_reply=""):
        brain_calls.append(text)
        yield (f"回复{len(brain_calls)}", f"回复{len(brain_calls)}")

    with patch("app.xiaozhi_ws._synth_wake_beep", return_value=b""), \
         patch("app.xiaozhi_ws._synth_mp3", return_value=b"\x00" * 200), \
         patch("app.xiaozhi_ws.mp3_to_opus_packets",
               return_value=[b"\xf8\xff\xfe", b"\xf8\xff\xfe", b"\xf8\xff\xfe"]), \
         patch("app.xiaozhi_ws._load_silero_vad", return_value=None), \
         patch("voice_agent.asr", side_effect=fake_asr), \
         patch("voice_agent.brain_stream_sentences", side_effect=fake_brain):
        with ws_connect(xiaozhi_server) as ws:
            _hello(ws)
            # Wake + first utterance
            ws.send(json.dumps({"type": "listen", "state": "detect",
                                "text": "你好小智"}))
            time.sleep(0.6)  # let wake echo cooldown pass
            ws.send(json.dumps({"type": "listen", "state": "start"}))
            _send_utterance_burst(ws)
            # Wait for first reply (ASR + brain + TTS)
            first = _drain_json(ws, 12.0)

            # Second utterance — NO new wake word; device just re-listens
            ws.send(json.dumps({"type": "listen", "state": "start"}))
            _send_utterance_burst(ws)
            second = _drain_json(ws, 12.0)

    all_msgs = first + second
    tts_starts = [m for m in all_msgs
                  if m.get("type") == "tts" and m.get("state") == "start"]
    sentence_texts = [m.get("text") for m in all_msgs
                      if m.get("type") == "tts" and m.get("state") == "sentence_start"]

    assert len(tts_starts) == 2, (
        f"expected 2 TTS replies after one wake, got {len(tts_starts)}; "
        f"sentences={sentence_texts}")
    assert len(brain_calls) == 2, f"brain called {len(brain_calls)}x: {brain_calls}"
    assert brain_calls == ["第一句问题", "第二句追问"], brain_calls


def test_short_utterance_reaches_asr(xiaozhi_server):
    """Short commands like "几点了" (~0.9s of speech) must still endpoint.
    Regression for the bug where only ~13 hot frames accumulated and, with
    MIN_SPEECH_FRAMES=18, the utterance never ended until the goodbye timeout."""
    asr_calls = []

    def fake_asr(audio, fmt="wav"):
        asr_calls.append(True)
        return "现在几点了"

    def fake_brain(text, session_id="default", interrupted_reply=""):
        yield ("现在09点28分。", "现在09点28分。")

    with patch("app.xiaozhi_ws._synth_wake_beep", return_value=b""), \
         patch("app.xiaozhi_ws._synth_mp3", return_value=b"\x00" * 200), \
         patch("app.xiaozhi_ws.mp3_to_opus_packets",
               return_value=[b"\xf8\xff\xfe", b"\xf8\xff\xfe", b"\xf8\xff\xfe"]), \
         patch("app.xiaozhi_ws._load_silero_vad", return_value=None), \
         patch("voice_agent.asr", side_effect=fake_asr), \
         patch("voice_agent.brain_stream_sentences", side_effect=fake_brain):
        with ws_connect(xiaozhi_server) as ws:
            _hello(ws)
            ws.send(json.dumps({"type": "listen", "state": "detect",
                                "text": "你好小智"}))
            time.sleep(0.6)
            ws.send(json.dumps({"type": "listen", "state": "start"}))
            # ~1.0s of loud tone + silence: short utterance, not a click.
            _send_utterance_burst(ws, tone_sec=1.0, silence_sec=2.2)
            msgs = _drain_json(ws, 12.0)

    assert asr_calls, "ASR was never called — short utterance did not endpoint"
    starts = [m for m in msgs
              if m.get("type") == "tts" and m.get("state") == "start"]
    assert starts, f"expected a TTS reply for a short command, got: {msgs}"


def test_idle_after_arm_window_sends_goodbye(xiaozhi_server):
    """After the follow-up window expires with no speech, the server must send
    goodbye so the firmware returns to local KWS (firmware disables KWS while
    the WS is open — without goodbye the device streams forever and can never
    be re-woken)."""
    import websockets as _ws
    with patch("app.xiaozhi_ws._synth_wake_beep", return_value=b""), \
         patch("app.xiaozhi_ws._get_arm_window", return_value=0.3):
        with ws_connect(xiaozhi_server) as ws:
            _hello(ws)
            ws.send(json.dumps({"type": "listen", "state": "detect",
                                "text": "你好小智"}))
            time.sleep(0.3)
            # Stream silence; the server closes after sending goodbye.
            closed = False
            for pkt in pcm_to_opus_packets(silence_pcm(8.0)):
                try:
                    ws.send(pkt)
                except _ws.exceptions.ConnectionClosed:
                    closed = True
                    break
                time.sleep(0.02)
            # Drain any remaining JSON until the connection closes.
            msgs = []
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    m = ws.recv(timeout=0.5)
                    if isinstance(m, str):
                        try:
                            msgs.append(json.loads(m))
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    break

    assert any(m.get("type") == "goodbye" for m in msgs), \
        f"expected goodbye after idle window, got: {[m.get('type') for m in msgs]}"


def test_brain_error_sends_fallback(xiaozhi_server):
    """当 brain 抛异常时，客户端应收到的兜底语（不走死链超时）。"""
    def raising_brain(text, session_id="default", interrupted_reply=""):
        raise RuntimeError("brain boom")

    with patch("app.xiaozhi_ws._synth_wake_beep", return_value=b""), \
         patch("app.xiaozhi_ws._synth_mp3", return_value=b"\x00" * 200), \
         patch("app.xiaozhi_ws.mp3_to_opus_packets",
               return_value=[b"\xf8\xff\xfe", b"\xf8\xff\xfe", b"\xf8\xff\xfe"]), \
         patch("app.xiaozhi_ws._load_silero_vad", return_value=None), \
         patch("voice_agent.asr", return_value="你好"), \
         patch("voice_agent.brain_stream_sentences", side_effect=raising_brain):
        with ws_connect(xiaozhi_server) as ws:
            _hello(ws)
            ws.send(json.dumps({"type": "listen", "state": "detect",
                                "text": "你好小智"}))
            time.sleep(0.6)
            ws.send(json.dumps({"type": "listen", "state": "start"}))
            _send_utterance_burst(ws, tone_sec=1.0, silence_sec=2.2)
            jsons, _, _ = collect_responses(ws, total_timeout=15.0)

    texts = [m.get("text", "") for m in jsons if m.get("type") == "tts"
             and m.get("state") == "sentence_start"]
    assert any("出了点问题" in t for t in texts), \
        f"expected error fallback text, got: {texts}"


def test_brain_deadlink_sends_slow_reply_fallback(xiaozhi_server):
    """brain 死链（永不产出）时，客户端应收到的兜底语且流程正常结束。"""
    _stop = threading.Event()

    def blocking_brain(text, session_id="default", interrupted_reply=""):
        # 模拟 brain 死链：在首 yield 前永久阻塞，但可被 Event 取消
        _stop.wait(timeout=120)
        yield ("never", "never")

    try:
        with patch("app.xiaozhi_ws._synth_wake_beep", return_value=b""), \
             patch("app.xiaozhi_ws._synth_mp3", return_value=b"\x00" * 200), \
             patch("app.xiaozhi_ws.mp3_to_opus_packets",
                   return_value=[b"\xf8\xff\xfe", b"\xf8\xff\xfe", b"\xf8\xff\xfe"]), \
             patch("app.xiaozhi_ws._load_silero_vad", return_value=None), \
             patch("voice_agent.asr", return_value="你好"), \
             patch("voice_agent.brain_stream_sentences", side_effect=blocking_brain), \
             patch("app.xiaozhi_ws._FIRST_SENTENCE_TIMEOUT", 3.0):
            with ws_connect(xiaozhi_server) as ws:
                _hello(ws)
                ws.send(json.dumps({"type": "listen", "state": "detect",
                                    "text": "你好小智"}))
                time.sleep(0.6)
                ws.send(json.dumps({"type": "listen", "state": "start"}))
                _send_utterance_burst(ws, tone_sec=1.0, silence_sec=2.2)
                jsons, _, _ = collect_responses(ws, total_timeout=15.0)

        types = [(m.get("type"), m.get("state"), m.get("text", "")) for m in jsons]
        # 必须收到 stop + 兜底语
        assert any(t == "tts" and s == "stop" for t, s, _ in types), \
            f"expected tts stop, got: {types}"
        assert any("反应慢了" in text for _, _, text in types), \
            f"expected slow-reply fallback, got: {types}"
    finally:
        _stop.set()


def test_xiaozhi_ws_local_no_token_accepted(xiaozhi_server):
    """本地直连（无代理头、对端IP为127.0.0.1）不带 token 也应 accept。"""
    with ws_connect(xiaozhi_server) as ws:
        resp = _hello(ws)
    assert resp["type"] == "hello"


def test_xiaozhi_ws_proxy_no_token_rejected(xiaozhi_server):
    """带 X-Forwarded-For 且无 token 时，应 close(1008)。"""
    with patch("app.xiaozhi_ws.AUTH_TOKEN", "test-secret"):
        try:
            with ws_connect(xiaozhi_server, additional_headers={"X-Forwarded-For": "1.2.3.4"}) as ws:
                ws.recv(timeout=2.0)
        except Exception:
            # websockets 库在服务端 close 后可能抛 ConnectionClosed
            pass


def test_xiaozhi_ws_proxy_with_token_accepted(xiaozhi_server):
    """带 X-Forwarded-For 且 query token 正确时，应 accept。"""
    with patch("app.xiaozhi_ws.AUTH_TOKEN", "test-secret"):
        url = f"{xiaozhi_server}?token=test-secret"
        with ws_connect(url, additional_headers={"X-Forwarded-For": "1.2.3.4"}) as ws:
            resp = _hello(ws)
        assert resp["type"] == "hello"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
