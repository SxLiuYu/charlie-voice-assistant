"""Xiaozhi-compatible WebSocket endpoint.

Speaks the xiaozhi wire protocol:
- JSON text frames for control (hello/listen/tts/stt/abort/ping)
- Binary frames for raw Opus audio packets

Reuses Charlie's existing ASR → Brain → TTS pipeline internally.
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.audio import likely_empty_audio
from app.xiaozhi_codec import opus_decode_to_wav, mp3_to_opus_packets

log = logging.getLogger(__name__)

DOWNLINK_SAMPLE_RATE = 24000
UPLINK_SAMPLE_RATE = 16000
OPUS_FRAME_DURATION_MS = 60


def register_xiaozhi_routes(app: FastAPI):
    """Register the /ws/xiaozhi endpoint on the given FastAPI app."""

    @app.websocket("/ws/xiaozhi")
    async def xiaozhi_websocket(ws: WebSocket):
        await ws.accept()
        log.info("[xiaozhi] client connected from %s", ws.client)

        session_id = uuid.uuid4().hex[:16]
        opus_frames: list[bytes] = []
        stream_task: Optional[asyncio.Task] = None
        listening = False

        async def cancel_stream():
            nonlocal stream_task
            if stream_task and not stream_task.done():
                stream_task.cancel()
                try:
                    await stream_task
                except (asyncio.CancelledError, Exception):
                    pass
            stream_task = None

        async def send_json(obj: dict):
            try:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

        async def stream_response(text: str):
            """Run brain + TTS, sending xiaozhi-protocol messages and Opus audio."""
            from voice_agent import brain_stream_sentences, _tts_cleaned_to_mp3

            await send_json({"type": "tts", "state": "start"})

            loop = asyncio.get_running_loop()

            def brain_worker():
                sentences = []
                try:
                    for sentence, _full in brain_stream_sentences(
                        text, session_id="xiaozhi-" + session_id
                    ):
                        if sentence and sentence != "__MUSIC__" and sentence != "__MUSIC_STOP__":
                            sentences.append(sentence)
                except Exception as e:
                    log.error("[xiaozhi] brain error: %s", e)
                return sentences

            sentences = await loop.run_in_executor(None, brain_worker)

            for sentence in sentences:
                if ws.client_state != WebSocketState.CONNECTED:
                    break

                await send_json({
                    "type": "tts",
                    "state": "sentence_start",
                    "text": sentence,
                })

                try:
                    mp3_bytes = await loop.run_in_executor(
                        None, _tts_cleaned_to_mp3, sentence
                    )
                except Exception as e:
                    log.warning("[xiaozhi] TTS failed for sentence: %s", e)
                    continue

                if not mp3_bytes or len(mp3_bytes) < 100:
                    continue

                try:
                    opus_pkts = await loop.run_in_executor(
                        None,
                        mp3_to_opus_packets,
                        mp3_bytes,
                        DOWNLINK_SAMPLE_RATE,
                        OPUS_FRAME_DURATION_MS,
                    )
                except Exception as e:
                    log.warning("[xiaozhi] Opus encode failed: %s", e)
                    continue

                for pkt in opus_pkts:
                    if ws.client_state != WebSocketState.CONNECTED:
                        break
                    try:
                        await ws.send_bytes(pkt)
                    except Exception:
                        break

            await send_json({"type": "tts", "state": "stop"})

        try:
            while True:
                msg = await ws.receive()

                if msg["type"] == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"] is not None:
                    if listening:
                        opus_frames.append(msg["bytes"])
                    continue

                text = msg.get("text")
                if not text:
                    continue

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue

                mtype = data.get("type", "")

                if mtype == "hello":
                    await send_json({
                        "type": "hello",
                        "transport": "websocket",
                        "session_id": session_id,
                        "audio_params": {
                            "sample_rate": DOWNLINK_SAMPLE_RATE,
                            "frame_duration": OPUS_FRAME_DURATION_MS,
                        },
                    })
                    log.info("[xiaozhi] hello handshake done, session=%s", session_id)

                elif mtype == "listen":
                    state = data.get("state", "")
                    if state == "detect":
                        wake = data.get("text", "unknown")
                        log.info("[xiaozhi] wake word: %s", wake)
                        await cancel_stream()
                    elif state == "start":
                        listening = True
                        opus_frames = []
                        log.info("[xiaozhi] listening started")
                    elif state == "stop":
                        listening = False
                        log.info("[xiaozhi] listening stopped, %d opus frames", len(opus_frames))

                        if not opus_frames:
                            continue

                        try:
                            wav = opus_decode_to_wav(opus_frames, UPLINK_SAMPLE_RATE)
                        except Exception as e:
                            log.error("[xiaozhi] opus decode failed: %s", e)
                            opus_frames = []
                            continue
                        opus_frames = []

                        if not wav or likely_empty_audio(wav):
                            log.info("[xiaozhi] empty audio, skipping ASR")
                            stream_task = asyncio.create_task(
                                stream_response("（没有听到声音，请再说一次）")
                            )
                            continue

                        from voice_agent import asr
                        try:
                            asr_text = await asyncio.wait_for(
                                asyncio.to_thread(asr, wav, "wav"), timeout=30
                            )
                        except asyncio.TimeoutError:
                            log.error("[xiaozhi] ASR timeout")
                            await send_json({"type": "tts", "state": "start"})
                            await send_json({
                                "type": "tts",
                                "state": "sentence_start",
                                "text": "语音识别超时，请稍后再试",
                            })
                            await send_json({"type": "tts", "state": "stop"})
                            continue

                        asr_text = (asr_text or "").strip()
                        log.info("[xiaozhi] ASR: %s", asr_text)

                        if not asr_text:
                            stream_task = asyncio.create_task(
                                stream_response("我没有听清楚，能再说一遍吗？")
                            )
                            continue

                        await send_json({"type": "stt", "text": asr_text})
                        stream_task = asyncio.create_task(stream_response(asr_text))

                elif mtype == "abort":
                    log.info("[xiaozhi] abort received")
                    await cancel_stream()
                    await send_json({"type": "tts", "state": "stop"})

                elif mtype == "ping":
                    await send_json({"type": "pong"})

                elif mtype == "pong":
                    pass

        except WebSocketDisconnect:
            log.info("[xiaozhi] client disconnected")
        except Exception as e:
            log.error("[xiaozhi] error: %s", e, exc_info=True)
        finally:
            await cancel_stream()
            log.info("[xiaozhi] session %s cleaned up", session_id)
