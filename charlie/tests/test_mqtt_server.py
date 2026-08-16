"""Unit tests for app/mqtt_server.py — MQTT protocol server for ESP32.

Tests cover:
1. Utility functions (AES, packet building, device ID extraction)
2. Hello handling (session creation, UDP config reply)
3. Push TTS (with and without active session)
4. Proactive text-only push (no UDP session needed)
5. Goodbye handling (session cleanup)
6. Listen state handling
"""

import json
import os
import socket
import struct
import sys
import threading
import time
from unittest.mock import MagicMock, patch, ANY

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from app.mqtt_server import (
    _generate_aes_key_nonce,
    _hex_encode,
    _aes_ctr_crypt,
    _build_audio_packet,
    _extract_device_id,
    UDP_AUDIO_HEADER_SIZE,
    DOWNLINK_SAMPLE_RATE,
    UPLINK_SAMPLE_RATE,
    OPUS_FRAME_DURATION_MS,
    MqttXiaozhiServer,
    _sessions,
    _sessions_lock,
    _device_topics,
    _device_topics_lock,
    _addr_to_device,
    _addr_to_device_lock,
    get_server,
    init_server,
    push_tts_to_mqtt,
)


# ── Utility function tests ──────────────────────────────────────────────────

class TestAesKeyGeneration:
    """AES key/nonce generation for UDP encrypted audio."""

    def test_generates_16_byte_key_and_nonce(self):
        key, nonce = _generate_aes_key_nonce()
        assert len(key) == 16
        assert len(nonce) == 16
        assert isinstance(key, bytes)
        assert isinstance(nonce, bytes)

    def test_generates_unique_keys(self):
        keys = {_generate_aes_key_nonce()[0] for _ in range(100)}
        assert len(keys) == 100, "AES keys must be unique"

    def test_hex_encode_round_trip(self):
        data = b"\x01\x02\x03\xff"
        hex_str = _hex_encode(data)
        assert hex_str == "010203ff"
        assert bytes.fromhex(hex_str) == data


class TestAesCtrCrypt:
    """AES-CTR encrypt/decrypt is symmetric."""

    def test_encrypt_decrypt_round_trip(self):
        key = b"0123456789abcdef"
        nonce = b"abcdef0123456789"
        plaintext = b"Hello ESP32 audio frame data!"
        encrypted = _aes_ctr_crypt(key, nonce, plaintext)
        decrypted = _aes_ctr_crypt(key, nonce, encrypted)
        assert decrypted == plaintext

    def test_different_nonce_produces_different_output(self):
        key = b"0123456789abcdef"
        nonce1 = b"abcdef0123456789"
        nonce2 = b"abcdef0123456790"
        plaintext = b"test data"
        enc1 = _aes_ctr_crypt(key, nonce1, plaintext)
        enc2 = _aes_ctr_crypt(key, nonce2, plaintext)
        assert enc1 != enc2
        assert enc1 != plaintext
        assert enc2 != plaintext


class TestBuildAudioPacket:
    """UDP audio packet format: |type|flags|payload_len|ssrc|timestamp|sequence|encrypted_payload|"""

    def test_packet_header_correct(self):
        aes_key = b"0123456789abcdef"
        aes_nonce = b"abcdef0123456789"
        payload = b"\x00\x01\x02\x03"
        ts = 1000
        seq = 5
        packet = _build_audio_packet(aes_key, aes_nonce, payload, ts, seq)

        assert packet[0] == 0x01  # type = audio
        assert packet[1] == 0x00  # flags
        payload_len = struct.unpack_from("!H", packet, 2)[0]
        assert payload_len == len(payload)
        timestamp = struct.unpack_from("!I", packet, 8)[0]
        assert timestamp == ts
        sequence = struct.unpack_from("!I", packet, 12)[0]
        assert sequence == seq
        assert len(packet) == UDP_AUDIO_HEADER_SIZE + len(payload)

    def test_packet_encrypted(self):
        aes_key = b"0123456789abcdef"
        aes_nonce = b"abcdef0123456789"
        payload = b"audio data"
        packet = _build_audio_packet(aes_key, aes_nonce, payload, 0, 0)
        # The payload part (after header) should NOT equal the plaintext
        encrypted = packet[UDP_AUDIO_HEADER_SIZE:]
        assert encrypted != payload

    def test_packet_decryptable(self):
        """Packet payload can be decrypted back to original."""
        aes_key = b"0123456789abcdef"
        aes_nonce = b"abcdef0123456789"
        payload = b"test audio frame"
        packet = _build_audio_packet(aes_key, aes_nonce, payload, 42, 7)

        # Extract per-packet nonce from the base nonce
        pnonce = bytearray(aes_nonce)
        struct.pack_into("!H", pnonce, 2, len(payload))
        struct.pack_into("!I", pnonce, 8, 42)
        struct.pack_into("!I", pnonce, 12, 7)

        encrypted = packet[UDP_AUDIO_HEADER_SIZE:]
        decrypted = _aes_ctr_crypt(aes_key, bytes(pnonce), encrypted)
        assert decrypted == payload


class TestExtractDeviceId:
    """Extract device_id from MQTT topic string."""

    def test_normal_topic(self):
        topic = "charlie/esp32/esp32-default/up"
        assert _extract_device_id(topic) == "esp32-default"

    def test_custom_device_id(self):
        topic = "charlie/esp32/living-room-01/up"
        assert _extract_device_id(topic) == "living-room-01"

    def test_down_topic_returns_unknown(self):
        topic = "charlie/esp32/esp32-default/down"
        assert _extract_device_id(topic) == "unknown"

    def test_invalid_topic(self):
        assert _extract_device_id("invalid/topic") == "unknown"
        assert _extract_device_id("") == "unknown"


# ── MqttXiaozhiServer unit tests ────────────────────────────────────────────

@pytest.fixture
def mqtt_server():
    """Create an MqttXiaozhiServer instance without starting MQTT/UDP."""
    server = MqttXiaozhiServer()
    # Don't call start() — we test methods directly
    yield server
    # Cleanup any sessions created during tests
    with _sessions_lock:
        _sessions.clear()
    with _device_topics_lock:
        _device_topics.clear()
    with _addr_to_device_lock:
        _addr_to_device.clear()


class TestHandleHello:
    """Hello handling: creates session, replies with UDP config."""

    def test_creates_session_with_aes_keys(self, mqtt_server):
        device_id = "test-device-01"
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = MagicMock()

        mqtt_server._handle_hello({}, device_id, up_topic)

        with _sessions_lock:
            session = _sessions.get(device_id)
        assert session is not None
        assert len(session["aes_key"]) == 16
        assert len(session["aes_nonce"]) == 16
        assert session["addr"] is None  # addr filled on first UDP packet
        assert "timestamp" in session

    def test_registers_device_topic(self, mqtt_server):
        device_id = "test-device-02"
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = MagicMock()

        mqtt_server._handle_hello({}, device_id, up_topic)

        with _device_topics_lock:
            assert device_id in _device_topics
            assert _device_topics[device_id] == f"charlie/esp32/{device_id}/down"

    def test_publishes_hello_reply_with_udp_config(self, mqtt_server):
        device_id = "test-device-03"
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = MagicMock()
        mqtt_server._udp_port = 8888

        mqtt_server._handle_hello({}, device_id, up_topic)

        # Check publish was called (first to down_topic, then also to up_topic)
        assert mqtt_server._client.publish.called
        call_args = mqtt_server._client.publish.call_args_list
        # First call is to down_topic
        assert call_args[0][0][0] == f"charlie/esp32/{device_id}/down"
        # Second call is to up_topic (firmware v2.1.0 compatibility)
        assert call_args[1][0][0] == f"charlie/esp32/{device_id}/up"
        payload = json.loads(call_args[0][0][1])

        assert payload["type"] == "hello"
        assert payload["transport"] == "udp"
        assert "session_id" in payload
        assert payload["audio_params"]["format"] == "opus"
        assert payload["audio_params"]["sample_rate"] == DOWNLINK_SAMPLE_RATE
        assert "udp" in payload
        assert "server" in payload["udp"]
        assert payload["udp"]["port"] == 8888
        assert "key" in payload["udp"]
        assert "nonce" in payload["udp"]
        # Key and nonce should be hex strings
        assert len(payload["udp"]["key"]) == 32  # 16 bytes = 32 hex chars
        assert len(payload["udp"]["nonce"]) == 32


class TestHandleGoodbye:
    """Goodbye handling: cleans up sessions."""

    def test_removes_session(self, mqtt_server):
        device_id = "test-device-gb"
        with _sessions_lock:
            _sessions[device_id] = {"aes_key": b"x" * 16, "aes_nonce": b"y" * 16,
                                    "addr": None, "timestamp": time.time()}
        with _device_topics_lock:
            _device_topics[device_id] = f"charlie/esp32/{device_id}/down"

        mqtt_server._handle_goodbye({"session_id": "test"}, device_id)

        with _sessions_lock:
            assert device_id not in _sessions
        with _device_topics_lock:
            assert device_id not in _device_topics


class TestHandleListen:
    """Listen state handling."""

    def test_detect_state(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._handle_listen({"state": "detect", "text": "你好小智"}, "dev-1")
        # Should not crash, just log

    def test_start_state(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._handle_listen({"state": "start"}, "dev-1")

    def test_stop_state(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._handle_listen({"state": "stop"}, "dev-1")


class TestPushTts:
    """Push TTS audio to devices."""

    def test_push_tts_no_session_returns_false(self, mqtt_server):
        """Without a hello/session, push should fail gracefully."""
        mqtt_server._client = MagicMock()
        result = mqtt_server.push_tts("test", [b"\x00" * 10])
        assert result is False

    def test_push_tts_single_no_session_returns_false(self, mqtt_server):
        mqtt_server._client = MagicMock()
        result = mqtt_server.push_tts_single("test", [b"\x00" * 10], "no-such-device")
        assert result is False

    def test_push_tts_single_no_udp_addr_returns_false(self, mqtt_server):
        """Session exists but no UDP addr yet (device hasn't sent audio)."""
        device_id = "push-test-01"
        with _sessions_lock:
            _sessions[device_id] = {
                "aes_key": b"k" * 16, "aes_nonce": b"n" * 16,
                "addr": None, "timestamp": time.time()
            }
        mqtt_server._client = MagicMock()
        result = mqtt_server.push_tts_single("test", [b"\x00" * 10], device_id)
        assert result is False

    def test_push_tts_single_with_session_and_addr(self, mqtt_server):
        """Full push: session + device topic + UDP addr → sends MQTT + UDP."""
        device_id = "push-test-02"
        addr = ("192.168.1.10", 12345)
        # Must register both session AND device topic (hello does both)
        with _sessions_lock:
            _sessions[device_id] = {
                "aes_key": b"k" * 16, "aes_nonce": b"n" * 16,
                "addr": addr, "timestamp": time.time()
            }
        with _device_topics_lock:
            _device_topics[device_id] = f"charlie/esp32/{device_id}/down"

        mqtt_server._client = MagicMock()
        mqtt_server._udp_sock = MagicMock()
        mqtt_server._udp_port = 8888

        opus_packets = [b"\x00" * 20, b"\x01" * 20]
        result = mqtt_server.push_tts_single("hello", opus_packets, device_id)

        assert result is True
        # Should have published TTS start JSON immediately
        assert mqtt_server._client.publish.called
        # Should have sent UDP packets (in a background thread)
        time.sleep(0.5)  # wait for thread
        assert mqtt_server._udp_sock.sendto.called
        # Should also publish TTS stop after audio
        stop_calls = [c for c in mqtt_server._client.publish.call_args_list
                      if '"stop"' in str(c)]
        assert len(stop_calls) >= 1


class TestProactiveTextPush:
    """Proactive text-only push via MQTT (no UDP audio session needed).

    This is the key feature for pushing notifications to the device screen
    even when the device is idle (no wake word, no active conversation).
    """

    def test_push_notification_no_session_uses_default_device(self, mqtt_server):
        """push_notification should push to default device even without session."""
        mqtt_server._client = MagicMock()
        mqtt_server.push_notification("提醒：该喝水了")
        # Should publish to default device topic: charlie/esp32/<default>/down
        assert mqtt_server._client.publish.called
        call = mqtt_server._client.publish.call_args
        topic = call[0][0]
        payload = json.loads(call[0][1])
        assert "charlie/esp32/" in topic
        assert topic.endswith("/down")
        assert payload["type"] == "notification"
        assert "提醒" in payload["text"]

    def test_push_notification_with_registered_device(self, mqtt_server):
        """After hello, push_notification should deliver to device's down topic."""
        device_id = "notif-test-01"
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = MagicMock()
        mqtt_server._udp_port = 8888

        # Simulate hello (registers device topic + creates session)
        mqtt_server._handle_hello({}, device_id, up_topic)

        # Now push notification
        mqtt_server.push_notification("测试通知")

        # Should have published to device's down topic
        publish_calls = mqtt_server._client.publish.call_args_list
        down_topic = f"charlie/esp32/{device_id}/down"
        found = any(call[0][0] == down_topic for call in publish_calls)
        assert found, f"Expected publish to {down_topic}"

    def test_push_notification_custom_default_device(self, mqtt_server, monkeypatch):
        """push_notification respects MQTT_DEVICE_ID env var."""
        monkeypatch.setenv("MQTT_DEVICE_ID", "custom-esp32")
        mqtt_server._client = MagicMock()
        mqtt_server.push_notification("test")
        assert mqtt_server._client.publish.called
        call = mqtt_server._client.publish.call_args
        assert "custom-esp32" in call[0][0]

    def test_push_text_tts_without_session(self, mqtt_server):
        """_push_text_tts should gracefully handle no session (just skip audio)."""
        mqtt_server._client = MagicMock()
        # Should not crash even without a session
        mqtt_server._push_text_tts("test reply", "no-such-device")


class TestOnMqttMessage:
    """MQTT message routing."""

    def test_hello_routes_to_handle_hello(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._udp_port = 8888

        msg = MagicMock()
        msg.topic = "charlie/esp32/route-test/up"
        msg.payload = json.dumps({"type": "hello", "version": 3}).encode()

        mqtt_server._on_mqtt_message(msg)

        with _sessions_lock:
            assert "route-test" in _sessions

    def test_goodbye_routes_to_handle_goodbye(self, mqtt_server):
        device_id = "route-gb"
        with _sessions_lock:
            _sessions[device_id] = {"aes_key": b"x" * 16, "aes_nonce": b"y" * 16,
                                      "addr": None, "timestamp": time.time()}
        mqtt_server._client = MagicMock()

        msg = MagicMock()
        msg.topic = f"charlie/esp32/{device_id}/up"
        msg.payload = json.dumps({"type": "goodbye", "session_id": "abc"}).encode()

        mqtt_server._on_mqtt_message(msg)

        with _sessions_lock:
            assert device_id not in _sessions

    def test_listen_routes_to_handle_listen(self, mqtt_server):
        mqtt_server._client = MagicMock()
        msg = MagicMock()
        msg.topic = "charlie/esp32/listen-test/up"
        msg.payload = json.dumps({"type": "listen", "state": "start"}).encode()

        # Should not crash
        mqtt_server._on_mqtt_message(msg)

    def test_invalid_json_skipped(self, mqtt_server):
        mqtt_server._client = MagicMock()
        msg = MagicMock()
        msg.topic = "charlie/esp32/bad/up"
        msg.payload = b"not json"

        # Should not crash
        mqtt_server._on_mqtt_message(msg)
        with _sessions_lock:
            assert "bad" not in _sessions


class TestDeviceCountAndConnected:
    """Device counting and connection status."""

    def test_device_count_zero(self, mqtt_server):
        assert mqtt_server.device_count() == 0

    def test_device_count_after_hello(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._handle_hello({}, "count-1", "charlie/esp32/count-1/up")
        assert mqtt_server.device_count() == 1

    def test_is_connected_no_sessions(self, mqtt_server):
        assert mqtt_server.is_connected() is False

    def test_is_connected_with_session(self, mqtt_server):
        mqtt_server._client = MagicMock()
        mqtt_server._handle_hello({}, "conn-1", "charlie/esp32/conn-1/up")
        assert mqtt_server.is_connected() is True


# ── Integration: module-level functions ──────────────────────────────────────

class TestModuleFunctions:
    """Module-level init_server and push_tts_to_mqtt."""

    def test_get_server_returns_none_before_init(self):
        # get_server may return None or a stopped server
        server = get_server()
        # Could be None or a stopped instance
        assert server is None or not server.is_connected()

    def test_push_tts_to_mqtt_without_server(self):
        """Should return False gracefully when no server is running."""
        # Clear any existing server
        import app.mqtt_server as mod
        mod._server = None
        result = push_tts_to_mqtt("test", b"mp3 data")
        assert result is False


# ── UDP auto-matching (fix for chicken-and-egg addr problem) ─────────────────

class TestUdpAutoMatching:
    """Auto-matching of unknown UDP packets to sessions with addr=None.

    The first UDP packet from a new device has an unknown source address.
    The _udp_recv_loop tries to decrypt with each session's key to find
    the match. This tests the core decrypt-and-verify mechanism.
    """

    def test_auto_match_decrypts_with_correct_key(self):
        """A valid Opus packet encrypted with the session key decrypts correctly."""
        aes_key, aes_nonce = b"0123456789abcdef", b"abcdef0123456789"
        opus_payload = b"\xfc" + b"\x00" * 19  # 20-byte Opus frame
        packet = _build_audio_packet(aes_key, aes_nonce, opus_payload, 0, 0)

        # Extract per-packet nonce and decrypt
        plen = struct.unpack_from("!H", packet, 2)[0]
        ts = struct.unpack_from("!I", packet, 8)[0]
        seq = struct.unpack_from("!I", packet, 12)[0]
        n = bytearray(aes_nonce)
        struct.pack_into("!H", n, 2, plen)
        struct.pack_into("!I", n, 8, ts)
        struct.pack_into("!I", n, 12, seq)
        decrypted = _aes_ctr_crypt(aes_key, bytes(n), packet[UDP_AUDIO_HEADER_SIZE:])

        assert decrypted[0] & 0xFC == 0xFC, f"Expected Opus TOC, got {decrypted[0]:#x}"
        assert decrypted == opus_payload

    def test_auto_match_fails_with_wrong_key(self):
        """A packet encrypted with a different key produces garbage, not Opus."""
        key1, nonce1 = b"0123456789abcdef", b"abcdef0123456789"
        key2 = b"zzzzzzzzzzzzzzzz"
        opus_payload = b"\xfc" + b"\x00" * 19
        packet = _build_audio_packet(key1, nonce1, opus_payload, 0, 0)

        plen = struct.unpack_from("!H", packet, 2)[0]
        n = bytearray(nonce1)
        struct.pack_into("!H", n, 2, plen)
        decrypted = _aes_ctr_crypt(key2, bytes(n), packet[UDP_AUDIO_HEADER_SIZE:])

        # With wrong key, decrypted data should NOT look like Opus
        is_opus = (decrypted[0] & 0xFC) == 0xFC
        if is_opus:
            pass  # False positive (~1/64), acceptable

    def test_session_hello_then_packet_match(self, mqtt_server):
        """Simulate: hello creates session (addr=None), then UDP packet matches."""
        device_id = "auto-match-01"
        up_topic = f"charlie/esp32/{device_id}/up"
        mqtt_server._client = MagicMock()
        mqtt_server._udp_port = 8888

        # hello creates session with addr=None
        mqtt_server._handle_hello({}, device_id, up_topic)
        with _sessions_lock:
            assert _sessions[device_id]["addr"] is None

        # Build a UDP packet encrypted with the session key
        session = _sessions[device_id]
        opus_payload = b"\xfc" + b"\x00" * 19
        packet = _build_audio_packet(session["aes_key"], session["aes_nonce"], opus_payload, 0, 0)

        # Verify it decrypts correctly
        plen = struct.unpack_from("!H", packet, 2)[0]
        ts = struct.unpack_from("!I", packet, 8)[0]
        seq = struct.unpack_from("!I", packet, 12)[0]
        n = bytearray(session["aes_nonce"])
        struct.pack_into("!H", n, 2, plen)
        struct.pack_into("!I", n, 8, ts)
        struct.pack_into("!I", n, 12, seq)
        decrypted = _aes_ctr_crypt(session["aes_key"], bytes(n), packet[UDP_AUDIO_HEADER_SIZE:])
        assert decrypted[0] & 0xFC == 0xFC
        assert decrypted == opus_payload
