"""
Post-wake-word speech-to-text using Vosk with restricted vocabulary.

The recognizer activates when *wake_event* is set, transcribes speech
until silence or *command_timeout_s* elapses, then calls *on_transcript*
with the recognised text.

The Vosk KaldiRecognizer is re-initialised whenever *rebuild_event* is
set (i.e. after the voice command list changes via the API).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# Vosk internal sample rate must match capture rate
_VOSK_RATE = 16_000


class SpeechRecognizer:
    """
    Listens for wake → transcribes one command → calls on_transcript.

    Parameters
    ----------
    model_path:
        Path to the extracted Vosk model directory.
    audio_queue:
        Queue of int16 NumPy frames from AudioCapture.
    wake_event:
        Set by WakeWordDetector when the wake word fires.  Cleared here
        after transcription begins.
    rebuild_event:
        Set by VoiceCommandStore after the command list changes.  Cleared
        here after rebuilding the recognizer.
    get_phrases:
        Callable that returns the current list of command phrases (used to
        restrict the Vosk vocabulary).
    on_transcript:
        Called with the final transcription string when recognition completes.
    command_timeout_s:
        Seconds to wait for a complete utterance after the wake word.
    """

    def __init__(
        self,
        model_path: str | Path,
        audio_queue: "queue.Queue[np.ndarray]",
        wake_event: threading.Event,
        rebuild_event: threading.Event,
        get_phrases: Callable[[], List[str]],
        on_transcript: Callable[[str], None],
        command_timeout_s: float = 5.0,
    ) -> None:
        self._model_path = Path(model_path)
        self._audio_queue = audio_queue
        self._wake_event = wake_event
        self._rebuild_event = rebuild_event
        self._get_phrases = get_phrases
        self._on_transcript = on_transcript
        self._command_timeout_s = command_timeout_s

        self._vosk_model = None
        self._recognizer = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Shared mutable status for the /api/voice/status endpoint
        self.status: dict = {"state": "loading"}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load the Vosk model and start the STT thread."""
        import vosk
        log.info("Loading Vosk model from %s ...", self._model_path)
        self._vosk_model = vosk.Model(str(self._model_path))
        self._build_recognizer()
        log.info("Vosk model loaded")
        self.status["state"] = "idle"

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stt",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the STT thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.status["state"] = "stopped"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_recognizer(self) -> None:
        """(Re)build KaldiRecognizer with the current phrase list."""
        import vosk
        phrases = self._get_phrases()
        if phrases:
            # Restrict vocabulary; "[unk]" handles out-of-vocabulary tokens
            vocab = json.dumps(phrases + ["[unk]"])
            log.debug("Building Vosk recognizer with %d phrase(s)", len(phrases))
            self._recognizer = vosk.KaldiRecognizer(self._vosk_model, _VOSK_RATE, vocab)
        else:
            log.debug("No phrases defined — using open vocabulary")
            self._recognizer = vosk.KaldiRecognizer(self._vosk_model, _VOSK_RATE)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        log.debug("STT thread started")

        while not self._stop_event.is_set():
            # Rebuild recognizer if the vocabulary changed
            if self._rebuild_event.is_set():
                self._rebuild_event.clear()
                self._build_recognizer()
                log.info("Vosk vocabulary rebuilt")

            # Wait for wake word (check stop/rebuild every 0.5 s)
            if not self._wake_event.wait(timeout=0.5):
                continue

            # Wake word fired — start transcribing
            self._wake_event.clear()
            # Signal detector to suppress wake beep while we are listening.
            if hasattr(self, "_listening_event"):
                self._listening_event.set()
            self.status["state"] = "listening"
            log.info("Wake word detected — listening for command (timeout=%.1fs)", self._command_timeout_s)

            self._recognizer.Reset()
            deadline = time.monotonic() + self._command_timeout_s
            transcript = ""

            while time.monotonic() < deadline and not self._stop_event.is_set():
                try:
                    frame = self._audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                audio_bytes = frame.tobytes()

                if self._recognizer.AcceptWaveform(audio_bytes):
                    result = json.loads(self._recognizer.Result())
                    text = result.get("text", "").strip()
                    if text and text != "[unk]":
                        transcript = text
                        break   # got a final result
                else:
                    partial = json.loads(self._recognizer.PartialResult())
                    p = partial.get("partial", "")
                    if p:
                        self.status["state"] = "recognizing"

            # Drain any remaining audio after timeout
            if not transcript:
                final = json.loads(self._recognizer.FinalResult())
                transcript = final.get("text", "").strip()

            self.status["state"] = "idle"
            # STT done listening — allow wake beep again.
            if hasattr(self, "_listening_event"):
                self._listening_event.clear()

            if transcript and transcript != "[unk]":
                log.info("Transcription: %r", transcript)
                # Play acknowledgement beep: 800Hz for 0.25s, then 1600Hz for 0.25s
                try:
                    import sounddevice as _sd

                    device = getattr(self, "_beep_device", None)
                    try:
                        info = _sd.query_devices(device, kind="output")
                        rate = int(info.get("default_samplerate", 16000))
                    except Exception:
                        rate = 16000

                    dur = 0.15
                    samples = int(round(dur * rate))
                    if samples > 0:
                        t = np.arange(samples, dtype=np.float32) / float(rate)
                        tone1 = (0.3 * np.sin(2.0 * np.pi * 800.0 * t)).astype(np.float32)
                        tone2 = (0.3 * np.sin(2.0 * np.pi * 1600.0 * t)).astype(np.float32)
                        wave = np.concatenate((tone1, tone2))
                        try:
                            _sd.play(wave, samplerate=rate, device=device, blocking=False)
                        except Exception as _exc:
                            log.warning("Ack beep playback failed: %s", _exc)
                except Exception:
                    log.debug("sounddevice not available for ack beep")
                try:
                    self._on_transcript(transcript)
                except Exception as exc:
                    log.error("on_transcript raised: %s", exc)
            else:
                log.info("No command recognised (transcript=%r)", transcript)
                # Play a short timeout tone one octave down (half frequency)
                try:
                    import sounddevice as _sd

                    device = getattr(self, "_beep_device", None)
                    base_freq = float(getattr(self, "_beep_freq", 800))
                    freq = max(20.0, base_freq / 2.0)
                    duration = float(getattr(self, "_beep_duration_s", 0.5))

                    # Query output device default samplerate and fall back
                    # to 16000 Hz.
                    try:
                        info = _sd.query_devices(device, kind="output")
                        rate = int(info.get("default_samplerate", 16000))
                    except Exception:
                        rate = 16000

                    samples = int(round(duration * rate))
                    if samples > 0:
                        t = np.arange(samples, dtype=np.float32) / float(rate)
                        wave = (0.3 * np.sin(2.0 * np.pi * float(freq) * t)).astype(
                            np.float32
                        )
                        try:
                            _sd.play(wave, samplerate=rate, device=device, blocking=False)
                        except Exception as _exc:
                            log.warning("Timeout beep playback failed: %s", _exc)
                except Exception:
                    log.debug("sounddevice not available for timeout beep")

        log.debug("STT thread stopped")
