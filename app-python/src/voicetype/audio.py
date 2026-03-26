"""Audio capture with optional TEN VAD gating.

Captures audio from the default input device, runs VAD on each chunk,
and only enqueues speech frames to the STT queue.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# Audio config — Deepgram expects 16kHz mono PCM16
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 2560  # 160ms at 16kHz — matches Rust version's chunk duration

# VAD config
VAD_HOP_SIZE = 256
VAD_THRESHOLD = 0.5
# Keep sending keepalive silence every N seconds to prevent WS timeout
KEEPALIVE_INTERVAL_SECS = 8.0

# Try to import TEN VAD
try:
    from ten_vad import TenVad
    _TEN_VAD_AVAILABLE = True
except ImportError:
    TenVad = None
    _TEN_VAD_AVAILABLE = False
    log.info("ten-vad not installed — VAD disabled, all audio will be sent")


class AudioCapture:
    """Captures mic audio and pushes PCM16 bytes to an asyncio queue.

    If TEN VAD is available, only speech frames are sent.
    """

    def __init__(
        self,
        audio_queue: asyncio.Queue[bytes | None],
        loop: asyncio.AbstractEventLoop,
        vad_enabled: bool = True,
    ):
        self._queue = audio_queue
        self._loop = loop
        self._vad_enabled = vad_enabled and _TEN_VAD_AVAILABLE
        self._stream: Optional[sd.InputStream] = None
        self._vad: Optional[TenVad] = None
        self._running = False
        self._silence_since: float = 0.0
        self._last_keepalive: float = 0.0

        if self._vad_enabled:
            try:
                self._vad = TenVad(hop_size=VAD_HOP_SIZE, threshold=VAD_THRESHOLD)
                log.info("TEN VAD initialized (hop=%d, threshold=%.2f)", VAD_HOP_SIZE, VAD_THRESHOLD)
            except Exception as e:
                log.warning("Failed to init TEN VAD: %s — disabling", e)
                self._vad = None
                self._vad_enabled = False

    @property
    def vad_available(self) -> bool:
        return _TEN_VAD_AVAILABLE

    @property
    def vad_active(self) -> bool:
        return self._vad_enabled and self._vad is not None

    def start(self) -> None:
        """Start capturing audio."""
        import time
        self._running = True
        self._last_keepalive = time.monotonic()
        self._silence_since = time.monotonic()

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._audio_callback,
        )
        self._stream.start()
        log.info(
            "Audio capture started: %dHz, %dch, block=%d, VAD=%s",
            SAMPLE_RATE, CHANNELS, BLOCK_SIZE, "on" if self.vad_active else "off",
        )

    def stop(self) -> None:
        """Stop capturing and signal end-of-stream."""
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("Audio capture stopped")
        # Signal EOF to the STT client
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags
    ) -> None:
        """Called from the audio thread for each block."""
        if not self._running:
            return

        if status:
            log.warning("Audio callback status: %s", status)

        # indata is (frames, channels) float32
        samples = indata[:, 0]  # mono

        should_send = True

        if self.vad_active:
            should_send = self._check_vad(samples)
            if not should_send:
                should_send = self._check_keepalive()

        if should_send:
            pcm = self._float_to_pcm16(samples)
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm)
            except asyncio.QueueFull:
                log.debug("Audio queue full, dropping chunk")

    def _check_vad(self, samples: np.ndarray) -> bool:
        """Run VAD on samples. Returns True if speech detected."""
        import time
        assert self._vad is not None

        # TEN VAD expects int16 at 16kHz
        int_samples = (samples * 32767).astype(np.int16)

        # Process in hop-size chunks
        speech_detected = False
        for i in range(0, len(int_samples) - VAD_HOP_SIZE + 1, VAD_HOP_SIZE):
            chunk = int_samples[i : i + VAD_HOP_SIZE]
            prob, flag = self._vad.process(chunk)
            if flag:
                speech_detected = True
                break

        if speech_detected:
            self._silence_since = time.monotonic()

        return speech_detected

    def _check_keepalive(self) -> bool:
        """Send a keepalive frame periodically during silence."""
        import time
        now = time.monotonic()
        if now - self._last_keepalive >= KEEPALIVE_INTERVAL_SECS:
            self._last_keepalive = now
            log.debug("Sending keepalive silence frame")
            return True
        return False

    @staticmethod
    def _float_to_pcm16(samples: np.ndarray) -> bytes:
        """Convert float32 [-1, 1] to 16-bit PCM bytes."""
        clipped = np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767).astype(np.int16)
        return pcm.tobytes()
