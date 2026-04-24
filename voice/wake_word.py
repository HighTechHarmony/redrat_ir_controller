"""
Wake word detection using openWakeWord.

Continuously reads audio frames from an AudioCapture queue and fires a
threading.Event when the configured wake word is detected above threshold.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import numpy as np
from voice.audio import FRAME_SAMPLES

log = logging.getLogger(__name__)


class WakeWordDetector:
    """
    Wraps an openWakeWord model and runs detection in a background thread.

    When the wake word score exceeds *threshold*, *wake_event* is set.
    The detector then pauses scoring for *cooldown_s* seconds to avoid
    repeated triggers from the same utterance.
    """

    def __init__(
        self,
        model_name: str,
        audio_queue: "queue.Queue[np.ndarray]",
        threshold: float = 0.5,
        cooldown_s: float = 2.0,
        log_scores: bool = False,
        log_every: int = 50,
        *,
        # Beep playback options when wake word fires
        beep_on_wake: bool = False,
        beep_device: Optional[str] = None,
        beep_freq: int = 800,
        beep_duration_s: float = 0.5,
    ) -> None:
        self._model_name = model_name
        self._audio_queue = audio_queue
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._model = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Set by this detector when the wake word fires; cleared by the STT engine
        self.wake_event = threading.Event()
        # Set by SpeechRecognizer while it is actively listening for a command;
        # the detector checks this to suppress a second wake beep mid-session.
        self.listening_event = threading.Event()

        # Logging options
        self._log_scores = bool(log_scores)
        self._log_every = int(log_every)
        # Beep settings
        self._beep_on_wake = bool(beep_on_wake)
        self._beep_device = beep_device
        self._beep_freq = int(beep_freq)
        self._beep_duration_s = float(beep_duration_s)
        # Timestamp of last beep/fire to enforce cooldown independent of
        # `wake_event` being cleared by the STT thread.
        self._last_fire = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load the model and start the detection thread."""
        log.info("Loading openWakeWord model %r ...", self._model_name)
        from openwakeword.model import Model  # imported lazily — slow to load
        import openwakeword

        model_name = self._model_name
        # Backwards-compatible alias: allow "hey_jarvis" in config.
        if model_name == "hey_jarvis":
            model_name = "hey_jarvis_v0.1"

        try:
            self._model = Model(
                wakeword_models=[model_name],
                vad_threshold=0.5,
                enable_speex_noise_suppression=False,   # set True on arm64 if desired
            )
        except ValueError as exc:
            # Common first-run issue: package installed in one interpreter but
            # models downloaded in a different interpreter/site-packages path.
            if "Could not open" not in str(exc):
                raise
            log.warning(
                "Wake word model file missing for %r (%s). Attempting model download and retry...",
                model_name,
                exc,
            )
            openwakeword.utils.download_models()
            self._model = Model(
                wakeword_models=[model_name],
                vad_threshold=0.5,
                enable_speex_noise_suppression=False,
            )
        log.info("Wake word model loaded (threshold=%.2f)", self._threshold)

        # Resolved model key used in predictions (may differ from configured name)
        self._model_key = model_name

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="wake-word",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the detection thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Detection loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        import time

        model_key = getattr(self, "_model_key", self._model_name)
        log.debug("Wake word detection thread started")
        frame_count = 0

        # Overlapping windows: buffer incoming frames and slide by 50% hop so
        # a wake word that straddles a frame boundary is still detected.
        hop = FRAME_SAMPLES // 2
        buffer = np.array([], dtype=np.int16)

        while not self._stop_event.is_set():
            try:
                frame = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # Append incoming frame to the overlap buffer.
            buffer = np.concatenate((buffer, frame.astype(np.int16, copy=False)))

            # Evaluate every hop-sized advance while we have a full window.
            while buffer.shape[0] >= FRAME_SAMPLES:
                window = buffer[:FRAME_SAMPLES]
                buffer = buffer[hop:]           # advance by hop (50% overlap)
                frame_count += 1

                frame_f32 = window.astype(np.float32) / 32768.0
                predictions = self._model.predict(window)  # int16 required by openwakeword preprocessor
                score = predictions.get(model_key, 0.0)

                # Optional periodic score logging
                if self._log_scores and (model_key not in predictions):
                    try:
                        top = max(predictions.items(), key=lambda kv: kv[1])
                    except Exception:
                        top = (None, 0.0)
                    log.info(
                        "WakeWord missing key: expected=%r available=%r top=%r",
                        model_key,
                        list(predictions.keys()),
                        top,
                    )

                if self._log_scores and (frame_count % self._log_every) == 0:
                    try:
                        qsize = self._audio_queue.qsize()
                    except Exception:
                        qsize = -1
                    try:
                        rms = float(np.sqrt(np.mean(frame_f32 * frame_f32)))
                    except Exception:
                        rms = 0.0
                    try:
                        top = max(predictions.items(), key=lambda kv: kv[1])
                    except Exception:
                        top = (None, 0.0)
                    log.debug(
                        "WakeWord score: model=%r frame=%d queue=%s score=%.3f rms=%.5f top=%r top_score=%.3f",
                        model_key,
                        frame_count,
                        qsize,
                        float(score),
                        rms,
                        top[0],
                        float(top[1]),
                    )

                if score >= self._threshold:
                    now = time.monotonic()
                    # Enforce cooldown based on wall-clock time so that the
                    # detector won't trigger multiple beeps even if the
                    # recognizer clears `wake_event` quickly.
                    if (now - getattr(self, "_last_fire", 0.0)) <= self._cooldown_s:
                        # Skip logging/beep while still in cooldown.
                        if self._log_scores:
                            log.debug(
                                "Wake word suppressed by cooldown (%.2fs remaining)",
                                self._cooldown_s - (now - self._last_fire),
                            )
                        continue

                    # Suppress wake beep (and re-trigger) while STT is
                    # actively listening for a command.
                    if self.listening_event.is_set():
                        if self._log_scores:
                            log.debug("Wake word suppressed — STT is currently listening")
                        continue

                    # Only set the wake event and play the beep if it is not
                    # already set. This prevents multiple beeps when the model
                    # fires repeatedly for the same utterance.
                    if not self.wake_event.is_set():
                        log.info("Wake word detected! model=%r score=%.3f", model_key, score)
                        self._last_fire = now
                        self.wake_event.set()
                        if self._beep_on_wake:
                            # Play a short beep in a non-blocking way. Import sounddevice lazily.
                            # Log debug info so we can trace duplicate beeps (thread id
                            # and timestamp) when diagnosing repeated triggers.
                            try:
                                log.debug(
                                    "Wake beep requested: freq=%s dur=%.3f device=%r thread=%s",
                                    self._beep_freq,
                                    self._beep_duration_s,
                                    self._beep_device,
                                    threading.get_ident(),
                                )
                                import sounddevice as _sd

                                # Query output device default samplerate and fall back to 16 kHz.
                                try:
                                    info = _sd.query_devices(self._beep_device, kind="output")
                                    rate = int(info.get("default_samplerate", 16000))
                                except Exception:
                                    rate = 16000

                                samples = int(round(self._beep_duration_s * rate))
                                if samples <= 0:
                                    raise ValueError("beep_duration_s too small")
                                t = np.arange(samples, dtype=np.float32) / float(rate)
                                wave = (0.3 * np.sin(2.0 * np.pi * float(self._beep_freq) * t)).astype(np.float32)
                                # sounddevice.play is non-blocking when blocking=False
                                try:
                                    _sd.play(wave, samplerate=rate, device=self._beep_device, blocking=False)
                                except Exception as _exc:
                                    log.warning("Beep playback failed: %s", _exc)
                            except Exception:
                                log.debug("sounddevice not available for beep playback or failed to configure")

                        # Cooldown: clear buffer, drain the queue, sleep.
                        buffer = np.array([], dtype=np.int16)
                        time.sleep(self._cooldown_s)
                        while not self._audio_queue.empty():
                            try:
                                self._audio_queue.get_nowait()
                            except queue.Empty:
                                break

        log.debug("Wake word detection thread stopped")
