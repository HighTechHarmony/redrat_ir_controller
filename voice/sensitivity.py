"""
Wake word sensitivity manager — implements "quiet mode".

Temporarily raises the wake word threshold (making it harder to trigger, so
conversations in the room won't cause false wakes) for a configurable
duration, then automatically reverts to the configured value.

Driven by voice commands ("quiet mode" / "normal mode"), the web API, and
(eventually) a physical button — all through the same SensitivityManager.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_SUPPRESSED_THRESHOLD = 0.99
DEFAULT_SUPPRESS_SECONDS = 3600      # 1 hour
MAX_SUPPRESS_SECONDS = 4 * 3600      # 4 hours


class SensitivityManager:
    """
    Controls a runtime override on the wake word detector's threshold.

    Normal state: the detector uses its configured threshold (e.g. 0.8).
    Suppressed ("quiet mode"): the detector uses a higher threshold (e.g.
    0.99) for up to *seconds* seconds, then auto-reverts.
    """

    def __init__(
        self,
        detector,
        default_threshold: float,
        suppressed_threshold: float = DEFAULT_SUPPRESSED_THRESHOLD,
    ) -> None:
        self._detector = detector
        self._default_threshold = float(default_threshold)
        self._suppressed_threshold = float(suppressed_threshold)
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._suppressed_until = 0.0   # monotonic timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suppress(self, seconds: int = DEFAULT_SUPPRESS_SECONDS) -> None:
        """Enter quiet mode for *seconds* seconds (clamped to MAX_SUPPRESS_SECONDS)."""
        seconds = max(0, int(seconds))
        seconds = min(seconds, MAX_SUPPRESS_SECONDS)
        if seconds == 0:
            self.cancel()
            return
        with self._lock:
            self._cancel_timer_locked()
            self._detector.threshold = self._suppressed_threshold
            self._suppressed_until = time.monotonic() + seconds
            self._timer = threading.Timer(seconds, self._auto_revert)
            self._timer.daemon = True
            self._timer.start()
        log.info(
            "Wake word suppressed: threshold -> %.2f for %d s",
            self._suppressed_threshold,
            seconds,
        )

    def cancel(self) -> None:
        """Leave quiet mode immediately and revert to the configured threshold."""
        with self._lock:
            self._cancel_timer_locked()
            self._detector.threshold = None
            self._suppressed_until = 0.0
        log.info(
            "Wake word suppression cancelled — threshold back to %.2f",
            self._default_threshold,
        )

    @property
    def status(self) -> dict:
        """Snapshot of the current sensitivity state for the API/UI."""
        with self._lock:
            suppressed = self._suppressed_until > time.monotonic()
            remaining_s = None
            if suppressed:
                remaining_s = max(0, int(self._suppressed_until - time.monotonic()))
            return {
                "state": "suppressed" if suppressed else "normal",
                "threshold": float(self._detector.threshold),
                "default_threshold": self._default_threshold,
                "suppressed_threshold": self._suppressed_threshold,
                "remaining_s": remaining_s,
                "default_suppress_s": DEFAULT_SUPPRESS_SECONDS,
                "max_suppress_s": MAX_SUPPRESS_SECONDS,
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _auto_revert(self) -> None:
        """Called by the timer when the suppression period elapses."""
        with self._lock:
            # Only revert if this timer is still the current one (a newer
            # suppress()/cancel() call may have replaced or cancelled it).
            if self._suppressed_until <= time.monotonic():
                self._detector.threshold = None
                self._suppressed_until = 0.0
                log.info(
                    "Wake word suppression timed out — threshold back to %.2f",
                    self._default_threshold,
                )

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
