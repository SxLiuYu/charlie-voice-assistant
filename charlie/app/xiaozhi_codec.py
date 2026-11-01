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
# opuslib.Encoder.encode() 在 Windows 上并发调用会 segfault（进程直接死，
# 实测 keep-hot 音乐预热 + TTS 同帧编码）。编码全程串行。
_OPUS_ENC_LOCK = threading.Lock()
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


def _resample_pcm(pcm, src_sr: int, dst_sr: int):
    """线性插值重采样 s16 mono。src==dst 时原样返回。"""
    if src_sr == dst_sr or pcm is None or len(pcm) == 0:
        return pcm
    try:
        import numpy as np
    except ImportError:
        return pcm
    n_src = len(pcm)
    n_dst = int(round(float(n_src) * dst_sr / src_sr))
    if n_dst <= 0:
        return pcm
    x_src = np.arange(n_src, dtype=np.float64)
    x_dst = np.linspace(0.0, float(n_src - 1), n_dst, dtype=np.float64)
    out = np.interp(x_dst, x_src, pcm.astype(np.float64))
    return np.clip(out, -32768, 32767).astype(np.int16)


def _trim_edge_silence(pcm, sample_rate: int, lead: float = 0.10,
                       trail: float = 0.18):
    """裁掉 TTS 首尾静音，只留 lead/trail 秒的余量。

    nmcloud TTS 每句自带 ~0.12s 起始静音 + ~0.24–0.30s 结尾静音，
    3 个字的"贾维斯"总长仍有 1.19s。这些静音对设备只是白等，
    按句累加（多句播报）就是几百毫秒纯延迟。
    """
    try:
        import numpy as np
    except ImportError:
        return pcm
    if pcm is None or len(pcm) == 0:
        return pcm
    frame = max(1, int(sample_rate * 0.02))  # 20ms 窗
    n = len(pcm) // frame * frame
    if n <= 0:
        return pcm
    blocks = np.asarray(pcm[:n], dtype=np.float32).reshape(-1, frame)
    rms = np.sqrt((blocks ** 2).mean(axis=1))
    peak = float(rms.max()) if rms.size else 0.0
    if peak <= 0:
        return pcm
    # 门限取峰值的 3%，并至少抬到 60：房间底噪级别的尾巴不算语音
    thr = max(60.0, peak * 0.03)
    voiced = np.nonzero(rms > thr)[0]
    if voiced.size == 0:
        return pcm
    lead_frames = int(lead / 0.02)
    trail_frames = int(trail / 0.02)
    start = max(0, (int(voiced[0]) - lead_frames)) * frame
    end = min(len(pcm), (int(voiced[-1]) + 1 + trail_frames) * frame)
    if end - start < frame or end - start > len(pcm):
        return pcm
    trimmed = end - start
    # 裁得超过一半就不正常（可能门限偏高误杀），保守放弃
    if trimmed < len(pcm) * 0.4:
        return pcm
    return pcm[start:end]


def _mp3_to_pcm(mp3_data: bytes, sample_rate: int = 24000,
                volume_db: float = 0.0) -> bytes:
    """MP3/WAV bytes → 16-bit mono PCM at given sample rate.

    优先 soundfile 纯解码（不走子进程）：服务进程里 ffmpeg 8.0 pipe
    偶发 0xC0000002 崩溃（rc=3221225794），TTS 音频一个字节都发不出去，
    表现为"设备听到了却不说话"。soundfile ~12ms 且无子进程。
    失败再落 ffmpeg 临时文件（不用 pipe:0/pipe:1）。
    """
    if not mp3_data or len(mp3_data) < 10:
        raise RuntimeError(f"ffmpeg mp3->pcm failed: input too small ({len(mp3_data) if mp3_data else 0} bytes)")

    # 1) soundfile：mp3/wav 都能解，不依赖 ffmpeg
    try:
        import io as _io
        import soundfile as _sf
        pcm, sr = _sf.read(_io.BytesIO(mp3_data), dtype="int16", always_2d=False)
        if pcm.ndim > 1:
            pcm = pcm[:, 0]
        pcm = _resample_pcm(pcm, int(sr), sample_rate)
        pcm = _trim_edge_silence(pcm, sample_rate)
        if volume_db:
            try:
                import numpy as _np
                pcm = _np.clip(
                    pcm.astype(_np.float32) * (10.0 ** (volume_db / 20.0)),
                    -32768, 32767).astype(_np.int16)
            except Exception:
                pass
        if len(pcm) > 0:
            return pcm.tobytes()
    except Exception as e:
        log.debug("soundfile decode fail, fallback ffmpeg: %s", e)

    # 2) ffmpeg 临时文件（不用 pipe，规避 0xC0000002 / rc=69）
    import tempfile
    tmp_in = tmp_out = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".bin")
        fd_out, tmp_out = tempfile.mkstemp(suffix=".pcm")
        os.close(fd_in)
        os.close(fd_out)
        with open(tmp_in, "wb") as f:
            f.write(mp3_data)
        cmd = ["ffmpeg", "-y", "-i", tmp_in, "-ar", str(sample_rate), "-ac", "1"]
        if volume_db:
            cmd += ["-af", f"volume={volume_db}dB"]
        cmd += ["-f", "s16le", tmp_out]
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        if r.returncode == 0:
            with open(tmp_out, "rb") as f:
                out = f.read()
            if out:
                return out
        stderr = r.stderr.decode(errors="replace")
        err_lines = [ln for ln in stderr.split("\n")
                     if ln.strip() and "ffmpeg version" not in ln
                     and "built with" not in ln and "configuration:" not in ln]
        err_summary = " | ".join(err_lines[:3])[:300]
        raise RuntimeError(
            f"ffmpeg mp3->pcm failed (rc={r.returncode}, in={len(mp3_data)}B, "
            f"out={os.path.getsize(tmp_out) if os.path.exists(tmp_out) else 0}B): {err_summary}")
    finally:
        for p in (tmp_in, tmp_out):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


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
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    packets: list[bytes] = []
    offset = 0
    with _OPUS_ENC_LOCK:
        encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
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
    # 网易云外链对 ffmpeg 默认 UA 常见 403/断流；先 requests 带 UA 下载再喂 ffmpeg
    audio = b""
    try:
        import requests as _rq
        resp = _rq.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://music.163.com/",
        }, timeout=15, allow_redirects=True)
        if resp.ok:
            audio = resp.content or b""
    except Exception as e:
        log.warning("[codec] url download fail %.80s: %s", url, e)
    looks_audio = bool(audio) and (
        len(audio) > 4096
        and (audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3")
             or audio[:4] == b"RIFF" or audio[:4] == b"fLaC")
        or "audio" in (resp.headers.get("Content-Type", "") if audio else ""))
    if looks_audio:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", str(sample_rate), "-ac", "1",
             "-t", str(max_seconds), "-f", "s16le", "pipe:1"],
            input=audio, capture_output=True, timeout=60)
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", url, "-ar", str(sample_rate), "-ac", "1",
             "-t", str(max_seconds), "-f", "s16le", "pipe:1"],
            input=b"", capture_output=True, timeout=60,
        )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(
            f"ffmpeg url->pcm failed: {r.stderr.decode(errors='replace')[:200]}")
    pcm = r.stdout
    frame_samples = sample_rate * frame_duration // 1000
    frame_bytes = frame_samples * 2
    packets: list[bytes] = []
    offset = 0
    with _OPUS_ENC_LOCK:
        encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
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
