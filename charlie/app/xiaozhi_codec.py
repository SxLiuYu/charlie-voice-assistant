"""Opus codec utilities for xiaozhi protocol: decode uplink Opus frames to WAV,
and encode downlink PCM to Opus packets. Uses opuslib (libopus ctypes binding)."""

import io
import os
import pickle
import hashlib
import threading
import time
import wave
import subprocess
import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import opuslib
except ImportError:
    opuslib = None
    log.warning("opuslib not installed; xiaozhi Opus codec unavailable")

# Opus packet cache: key → (packets, last-used monotonic timestamp).
# Timestamps drive TTL-based eviction so volume-boost variants / stale mp3
# hashes don't accumulate forever.
_OPUS_CACHE: dict[tuple, tuple] = {}
# Keyed by (url, sample_rate, frame_duration, max_seconds) → list[bytes]
_URL_CACHE: dict[tuple, list] = {}
_OPUS_CACHE_LOCK = threading.Lock()
OPUS_CACHE_MAX = 64
OPUS_CACHE_TTL = 600.0   # drop entries unused for >10 min
_URL_CACHE_MAX = 16
# Hit counters (stats endpoint)
_stats_opus_hits = 0
_stats_url_mem_hits = 0
_stats_url_disk_hits = 0
# Persistent URL→Opus cache on disk so commonly-played tracks skip ffmpeg
# + download after a service restart. One file per sha256(url) in this dir.
_URL_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "url_opus_cache")
_URL_DISK_MAX = 32


def _url_cache_path(url: str, sample_rate: int, frame_duration: int,
                    max_seconds: int) -> str:
    key = f"{sample_rate}.{frame_duration}.{max_seconds}." + \
        hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(_URL_CACHE_DIR, key + ".pkl")


def _url_disk_load(url: str, sample_rate: int, frame_duration: int,
                   max_seconds: int) -> Optional[list]:
    """Load a previously persisted URL→Opus cache entry (if any)."""
    path = _url_cache_path(url, sample_rate, frame_duration, max_seconds)
    try:
        with open(path, "rb") as f:
            pkts = pickle.load(f)
        if not isinstance(pkts, list) or not pkts:
            return None
        return list(pkts)
    except Exception as e:
        log.debug(f"[app.xiaozhi_codec] error at line 61: {e}")
        return None


def _url_disk_save(url: str, sample_rate: int, frame_duration: int,
                   max_seconds: int, packets: list) -> None:
    """Persist a URL→Opus list to disk and trim the dirs to _URL_DISK_MAX."""
    try:
        os.makedirs(_URL_CACHE_DIR, exist_ok=True)
        path = _url_cache_path(url, sample_rate, frame_duration, max_seconds)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(packets, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        try:
            entries = sorted(
                os.path.join(_URL_CACHE_DIR, n)
                for n in os.listdir(_URL_CACHE_DIR) if n.endswith(".pkl"))
            while len(entries) > _URL_DISK_MAX:
                os.remove(entries.pop(0))
        except OSError:
            pass
    except Exception as e:
        log.debug(f"[app.xiaozhi_codec] error at line 83: {e}")
        pass


def _cache_stats() -> dict:
    """Expose cache sizes/hit bookkeeping for the /api/xiaozhi/status endpoint."""
    global _stats_opus_hits, _stats_url_mem_hits, _stats_url_disk_hits
    with _OPUS_CACHE_LOCK:
        return {
            "opus_packets_cached": len(_OPUS_CACHE),
            "url_packets_cached_mem": len(_URL_CACHE),
            "url_disk_pruned_to": _URL_DISK_MAX,
            "opus_cache_hits": _stats_opus_hits,
            "url_cache_mem_hits": _stats_url_mem_hits,
            "url_cache_disk_hits": _stats_url_disk_hits,
        }


def opus_decode_to_wav(frames: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decode a list of raw Opus packets into a 16-bit mono WAV byte string."""
    if opuslib is None:
        raise RuntimeError("opuslib not available")
    if not frames:
        return b""
    decoder = opuslib.Decoder(sample_rate, 1)
    pcm_parts: list[bytes] = []
    frame_size = sample_rate * 60 // 1000
    for pkt in frames:
        try:
            pcm = decoder.decode(pkt, frame_size=frame_size)
            pcm_parts.append(pcm)
        except Exception as e:
            log.warning("opus decode frame error: %s", e)
    pcm_data = b"".join(pcm_parts)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)
    return buf.getvalue()


def opus_decode_to_pcm(frames: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decode a list of raw Opus packets into raw 16-bit mono PCM bytes (no WAV header)."""
    if opuslib is None:
        raise RuntimeError("opuslib not available")
    if not frames:
        return b""
    decoder = opuslib.Decoder(sample_rate, 1)
    pcm_parts: list[bytes] = []
    frame_size = sample_rate * 60 // 1000
    for pkt in frames:
        try:
            pcm = decoder.decode(pkt, frame_size=frame_size)
            pcm_parts.append(pcm)
        except Exception as e:
            log.warning("opus decode frame error: %s", e)
    return b"".join(pcm_parts)


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit mono PCM bytes with a WAV header."""
    if not pcm_data:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)
    return buf.getvalue()


def _mp3_to_pcm(mp3_data: bytes, sample_rate: int = 24000,
                volume_db: float = 0.0) -> bytes:
    """Convert MP3 bytes to raw 16-bit mono PCM at given sample rate via ffmpeg."""
    if not mp3_data or len(mp3_data) < 10:
        raise RuntimeError(f"ffmpeg mp3->pcm failed: input too small ({len(mp3_data) if mp3_data else 0} bytes)")
    # 不强制 -f mp3：ffmpeg 8.x 对某些 TTS 输出强制格式会失败（rc=69），
    # auto-probe 反而更可靠且省去一次失败重试（~2s → ~0.3s）。
    cmd = ["ffmpeg", "-y", "-i", "pipe:0",
           "-ar", str(sample_rate), "-ac", "1"]
    if volume_db:
        cmd += ["-af", f"volume={volume_db}dB"]
    cmd += ["-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, input=mp3_data, capture_output=True, timeout=25)
    if r.returncode == 0 and r.stdout:
        return r.stdout
    stderr = r.stderr.decode(errors='replace')
    err_lines = [l for l in stderr.split('\n')
                 if l.strip() and 'ffmpeg version' not in l
                 and 'built with' not in l and 'configuration:' not in l]
    err_summary = ' | '.join(err_lines[:3])[:300]
    raise RuntimeError(
        f"ffmpeg mp3->pcm failed (rc={r.returncode}, in={len(mp3_data)}B, "
        f"out={len(r.stdout)}B): {err_summary}")


def mp3_to_opus_packets(mp3_data: bytes, sample_rate: int = 24000,
                        frame_duration: int = 60,
                        volume_db: float = 0.0) -> list[bytes]:
    """Convert MP3 bytes to a list of raw Opus packets at given sample rate.

    volume_db (if non-zero) boosts output loudness via `af volume`, useful
    when the device answers in a noisy room.

    Result is cached keyed on (hash, sample_rate, frame_duration, volume) so
    repeated TTS (cached mp3) skips ffmpeg + opus encode entirely. Cache
    access is guarded by a lock (multiple devices / concurrent streams).
    """
    if opuslib is None:
        raise RuntimeError("opuslib not available")
    if not mp3_data:
        return []
    if len(mp3_data) < 10:
        # 与 _mp3_to_pcm 的输入校验对齐：过小输入在下层必然失败，提前抛出
        raise RuntimeError(
            f"ffmpeg mp3->opus failed: input too small ({len(mp3_data)} bytes)")
    key = (hashlib.sha256(mp3_data).hexdigest(), sample_rate,
           frame_duration, volume_db)
    with _OPUS_CACHE_LOCK:
        hit = _OPUS_CACHE.get(key)
        if hit is not None:
            global _stats_opus_hits
            _stats_opus_hits += 1
            packets, _ts = hit
            _OPUS_CACHE[key] = (packets, time.monotonic())
            log.info("[codec] opus cache hit (%d packets)", len(packets))
            return list(packets)
    pcm = _mp3_to_pcm(mp3_data, sample_rate, volume_db)
    encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    packets: list[bytes] = []
    offset = 0
    while offset + frame_bytes <= len(pcm):
        chunk = pcm[offset:offset + frame_bytes]
        try:
            pkt = encoder.encode(chunk, frame_samples)
            packets.append(pkt)
        except Exception as e:
            log.warning("opus encode error: %s", e)
        offset += frame_bytes
    if packets:
        with _OPUS_CACHE_LOCK:
            now = time.monotonic()
            # Evict oldest-by-TTL under cap pressure; drop entries cold for TTL.
            if len(_OPUS_CACHE) >= OPUS_CACHE_MAX:
                _OPUS_CACHE.pop(next(iter(_OPUS_CACHE)), None)
            stale = [k for k, (_, ts) in _OPUS_CACHE.items()
                     if now - ts > OPUS_CACHE_TTL]
            for k in stale:
                _OPUS_CACHE.pop(k, None)
            _OPUS_CACHE[key] = (packets, now)
    return packets


def url_to_opus_packets(url: str, sample_rate: int = 24000,
                        frame_duration: int = 60, max_seconds: int = 30) -> list[bytes]:
    """Download an audio URL (m4a/aac/mp3) and produce Opus packets.

    Used to play music sent as __MUSIC__<url> on the ESP32 device. The
    firmware decodes each WS binary message as one Opus packet, so the whole
    song is sliced into per-frame Opus packets (same path as TTS).

    Results are cached per URL so repeated plays (or multiple devices playing
    the same track) don't re-download / re-encode.
    """
    if opuslib is None:
        raise RuntimeError("opuslib not available")
    key = (url, sample_rate, frame_duration, max_seconds)
    with _OPUS_CACHE_LOCK:
        hit = _URL_CACHE.get(key)
        if hit is not None:
            global _stats_url_mem_hits
            _stats_url_mem_hits += 1
            log.info("[codec] url cache hit %d packets: %.60s", len(hit), url)
            return list(hit)
    disk = _url_disk_load(url, sample_rate, frame_duration, max_seconds)
    if disk:
        global _stats_url_disk_hits
        _stats_url_disk_hits += 1
        with _OPUS_CACHE_LOCK:
            _URL_CACHE[key] = disk
        log.info("[codec] url disk cache hit %d packets: %.60s", len(disk), url)
        return list(disk)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", url, "-ar", str(sample_rate), "-ac", "1",
         "-t", str(max_seconds), "-f", "s16le", "pipe:1"],
        input=b"", capture_output=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(
            f"ffmpeg url->pcm failed: {r.stderr.decode(errors='replace')[:200]}")
    pcm = r.stdout
    encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    packets: list[bytes] = []
    offset = 0
    while offset + frame_bytes <= len(pcm):
        chunk = pcm[offset:offset + frame_bytes]
        try:
            packets.append(encoder.encode(chunk, frame_samples))
        except Exception as e:
            log.warning("opus encode error: %s", e)
        offset += frame_bytes
    if packets:
        with _OPUS_CACHE_LOCK:
            if len(_URL_CACHE) >= _URL_CACHE_MAX:
                _URL_CACHE.pop(next(iter(_URL_CACHE)))
            _URL_CACHE[key] = packets
        _url_disk_save(url, sample_rate, frame_duration, max_seconds, packets)
    return packets


def mp3_to_ogg_opus(mp3_data: bytes, sample_rate: int = 16000) -> bytes:
    """Convert MP3 bytes to a complete Ogg Opus file/stream.

    The xiaozhi v2.1.0 firmware expects Ogg-framed Opus (it parses OpusHead/
    OpusTags and OggS pages), not raw Opus packets. ffmpeg produces a valid
    Ogg Opus stream directly.
    """
    if not mp3_data or len(mp3_data) < 10:
        raise RuntimeError(
            f"ffmpeg mp3->ogg opus failed: input too small "
            f"({len(mp3_data) if mp3_data else 0} bytes)")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
         "-ar", str(sample_rate), "-ac", "1",
         "-c:a", "libopus", "-b:a", "32k",
         "-f", "ogg", "pipe:1"],
        input=mp3_data, capture_output=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(
            f"ffmpeg mp3->ogg opus failed: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout
