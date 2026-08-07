"""Opus codec utilities for xiaozhi protocol: decode uplink Opus frames to WAV,
and encode downlink PCM to Opus packets. Uses opuslib (libopus ctypes binding)."""

import io
import wave
import subprocess
import logging

log = logging.getLogger(__name__)

try:
    import opuslib
except ImportError:
    opuslib = None
    log.warning("opuslib not installed; xiaozhi Opus codec unavailable")


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


def _mp3_to_pcm(mp3_data: bytes, sample_rate: int = 24000) -> bytes:
    """Convert MP3 bytes to raw 16-bit mono PCM at given sample rate via ffmpeg."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
         "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "pipe:1"],
        input=mp3_data, capture_output=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"ffmpeg mp3->pcm failed: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout


def mp3_to_opus_packets(mp3_data: bytes, sample_rate: int = 24000,
                        frame_duration: int = 60) -> list[bytes]:
    """Convert MP3 bytes to a list of raw Opus packets at given sample rate."""
    if opuslib is None:
        raise RuntimeError("opuslib not available")
    pcm = _mp3_to_pcm(mp3_data, sample_rate)
    encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
    encoder.bitrate = 24000
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
    return packets
