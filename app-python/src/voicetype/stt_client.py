"""Deepgram real-time STT WebSocket client.

Connects to the Deepgram v2/listen API, streams PCM16 audio,
and yields TranscriptionResult events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass, field
from typing import Callable

import websockets

log = logging.getLogger(__name__)

STT_URL = "wss://api.deepgram.com/v2/listen"


@dataclass
class WordInfo:
    word: str
    confidence: float


@dataclass
class TranscriptionResult:
    event: str
    turn_index: int
    start: float
    timestamp: float
    transcript: str
    words: list[WordInfo] = field(default_factory=list)
    end_of_turn_confidence: float = 0.0


class SttClient:
    """Streams audio to Deepgram and delivers transcription callbacks."""

    def __init__(self, url: str = STT_URL, sample_rate: int = 16000, api_key: str = ""):
        self.url = url
        self.sample_rate = sample_rate
        self.api_key = api_key

    async def run(
        self,
        audio_queue: asyncio.Queue[bytes | None],
        on_transcription: Callable[[TranscriptionResult], None],
    ) -> None:
        """Connect, stream audio from queue, deliver transcription events.

        Send None to audio_queue to signal end-of-stream.
        """
        ws_url = f"{self.url}?model=flux-general-en&sample_rate={self.sample_rate}&encoding=linear16"

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Token {self.api_key}"

        log.info("Connecting to STT: %s", ws_url)

        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            log.info("Connected to Deepgram")

            send_task = asyncio.create_task(self._send_audio(ws, audio_queue))
            recv_task = asyncio.create_task(self._recv_messages(ws, on_transcription))

            try:
                done, pending = await asyncio.wait(
                    [send_task, recv_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Propagate exceptions
                for t in done:
                    t.result()
            except asyncio.CancelledError:
                send_task.cancel()
                recv_task.cancel()
                raise

    async def _send_audio(
        self, ws: websockets.ClientConnection, audio_queue: asyncio.Queue[bytes | None]
    ) -> None:
        try:
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    break
                await ws.send(chunk)
        finally:
            # Tell Deepgram no more audio
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
                log.debug("Sent CloseStream")
            except Exception:
                pass

    async def _recv_messages(
        self, ws: websockets.ClientConnection, on_transcription: Callable[[TranscriptionResult], None]
    ) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                log.warning("Unexpected binary message from server")
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.error("Invalid JSON from server: %s", raw[:200])
                continue

            msg_type = msg.get("type", "")

            if msg_type == "Connected":
                log.info("STT connected: request_id=%s", msg.get("request_id"))

            elif msg_type == "Configuration":
                log.debug("STT configuration ack: %s", msg)

            elif msg_type == "Error":
                log.error("STT error [%s]: %s", msg.get("code"), msg.get("description"))
                raise RuntimeError(f"Deepgram error: {msg.get('code')} - {msg.get('description')}")

            elif msg_type == "TurnInfo":
                words = [
                    WordInfo(word=w["word"], confidence=w.get("confidence", 0.0))
                    for w in msg.get("words", [])
                ]
                result = TranscriptionResult(
                    event=msg.get("event", ""),
                    turn_index=msg.get("turn_index", 0),
                    start=msg.get("audio_window_start", 0.0),
                    timestamp=msg.get("audio_window_end", 0.0),
                    transcript=msg.get("transcript", ""),
                    words=words,
                    end_of_turn_confidence=msg.get("end_of_turn_confidence", 0.0),
                )
                on_transcription(result)

            else:
                log.debug("Unknown message type: %s", msg_type)


def samples_to_pcm16(samples: list[float] | memoryview) -> bytes:
    """Convert float32 samples [-1.0, 1.0] to 16-bit PCM bytes."""
    buf = bytearray(len(samples) * 2)
    for i, s in enumerate(samples):
        clamped = max(-1.0, min(1.0, s))
        val = int(clamped * 32767)
        struct.pack_into("<h", buf, i * 2, val)
    return bytes(buf)
