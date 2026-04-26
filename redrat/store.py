"""
YAML-backed store for IR signals.

File schema (ir_codes.yaml):
  signal_name:
    carrier_hz: 38000
    timings_us: [8960, 4480, 560, ...]
    repeat: 0           # optional, default 0
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from redrat.protocol import IrData

log = logging.getLogger(__name__)


class SignalNotFoundError(KeyError):
    """Raised when a requested signal name does not exist in the store."""


class SignalStore:
    """
    Thread-safe YAML-backed store for IR signals.

    All public methods are safe to call from multiple threads.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._signals: Dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            log.info("Signal store not found at %s — starting empty", self._path)
            return
        with self._path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected a YAML mapping in {self._path}")
        self._signals = data
        log.info("Loaded %d signal(s) from %s", len(self._signals), self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self._signals, fh, default_flow_style=False, allow_unicode=True)
        tmp.replace(self._path)
        log.debug("Saved %d signal(s) to %s", len(self._signals), self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_names(self) -> List[str]:
        """Return a sorted list of all stored signal names."""
        with self._lock:
            return sorted(self._signals.keys())

    def get(self, name: str) -> IrData:
        """
        Retrieve a signal by name as an IrData.

        Raises SignalNotFoundError if the name is not in the store.
        """
        with self._lock:
            entry = self._signals.get(name)
        if entry is None:
            raise SignalNotFoundError(name)
        return IrData(
            carrier_hz=int(entry.get("carrier_hz", 38000)),
            timings_us=list(entry["timings_us"]),
            no_repeats=int(entry.get("repeat", 0)),
        )

    def save_signal(self, name: str, ir: IrData) -> None:
        """
        Store (or overwrite) a signal by name and persist to YAML.

        *name* must be a non-empty string containing only alphanumeric
        characters, underscores, or hyphens.
        """
        if not name or not all(c.isalnum() or c in "_-" for c in name):
            raise ValueError(
                f"Signal name {name!r} must be non-empty and contain only "
                "alphanumeric characters, underscores, or hyphens."
            )
        with self._lock:
            self._signals[name] = {
                "carrier_hz": ir.carrier_hz,
                "timings_us": ir.timings_us,
                "repeat": ir.no_repeats,
            }
            self._save()
        log.info("Saved signal %r (%d timings)", name, len(ir.timings_us))

    def delete(self, name: str) -> None:
        """
        Remove a signal by name.

        Raises SignalNotFoundError if not found.
        """
        with self._lock:
            if name not in self._signals:
                raise SignalNotFoundError(name)
            del self._signals[name]
            self._save()
        log.info("Deleted signal %r", name)

    def as_dict(self) -> Dict[str, dict]:
        """Return a copy of the raw signal dictionary (for serialisation)."""
        with self._lock:
            return dict(self._signals)

    def reload(self) -> None:
        """Reload signals from disk, replacing the in-memory state."""
        with self._lock:
            self._signals = {}
            self._load()
        log.info("Signal store reloaded from %s", self._path)
