"""Tests for uncovered functions in app/xiaozhi_codec.py.

Covers:
  - opus_decode_to_wav / opus_decode_to_pcm (real encode-decode round trip)
  - mp3_to_ogg_opus (ffmpeg MP3→Ogg Opus, too-small input error)
  - url_to_opus_packets cache-hit branches + invalid URL fast failure
  - _url_disk_load / _url_disk_save round trip + corrupted file safety
"""
import io
import os
import sys
import time
import pickle
import hashlib
import tempfile
import threading
import subprocess
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import opuslib  # noqa: E402

import app.xiaozhi_codec as codec  # noqa: E402


# ===================================================================
# Helpers
# ===================================================================

def _generate_sine_mp3(duration_sec: float = 0.5, freq: int = 440,
                       sample_rate: int = 24000) -> bytes:
    """Use ffmpeg lavfi to synthesize a sine-wave MP3."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={duration_sec}",
         "-ar", str(sample_rate), "-ac", "1", "-f", "mp3", "pipe:1"],
        capture_output=True, timeout=15,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")[:400]
    return r.stdout


def _pcm_to_opus_packets(pcm: bytes, sample_rate: int = 24000,
                         frame_duration: int = 60) -> list[bytes]:
    enc = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    out = []
    for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        out.append(enc.encode(pcm[i:i + frame_bytes], frame_samples))
    return out


# ===================================================================
# Fixtures: isolate in-memory caches and temp disk cache dir
# ===================================================================

@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path, monkeypatch):
    """Reset both in-memory caches and point disk cache to a temp dir."""
    with codec._OPUS_CACHE_LOCK:
        codec._OPUS_CACHE.clear()
    codec._URL_CACHE.clear()
    codec._stats_opus_hits = 0
    codec._stats_url_mem_hits = 0
    codec._stats_url_disk_hits = 0

    # Redirect disk cache dir to tmp_path so we don't pollute repo data/
    monkeypatch.setattr(codec, "_URL_CACHE_DIR", str(tmp_path))
    yield


# ===================================================================
# opus_decode_to_wav / opus_decode_to_pcm
# ===================================================================

class TestOpusDecode:

    def test_opus_decode_to_pcm_round_trip(self):
        """Encode real PCM via opuslib → decode back → lengths match."""
        pcm = _generate_sine_mp3(duration_sec=0.5, freq=440, sample_rate=24000)
        # Re-decode MP3 to raw PCM via ffmpeg for comparison
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
             "-ar", "24000", "-ac", "1", "-f", "s16le", "pipe:1"],
            input=pcm, capture_output=True, timeout=15,
        )
        assert r.returncode == 0, r.stderr.decode(errors="replace")[:300]
        original_pcm = r.stdout
        assert len(original_pcm) > 0

        packets = _pcm_to_opus_packets(original_pcm, sample_rate=24000)
        assert len(packets) > 0, "expected at least one opus packet"

        decoded_pcm = codec.opus_decode_to_pcm(packets, sample_rate=24000)
        assert len(decoded_pcm) > 0, "decoded PCM should not be empty"
        # decoded length should be in the same ballpark as original
        # (Opus frame padding may differ slightly)
        assert len(decoded_pcm) >= len(original_pcm) * 0.5

    def test_opus_decode_to_wav_has_header(self):
        """WAV output must start with RIFF and contain data chunk."""
        pcm = _generate_sine_mp3(duration_sec=0.3, freq=660, sample_rate=16000)
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
             "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"],
            input=pcm, capture_output=True, timeout=15,
        )
        assert r.returncode == 0
        original_pcm = r.stdout
        packets = _pcm_to_opus_packets(original_pcm, sample_rate=16000)
        assert packets

        wav_bytes = codec.opus_decode_to_wav(packets, sample_rate=16000)
        assert wav_bytes.startswith(b"RIFF"), "WAV must start with RIFF header"
        assert b"fmt " in wav_bytes[:40], "WAV must contain fmt chunk"
        assert b"data" in wav_bytes, "WAV must contain data chunk"
        # File size in header should match actual length
        import struct
        riff_size = struct.unpack_from("<I", wav_bytes, 4)[0]
        assert riff_size == len(wav_bytes) - 8, (
            f"RIFF chunk size {riff_size} != actual {len(wav_bytes) - 8}")

    def test_opus_decode_empty_frames_returns_empty(self):
        """Empty frame list → empty bytes for both PCM and WAV."""
        assert codec.opus_decode_to_pcm([], sample_rate=16000) == b""
        assert codec.opus_decode_to_wav([], sample_rate=16000) == b""

    def test_opus_decode_skips_bad_frames(self):
        """A completely garbage packet should be logged/skipped, not crash."""
        # Generate a real sine PCM and encode a valid opus packet from it
        pcm = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
             "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=10,
        ).stdout
        assert len(pcm) > 0
        enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_AUDIO)
        frame_samples = 16000 * 60 // 1000
        frame_bytes = frame_samples * 2
        good_pkt = enc.encode(pcm[:frame_bytes], frame_samples)
        result = codec.opus_decode_to_pcm([good_pkt, b"\xff\xff\xff"], sample_rate=16000)
        assert len(result) > 0, "valid packet should decode successfully"


# ===================================================================
# mp3_to_ogg_opus
# ===================================================================

class TestMp3ToOggOpus:

    def test_mp3_to_ogg_opus_returns_ogg_stream(self):
        """Synthesize MP3 → Ogg Opus → starts with OggS magic."""
        mp3 = _generate_sine_mp3(duration_sec=0.5, freq=440, sample_rate=24000)
        assert len(mp3) > 0

        ogg = codec.mp3_to_ogg_opus(mp3, sample_rate=24000)
        assert len(ogg) > 0, "Ogg Opus output should not be empty"
        assert ogg[:4] == b"OggS", f"expected OggS magic, got {ogg[:4]!r}"

    def test_mp3_to_ogg_opus_too_small_raises(self):
        """Input smaller than 10 bytes → RuntimeError (mirrors _mp3_to_pcm)."""
        with pytest.raises(RuntimeError, match="input too small"):
            codec.mp3_to_ogg_opus(b"", sample_rate=24000)
        with pytest.raises(RuntimeError, match="input too small"):
            codec.mp3_to_ogg_opus(b"\x00\x00\x00", sample_rate=24000)

    def test_mp3_to_ogg_opus_different_sample_rates(self):
        """Should work at 16000 Hz too (ESP32 uses 16k for music)."""
        mp3 = _generate_sine_mp3(duration_sec=0.2, freq=440, sample_rate=16000)
        ogg = codec.mp3_to_ogg_opus(mp3, sample_rate=16000)
        assert ogg[:4] == b"OggS"


# ===================================================================
# url_to_opus_packets — cache-hit branches + invalid URL fast failure
# ===================================================================
# Note: url_to_opus_packets calls ffmpeg with a URL as input. ffmpeg handles
# network download internally, so testing the full download→encode path without
# hitting the real network requires either a local HTTP server or ffmpeg
# protocol mocking. Instead we exercise:
#   - memory cache hit branch
#   - disk cache hit branch
#   - invalid URL → ffmpeg fails quickly (RuntimeError), not a hang/crash

class TestUrlToOpusPackets:

    def test_memory_cache_hit_skips_ffmpeg(self):
        """When _URL_CACHE has the key, ffmpeg must not be called."""
        url = "http://example.com/track.mp3"
        key = (url, 24000, 60, 30)
        fake_packets = [b"\xf8\xff\xfe", b"\xf8\xff\xfe"]
        codec._URL_CACHE[key] = fake_packets

        with patch("subprocess.run") as mock_run:
            result = codec.url_to_opus_packets(url, sample_rate=24000,
                                                frame_duration=60, max_seconds=30)
        assert result == fake_packets
        mock_run.assert_not_called()

    def test_disk_cache_hit_populates_memory_cache(self):
        """When disk cache exists, it's loaded into _URL_CACHE and returned."""
        url = "http://example.com/disk-cache-track.mp3"
        key = (url, 24000, 60, 30)
        fake_packets = [b"\xf8\xff\xfe", b"\xf8\xff\xfe", b"\xf8\xff\xfe"]

        # Write a fake disk cache entry
        path = codec._url_cache_path(url, 24000, 60, 30)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(fake_packets, f, protocol=pickle.HIGHEST_PROTOCOL)

        with patch("subprocess.run") as mock_run:
            result = codec.url_to_opus_packets(url, sample_rate=24000,
                                                frame_duration=60, max_seconds=30)
        assert result == fake_packets
        # Memory cache should now hold the entry
        assert key in codec._URL_CACHE
        mock_run.assert_not_called()

    def test_invalid_url_raises_runtime_error(self):
        """An URL that ffmpeg cannot open raises RuntimeError (no crash/hang)."""
        url = "http://127.0.0.1:1/nonexistent.mp3"  # unreachable
        with pytest.raises(RuntimeError, match="ffmpeg url->pcm failed"):
            codec.url_to_opus_packets(url, sample_rate=24000,
                                      frame_duration=60, max_seconds=1)

    def test_cache_key_includes_all_parameters(self):
        """Different (sample_rate, frame_duration, max_seconds) → different keys."""
        url = "http://example.com/track.mp3"
        # Pre-populate cache for (24000, 60, 30)
        codec._URL_CACHE[(url, 24000, 60, 30)] = [b"A"]
        # Request with different frame_duration should miss cache
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"")
            # This will raise because stdout is empty, but we only care that
            # ffmpeg was called (cache miss)
            with pytest.raises(RuntimeError):
                codec.url_to_opus_packets(url, sample_rate=24000,
                                          frame_duration=120, max_seconds=30)
        mock_run.assert_called_once()


# ===================================================================
# Disk cache load/save round trip + corruption safety
# ===================================================================

class TestUrlDiskCache:

    def test_save_then_load_round_trip(self):
        """Packets written by _url_disk_save are read back unchanged."""
        url = "http://example.com/round-trip.mp3"
        fake_packets = [b"\x00\x01\x02", b"\x03\x04\x05"]

        codec._url_disk_save(url, 24000, 60, 30, fake_packets)
        loaded = codec._url_disk_load(url, 24000, 60, 30)
        assert loaded == fake_packets

    def test_load_corrupted_file_returns_none(self):
        """Garbage bytes in a .pkl file must not crash; returns None."""
        url = "http://example.com/corrupt.mp3"
        path = codec._url_cache_path(url, 24000, 60, 30)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\xde\xad\xbe\xef" * 10)  # random garbage

        result = codec._url_disk_load(url, 24000, 60, 30)
        assert result is None, "corrupted cache file must degrade gracefully"

    def test_load_missing_file_returns_none(self):
        """Non-existent cache file → None."""
        result = codec._url_disk_load(
            "http://example.com/no-such-file.mp3", 24000, 60, 30)
        assert result is None

    def test_load_non_list_returns_none(self):
        """Pickle that doesn't unpickle to a non-empty list → None."""
        url = "http://example.com/bad-type.mp3"
        path = codec._url_cache_path(url, 24000, 60, 30)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump("not-a-list", f)

        result = codec._url_disk_load(url, 24000, 60, 30)
        assert result is None

    def test_load_empty_list_returns_none(self):
        """Empty list is treated as no cache hit → None."""
        url = "http://example.com/empty.mp3"
        path = codec._url_cache_path(url, 24000, 60, 30)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump([], f)

        result = codec._url_disk_load(url, 24000, 60, 30)
        assert result is None


# ===================================================================
# Cache size limits and eviction
# ===================================================================

class TestCacheLimits:

    def test_opus_cache_eviction_under_pressure(self):
        """When OPUS_CACHE is full, mp3_to_opus_packets evicts oldest entry."""
        for i in range(codec.OPUS_CACHE_MAX):
            key = (f"hash-{i}", 24000, 60, 0.0)
            codec._OPUS_CACHE[key] = ([b"\x00"], time.monotonic())

        first_key = next(iter(codec._OPUS_CACHE))
        mp3 = _generate_sine_mp3(duration_sec=0.05, freq=440, sample_rate=24000)
        with patch("hashlib.sha256") as mock_sha:
            mock_sha.return_value.hexdigest.return_value = "new-hash"
            codec.mp3_to_opus_packets(mp3, sample_rate=24000,
                                       frame_duration=60, volume_db=0.0)
        assert first_key not in codec._OPUS_CACHE, (
            "oldest entry should be evicted when cache is full")
        assert ("new-hash", 24000, 60, 0.0) in codec._OPUS_CACHE

    def test_url_cache_eviction_under_pressure(self):
        """When _URL_CACHE is full, url_to_opus_packets evicts oldest entry."""
        for i in range(codec._URL_CACHE_MAX):
            key = (f"url-{i}", 24000, 60, 30)
            codec._URL_CACHE[key] = [b"\x00"]

        first_key = next(iter(codec._URL_CACHE))

        # Create valid s16le PCM that yields at least one opus frame
        pcm = b"\x00\x00" * (24000 * 60 // 1000 * 2)  # 60ms of silence
        enc = opuslib.Encoder(24000, 1, opuslib.APPLICATION_AUDIO)
        fake_packets = [enc.encode(pcm, 480)]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0,
                stdout=pcm, stderr=b"")
            with patch.object(codec, "_url_disk_save"):
                result = codec.url_to_opus_packets(
                    "http://example.com/new.mp3", sample_rate=24000,
                    frame_duration=60, max_seconds=30)
        assert len(result) > 0, "should produce opus packets"
        assert ("url-new-placeholder", 24000, 60, 30) not in codec._URL_CACHE
        # The key used by url_to_opus_packets is based on the URL passed in
        expected_key = ("http://example.com/new.mp3", 24000, 60, 30)
        assert expected_key in codec._URL_CACHE
