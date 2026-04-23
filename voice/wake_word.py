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

        # Logging options
        self._log_scores = bool(log_scores)
        self._log_every = int(log_every)

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

        model_key = self._model_name
        log.debug("Wake word detection thread started")
        frame_count = 0

        while not self._stop_event.is_set():
            try:
                frame = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            frame_count += 1

            # openWakeWord expects float32 in range [-1, 1]
            frame_f32 = frame.astype(np.float32) / 32768.0
            predictions = self._model.predict(frame_f32)

            # predictions is a dict: {model_name: score}
            score = predictions.get(model_key, 0.0)

            if self._log_scores and (frame_count % self._log_every) == 0:
                try:
                    qsize = self._audio_queue.qsize()
                except Exception:
                    qsize = -1
                log.info(
                    "WakeWord score: model=%r frame=%d queue=%s score=%.3f",
                    model_key,
                    frame_count,
                    qsize,
                    float(score),
                )

            if score >= self._threshold:
                log.info("Wake word detected! model=%r score=%.3f", model_key, score)
                self.wake_event.set()
                # Cooldown: drain the queue and sleep to avoid re-triggering
                time.sleep(self._cooldown_s)
                # Flush any frames that accumulated during cooldown
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break

        log.debug("Wake word detection thread stopped")
