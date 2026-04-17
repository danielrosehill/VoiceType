"""Deepgram real-time STT WebSocket client.

Supports two backends:
- Flux (v2/listen): turn-based, voice-agent oriented. Emits TurnInfo events.
- Nova-3 (v1/listen): continuous streaming, higher accuracy, supports
  keyterm prompting. Emits Results + UtteranceEnd events.

Both are normalized into TranscriptionResult callbacks. The `event` field
is "Update" for in-progress transcripts and "EndOfTurn" when the utterance
is finalized.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

import websockets

log = logging.getLogger(__name__)

FLUX_URL = "wss://api.deepgram.com/v2/listen"
NOVA_URL = "wss://api.deepgram.com/v1/listen"


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

    def __init__(
        self,
        sample_rate: int = 16000,
        api_key: str = "",
        model: str = "nova-3",
        keyterms: list[str] | None = None,
    ):
        self.sample_rate = sample_rate
        self.api_key = api_key
        self.model = model
        self.keyterms = keyterms or []

    @property
    def is_nova(self) -> bool:
        return self.model.startswith("nova")

    def _build_url(self) -> str:
        if self.is_nova:
            params: list[tuple[str, str]] = [
                ("model", self._nova_model_id()),
                ("encoding", "linear16"),
                ("sample_rate", str(self.sample_rate)),
                ("interim_results", "true"),
                ("smart_format", "true"),
                ("utterance_end_ms", "1200"),
                ("vad_events", "true"),
            ]
            lang = self._nova_language()
            if lang:
                params.append(("language", lang))
            for kt in self.keyterms:
                kt = kt.strip()
                if kt:
                    params.append(("keyterm", kt))
            return f"{NOVA_URL}?{urllib.parse.urlencode(params)}"
        # Flux
        return (
            f"{FLUX_URL}?model={self.model}"
            f"&sample_rate={self.sample_rate}&encoding=linear16"
        )

    def _nova_model_id(self) -> str:
        # Our UI exposes "nova-3" and "nova-3-multi" — Deepgram uses bare
        # "nova-3" for both and a separate `language=multi` switch.
        return "nova-3"

    def _nova_language(self) -> str:
        if self.model == "nova-3-multi":
            return "multi"
        return "en"

    async def run(
        self,
        audio_queue: asyncio.Queue[bytes | None],
        on_transcription: Callable[[TranscriptionResult], None],
    ) -> None:
        """Connect, stream audio, deliver transcription events.

        Send None to audio_queue to signal end-of-stream.
        """
        ws_url = self._build_url()

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
            log.info("Connected to Deepgram (%s)", "Nova-3" if self.is_nova else "Flux")

            send_task = asyncio.create_task(self._send_audio(ws, audio_queue))
            if self.is_nova:
                recv_task = asyncio.create_task(self._recv_nova(ws, on_transcription))
            else:
                recv_task = asyncio.create_task(self._recv_flux(ws, on_transcription))

            try:
                done, pending = await asyncio.wait(
                    [send_task, recv_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
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
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
                log.debug("Sent CloseStream")
            except Exception:
                pass

    # ── Flux (v2/listen) ─────────────────────────────────────────────

    async def _recv_flux(
        self,
        ws: websockets.ClientConnection,
        on_transcription: Callable[[TranscriptionResult], None],
    ) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            if msg_type == "Error":
                raise RuntimeError(
                    f"Deepgram error: {msg.get('code')} - {msg.get('description')}"
                )
            if msg_type != "TurnInfo":
                continue

            words = [
                WordInfo(word=w["word"], confidence=w.get("confidence", 0.0))
                for w in msg.get("words", [])
            ]
            on_transcription(
                TranscriptionResult(
                    event=msg.get("event", ""),
                    turn_index=msg.get("turn_index", 0),
                    start=msg.get("audio_window_start", 0.0),
                    timestamp=msg.get("audio_window_end", 0.0),
                    transcript=msg.get("transcript", ""),
                    words=words,
                    end_of_turn_confidence=msg.get("end_of_turn_confidence", 0.0),
                )
            )

    # ── Nova-3 (v1/listen) ───────────────────────────────────────────

    async def _recv_nova(
        self,
        ws: websockets.ClientConnection,
        on_transcription: Callable[[TranscriptionResult], None],
    ) -> None:
        # Nova-3 sends Results messages with partial & finalized segments.
        # Final segments need to be accumulated until speech_final / UtteranceEnd
        # so the user sees the full sentence being built up in real time.
        finalized_prefix = ""  # committed text across is_final=true segments
        turn_index = 0

        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "Error":
                raise RuntimeError(
                    f"Deepgram error: {msg.get('code')} - {msg.get('description')}"
                )

            if msg_type == "Results":
                alt = (
                    msg.get("channel", {})
                    .get("alternatives", [{}])[0]
                )
                segment = alt.get("transcript", "") or ""
                is_final = bool(msg.get("is_final", False))
                speech_final = bool(msg.get("speech_final", False))

                # Full text to display = already-finalized + current segment
                combined = _join(finalized_prefix, segment)

                if is_final:
                    # Commit this segment to the finalized prefix
                    finalized_prefix = combined
                    if speech_final:
                        # End of utterance — finalize and reset
                        on_transcription(
                            TranscriptionResult(
                                event="EndOfTurn",
                                turn_index=turn_index,
                                start=0.0,
                                timestamp=0.0,
                                transcript=combined,
                                end_of_turn_confidence=alt.get("confidence", 0.0),
                            )
                        )
                        finalized_prefix = ""
                        turn_index += 1
                    else:
                        # Intermediate finalization — keep building the turn
                        on_transcription(
                            TranscriptionResult(
                                event="Update",
                                turn_index=turn_index,
                                start=0.0,
                                timestamp=0.0,
                                transcript=combined,
                            )
                        )
                else:
                    # Interim partial — replaces current tail
                    on_transcription(
                        TranscriptionResult(
                            event="Update",
                            turn_index=turn_index,
                            start=0.0,
                            timestamp=0.0,
                            transcript=combined,
                        )
                    )

            elif msg_type == "UtteranceEnd":
                if finalized_prefix:
                    on_transcription(
                        TranscriptionResult(
                            event="EndOfTurn",
                            turn_index=turn_index,
                            start=0.0,
                            timestamp=0.0,
                            transcript=finalized_prefix,
                        )
                    )
                    finalized_prefix = ""
                    turn_index += 1


def _join(prefix: str, segment: str) -> str:
    if not prefix:
        return segment
    if not segment:
        return prefix
    if prefix.endswith(" ") or segment.startswith(" "):
        return prefix + segment
    return prefix + " " + segment


def samples_to_pcm16(samples: list[float] | memoryview) -> bytes:
    """Convert float32 samples [-1.0, 1.0] to 16-bit PCM bytes."""
    buf = bytearray(len(samples) * 2)
    for i, s in enumerate(samples):
        clamped = max(-1.0, min(1.0, s))
        val = int(clamped * 32767)
        struct.pack_into("<h", buf, i * 2, val)
    return bytes(buf)
