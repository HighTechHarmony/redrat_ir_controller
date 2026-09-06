"""Text-to-speech playback through the system espeak-ng executable."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


class TtsError(RuntimeError):
    """Base error for text-to-speech failures."""


class TtsUnavailableError(TtsError):
    """The configured speech executable is not available."""


class TtsPlaybackError(TtsError):
    """The speech executable failed to play the text."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class TtsTimeoutError(TtsPlaybackError):
    """Speech playback exceeded its timeout."""


class TextToSpeech:
    """Invoke espeak-ng using a configured ALSA output device."""

    def __init__(
        self,
        device: str = "default",
        executable: str = "espeak-ng",
        timeout_s: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.device = device
        self.executable = executable
        self.timeout_s = timeout_s
        self._runner = runner

    def speak(self, text: str) -> None:
        command = [self.executable, "-d", self.device, text]
        try:
            result = self._runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise TtsUnavailableError("espeak-ng is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TtsTimeoutError("speech playback timed out") from exc
        except OSError as exc:
            raise TtsUnavailableError("could not start espeak-ng") from exc

        stderr = (getattr(result, "stderr", "") or "").strip()
        if result.returncode != 0 or stderr:
            raise TtsPlaybackError(
                "speech playback failed",
                returncode=result.returncode,
                stderr=stderr,
            )