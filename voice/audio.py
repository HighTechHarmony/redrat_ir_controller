"""
Continuous mono audio capture via sounddevice.

The voice pipeline expects 16 kHz int16 mono frames of 80 ms
(1280 samples), so this module will attempt native 16 kHz capture first
and, if the selected input device rejects that sample rate, transparently
fall back to the device default sample rate and resample to 16 kHz.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 80
FRAME_SAMPLES = TARGET_SAMPLE_RATE * FRAME_MS // 1000   # 1280 samples


class AudioCapture:
    """
    Captures audio from a named ALSA device and exposes it via a Queue.

    Each item placed in *queue* is a 1-D int16 NumPy array of length
    FRAME_SAMPLES (1280 samples @ 16 kHz = 80 ms).
    """

    def __init__(self, device: Optional[str], maxqueue: int = 50) -> None:
        """
        Parameters
        ----------
        device:
            ALSA device name (e.g. "hw:1,0", "plughw:1,0", "default")
            or None to use the system default.
        maxqueue:
            Maximum number of unprocessed frames to buffer.  When full,
            the oldest frame is dropped to prevent unbounded memory use.
        """
        self._device = device if device and device.lower() not in ("", "default") else None
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=maxqueue)
        self._stream: Optional[sd.InputStream] = None
        self._stop_event = threading.Event()
        self._input_sample_rate = TARGET_SAMPLE_RATE
        self._resample_buffer = np.array([], dtype=np.float32)

        # Debug / logging
        self._debug = False
        self._enqueue_count = 0
        self._enqueue_log_every = 100

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the audio stream and begin feeding frames into the queue."""
        if self._stream is not None:
            return
        log.info(
            "Opening audio device %r at %d Hz, %d-ms frames",
            self._device or "default",
            TARGET_SAMPLE_RATE,
            FRAME_MS,
        )
        self._stop_event.clear()
        self._resample_buffer = np.array([], dtype=np.float32)

        try:
            self._input_sample_rate = TARGET_SAMPLE_RATE
            self._stream = sd.InputStream(
                samplerate=TARGET_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=self._callback,
            )
        except sd.PortAudioError as exc:
            if "Invalid sample rate" not in str(exc):
                raise

            fallback_rate = self._device_default_sample_rate()
            log.warning(
                "Device %r rejected %d Hz (%s). Falling back to %.0f Hz and resampling.",
                self._device or "default",
                TARGET_SAMPLE_RATE,
                exc,
                fallback_rate,
            )
            self._input_sample_rate = int(fallback_rate)
            self._stream = sd.InputStream(
                samplerate=self._input_sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=0,
                device=self._device,
                callback=self._callback,
            )

        self._stream.start()
        log.info(
            "Audio stream running: input_rate=%dHz, output_rate=%dHz",
            self._input_sample_rate,
            TARGET_SAMPLE_RATE,
        )

    def stop(self) -> None:
        """Close the audio stream."""
        self._stop_event.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("Audio stream closed")

    def __enter__(self) -> "AudioCapture":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal callback
    # ------------------------------------------------------------------

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status,
    ) -> None:
        if status:
            log.warning("Audio capture status: %s", status)
        if self._stop_event.is_set():
            return
        mono = indata[:, 0].astype(np.float32, copy=False)
        if self._input_sample_rate == TARGET_SAMPLE_RATE and mono.shape[0] == FRAME_SAMPLES:
            self._enqueue_frame(mono.astype(np.int16, copy=True))
            return

        self._resample_buffer = np.concatenate((self._resample_buffer, mono))
        source_samples_per_frame = max(
            1,
            int(round(FRAME_SAMPLES * self._input_sample_rate / TARGET_SAMPLE_RATE)),
        )

        while self._resample_buffer.shape[0] >= source_samples_per_frame:
            source = self._resample_buffer[:source_samples_per_frame]
            self._resample_buffer = self._resample_buffer[source_samples_per_frame:]

            if source_samples_per_frame == FRAME_SAMPLES:
                out = source.astype(np.int16, copy=False)
            else:
                x_old = np.arange(source_samples_per_frame, dtype=np.float32)
                x_new = np.linspace(
                    0,
                    source_samples_per_frame - 1,
                    FRAME_SAMPLES,
                    dtype=np.float32,
                )
                out = np.interp(x_new, x_old, source).astype(np.int16)

            self._enqueue_frame(out)

    def _enqueue_frame(self, frame: np.ndarray) -> None:
        self._enqueue_count += 1
        if self.queue.full():
            try:
                self.queue.get_nowait()   # drop oldest
            except queue.Empty:
                pass
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            pass   # race — already drained above, just skip

        if self._debug and (self._enqueue_log_every > 0) and (self._enqueue_count % self._enqueue_log_every == 0):
            try:
                qsize = self.queue.qsize()
            except Exception:
                qsize = -1
            # Compute RMS for quick level diagnostics (frame is int16)
            try:
                rms = float((frame.astype('float32') / 32768.0).std())
            except Exception:
                rms = 0.0
            log.debug(
                "Audio enqueue: count=%d queue=%s input_rate=%d rms=%.5f",
                self._enqueue_count,
                qsize,
                self._input_sample_rate,
                rms,
            )

    def _device_default_sample_rate(self) -> float:
        info = sd.query_devices(self._device, kind="input")
        default_rate = float(info.get("default_samplerate", 0) or 0)
        if default_rate <= 0:
            raise sd.PortAudioError(
                "Unable to determine input device default sample rate for fallback"
            )
        return default_rate
