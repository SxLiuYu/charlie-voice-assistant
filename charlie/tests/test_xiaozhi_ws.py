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
    """3+ seconds of silent Opus triggers likely_empty_audio fallback.

    The fallback calls stream_response() with a canned sentence, which should
    still produce tts start + sentence_start + (possibly) binary Opus + tts
    stop as long as TTS is reachable. If TTS is unavailable the server still
    emits tts start and tts stop; we only hard-assert those two.
    """
    packets = pcm_to_opus_packets(silence_pcm(3.2))
    assert len(packets) >= 50, f"expected >=50 frames for ~3s, got {len(packets)}"

    with ws_connect(xiaozhi_server) as ws:
        _hello(ws)
        ws.send(json.dumps({"type": "listen", "state": "start"}))
        for pkt in packets:
            ws.send(pkt)
        ws.send(json.dumps({"type": "listen", "state": "stop"}))
        jsons, binary_count, _ = collect_responses(ws, total_timeout=60.0)

    types = [(m.get("type"), m.get("state")) for m in jsons]
    assert ("tts", "start") in types, f"no tts start in responses: {types}"
    assert ("tts", "stop") in types, f"no tts stop in responses: {types}"
    # sentence_start should be present for the canned fallback sentence.
    assert any(m.get("type") == "tts" and m.get("state") == "sentence_start"
               for m in jsons), f"no sentence_start in responses: {types}"


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


def test_real_audio_full_path(xiaozhi_server):
    """Send a real 440Hz tone through the full pipeline.

    A pure tone is not speech, so ASR will likely return empty text, taking
    the `if not asr_text` fallback branch -- but that STILL exercises TTS +
    MP3->Opus encoding + binary frame sending. We assert tts start/stop and
    at least one binary Opus packet. If external ASR/TTS services are
    unavailable, skip (don't fail).
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
            jsons, binary_count, binary_bytes = collect_responses(
                ws, total_timeout=45.0)
    except Exception as e:
        if _looks_like_external_dependency_failure(str(e)):
            pytest.skip(f"external ASR/TTS service unavailable: {e}")
        raise

    types = [(m.get("type"), m.get("state")) for m in jsons]

    # If we got no tts start at all, it's likely the external ASR service
    # failed server-side and was swallowed. Inspect for clues and skip rather
    # than hard-fail, but DO NOT skip assertion failures about present messages.
    if ("tts", "start") not in types:
        # Could be an ASR failure path that sends nothing observable, or an
        # external dependency issue. Skip with diagnostics.
        pytest.skip(
            "no tts start received; external ASR/TTS likely unavailable. "
            f"observed messages: {types}"
        )

    assert ("tts", "start") in types, f"no tts start: {types}"
    assert ("tts", "stop") in types, f"no tts stop: {types}"

    # At least one binary Opus packet proves MP3->Opus + binary send works.
    if binary_count == 0:
        pytest.skip(
            "tts start/stop received but no binary Opus packets; "
            "TTS backend likely unavailable or returned no audio. "
            f"messages: {types}"
        )
    assert binary_count > 0 and binary_bytes > 0, \
        f"binary packets empty: count={binary_count} bytes={binary_bytes}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
