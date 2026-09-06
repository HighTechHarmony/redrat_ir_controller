"""Short notification-tone playback through ALSA dmix."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterable

import numpy as np

log = logging.getLogger(__name__)

_SAMPLE_RATE = 48_000


def play_tones(
    tones: Iterable[tuple[float, float]],
    device: str,
    *,
    gap_s: float = 0.0,
    blocking: bool = True,
) -> None:
    """Play sine tones through an ALSA PCM, optionally in a daemon thread."""
    chunks: list[np.ndarray] = []
    tone_list = list(tones)
    for index, (frequency, duration_s) in enumerate(tone_list):
        samples = int(round(duration_s * _SAMPLE_RATE))
        if samples <= 0:
            raise ValueError("tone duration must be positive")
        time_points = np.arange(samples, dtype=np.float32) / _SAMPLE_RATE
        chunks.append((0.3 * np.sin(2.0 * np.pi * frequency * time_points)).astype(np.float32))
        if gap_s > 0 and index < len(tone_list) - 1:
            chunks.append(np.zeros(int(round(gap_s * _SAMPLE_RATE)), dtype=np.float32))

    if not chunks:
        return
    pcm = (np.concatenate(chunks) * np.iinfo(np.int16).max).astype(np.int16).tobytes()
    command = [
        "aplay",
        "--quiet",
        "--device",
        device,
        "--file-type=raw",
        "--format=S16_LE",
        "--channels=1",
        f"--rate={_SAMPLE_RATE}",
    ]

    def _play() -> None:
        try:
            result = subprocess.run(command, input=pcm, capture_output=True, check=False)
        except OSError as exc:
            log.warning("Tone playback could not start: %s", exc)
            return
        if result.returncode:
            log.warning("Tone playback failed: %s", result.stderr.decode(errors="replace").strip())

    if blocking:
        _play()
    else:
        threading.Thread(target=_play, daemon=True, name="notification-tone").start()