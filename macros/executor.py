"""
Macro executor — runs named sequences of IR signals with inter-step delays.

Macro YAML schema (macros.yaml):
  macro_name:
    - signal: signal_name
      delay_ms: 500       # milliseconds to wait AFTER sending this signal
    - signal: another_signal
      delay_ms: 0
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List

import yaml

from redrat.store import SignalStore, SignalNotFoundError

log = logging.getLogger(__name__)

VIRTUAL_DELAY_1S = "__delay_1s__"
VIRTUAL_DELAY_10S = "__delay_10s__"

# Reserved system macro names backed by code (not IR signals).
SYSTEM_QUIET_MODE = "__quiet_mode__"
SYSTEM_NORMAL_MODE = "__normal_mode__"


class MacroNotFoundError(KeyError):
    """Raised when a requested macro name does not exist."""


class MacroExecutor:
    """
    Loads macros from a YAML file and executes them against a RedRat3 device.

    Macros are loaded once at startup. Call reload() to pick up file changes
    without restarting the service.
    """

    def __init__(
        self,
        macro_path: str | Path,
        signal_store: SignalStore,
        device,
        system_actions: Dict[str, Callable] | None = None,
    ) -> None:
        self._path = Path(macro_path)
        self._store = signal_store
        self._device = device
        self._lock = threading.Lock()
        self._system_actions: Dict[str, Callable] = dict(system_actions or {})
        self._macros: Dict[str, List[dict]] = {}
        self.reload()

    def set_system_actions(self, actions: Dict[str, Callable]) -> None:
        """Register code-backed system macros (e.g. quiet mode) keyed by name."""
        with self._lock:
            self._system_actions = dict(actions)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """(Re)load macros from disk."""
        with self._lock:
            if not self._path.exists():
                log.warning("Macro file not found: %s", self._path)
                self._macros = {}
                return
            with self._path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if not isinstance(data, dict):
                raise ValueError(f"Expected a YAML mapping in {self._path}")
            self._macros = data
            log.info("Loaded %d macro(s) from %s", len(self._macros), self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self._macros, fh, default_flow_style=False, allow_unicode=True)
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Virtual macros  (single-step macros auto-created for every signal)
    # ------------------------------------------------------------------

    def _is_virtual(self, name: str) -> bool:
        """Return True if *name* is a signal name without a real macro."""
        with self._lock:
            if name in self._macros or name in self._system_actions:
                return False
        try:
            self._store.get(name)
            return True
        except SignalNotFoundError:
            return False

    def _virtual_steps(self, name: str) -> List[dict] | None:
        """Return a single-step macro definition if *name* is a signal, else None."""
        with self._lock:
            if name in self._system_actions:
                return None
        try:
            self._store.get(name)
            return [{"signal": name}]
        except SignalNotFoundError:
            return None

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list_macros(self) -> Dict[str, List[dict]]:
        """Return a copy of all macro definitions, including system and virtual macros."""
        with self._lock:
            result = {k: list(v) for k, v in self._macros.items()}
        for name in self._system_actions:
            result.setdefault(name, [])
        for sig_name in self._store.list_names():
            if sig_name not in result:
                result[sig_name] = [{"signal": sig_name}]
        return result

    def macro_names(self) -> List[str]:
        """Return sorted list of macro names (real + system + virtual)."""
        with self._lock:
            real = set(self._macros.keys())
            system = set(self._system_actions.keys())
        virtual = {s for s in self._store.list_names() if s not in real}
        return sorted(real | system | virtual)

    def save_macro(self, name: str, steps: List[dict]) -> None:
        """Create or replace a macro and persist it to YAML."""
        _validate_macro_name(name)
        with self._lock:
            if name in self._system_actions:
                # System macros are code-backed; accept any steps but store empty.
                self._macros[name] = []
                self._save()
                return
        normalized: List[dict] = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"Macro step {i} must be an object")

            signal = step.get("signal")
            delay_ms = int(step.get("delay_ms", 0))

            if signal is None and delay_ms <= 0:
                raise ValueError(
                    f"Macro step {i} must define 'signal' or a positive 'delay_ms'"
                )

            if signal is not None:
                signal = str(signal).strip()
                if signal != VIRTUAL_DELAY_1S and signal != VIRTUAL_DELAY_10S:
                    if not signal:
                        raise ValueError(f"Macro step {i} has an empty signal name")
                    if signal not in self._store.list_names():
                        raise ValueError(
                            f"Macro step {i} references unknown signal {signal!r}"
                        )

            normalized_step = {}
            if signal is not None:
                normalized_step["signal"] = signal
            if delay_ms:
                normalized_step["delay_ms"] = delay_ms
            normalized.append(normalized_step)

        with self._lock:
            self._macros[name] = normalized
            self._save()

    def delete_macro(self, name: str) -> None:
        with self._lock:
            if name in self._system_actions:
                raise ValueError(f"System macro {name!r} cannot be deleted")
            if name not in self._macros:
                raise MacroNotFoundError(name)
            del self._macros[name]
            self._save()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, name: str) -> None:
        """
        Execute a macro by name.

        If no real macro is found, falls back to a virtual single-step
        macro for any signal in the IR code store.

        Raises MacroNotFoundError if the name is neither a macro nor a signal.
        Raises SignalNotFoundError or RedRatError on IR signal problems.
        """
        with self._lock:
            action = self._system_actions.get(name)
        if action is not None:
            log.info("Running system macro %r", name)
            action()
            return

        with self._lock:
            steps = self._macros.get(name)
            if steps is not None:
                steps = list(steps)
        if steps is None:
            steps = self._virtual_steps(name)
        if steps is None:
            raise MacroNotFoundError(name)

        log.info("Running macro %r (%d step(s))", name, len(steps))

        for i, step in enumerate(steps):
            signal_name = step.get("signal")
            delay_ms = int(step.get("delay_ms", 0))

            # Virtual delay tokens, useful as steps in macro editors.
            if signal_name == VIRTUAL_DELAY_1S:
                log.debug("Step %d: virtual delay 1000ms", i)
                time.sleep(1.0)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                continue

            if signal_name == VIRTUAL_DELAY_10S:
                log.debug("Step %d: virtual delay 10000ms", i)
                time.sleep(10.0)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                continue

            if not signal_name:
                if delay_ms > 0:
                    log.debug("Step %d: delay-only step %dms", i, delay_ms)
                    time.sleep(delay_ms / 1000.0)
                else:
                    log.warning("Macro %r step %d has no action — skipping", name, i)
                continue

            try:
                ir = self._store.get(signal_name)
            except SignalNotFoundError:
                raise SignalNotFoundError(
                    f"Macro {name!r} step {i}: signal {signal_name!r} not found in store"
                )

            log.debug("Step %d: sending %r", i, signal_name)
            self._device.send(ir)

            if delay_ms > 0:
                log.debug("Step %d: sleeping %dms", i, delay_ms)
                time.sleep(delay_ms / 1000.0)

        log.info("Macro %r complete", name)


def _validate_macro_name(name: str) -> None:
    if not name or not name.strip():
        raise ValueError("macro name must be non-empty")
    if not all(c.isalnum() or c in "_-" for c in name.strip()):
        raise ValueError(
            "macro name must contain only alphanumeric characters, underscores, or hyphens"
        )
