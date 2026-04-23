"""
YAML-backed store for voice command → macro mappings.

File schema (voice_commands.yaml):
  - id: "uuid-string"
    phrase: "turn on the projector"
    macro: "projector_on"

Each entry is assigned a UUID on creation and addressed by that UUID
for updates and deletes so that phrase changes don't break references.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)


class VoiceCommandNotFoundError(KeyError):
    """Raised when a command ID is not found in the store."""


class VoiceCommandStore:
    """
    Thread-safe YAML-backed store for voice command → macro mappings.

    Consumers (e.g. the STT engine) should watch *rebuild_event*:
    the event is set whenever the command list changes, signalling
    that the Vosk vocabulary should be rebuilt.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._commands: List[Dict] = []
        # External consumers wait on this; we set it after every write.
        self.rebuild_event = threading.Event()
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            log.info("Voice command store not found at %s — starting empty", self._path)
            return
        with self._path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or []
        if not isinstance(data, list):
            raise ValueError(f"Expected a YAML sequence in {self._path}")
        self._commands = data
        log.info("Loaded %d voice command(s) from %s", len(self._commands), self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                self._commands, fh, default_flow_style=False, allow_unicode=True
            )
        tmp.replace(self._path)
        log.debug("Saved %d voice command(s) to %s", len(self._commands), self._path)

    def _signal_rebuild(self) -> None:
        """Notify the STT engine that vocabulary needs to be rebuilt."""
        self.rebuild_event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_commands(self) -> List[Dict]:
        """Return a copy of all command entries."""
        with self._lock:
            return [dict(c) for c in self._commands]

    def get(self, command_id: str) -> Dict:
        """
        Return a single command entry by ID.

        Raises VoiceCommandNotFoundError if not found.
        """
        with self._lock:
            for cmd in self._commands:
                if cmd.get("id") == command_id:
                    return dict(cmd)
        raise VoiceCommandNotFoundError(command_id)

    def add(self, phrase: str, macro: str) -> Dict:
        """
        Add a new voice command mapping and persist.

        Returns the new entry (including its assigned ID).
        """
        _validate_phrase(phrase)
        _validate_macro_name(macro)
        entry = {
            "id": str(uuid.uuid4()),
            "phrase": phrase.strip(),
            "macro": macro.strip(),
        }
        with self._lock:
            self._commands.append(entry)
            self._save()
        self._signal_rebuild()
        log.info("Added voice command %r → macro %r (id=%s)", phrase, macro, entry["id"])
        return dict(entry)

    def update(self, command_id: str, phrase: Optional[str] = None, macro: Optional[str] = None) -> Dict:
        """
        Update the phrase and/or macro of an existing command.

        Raises VoiceCommandNotFoundError if the ID is not found.
        Returns the updated entry.
        """
        if phrase is not None:
            _validate_phrase(phrase)
        if macro is not None:
            _validate_macro_name(macro)

        with self._lock:
            for cmd in self._commands:
                if cmd.get("id") == command_id:
                    if phrase is not None:
                        cmd["phrase"] = phrase.strip()
                    if macro is not None:
                        cmd["macro"] = macro.strip()
                    self._save()
                    self._signal_rebuild()
                    log.info("Updated voice command id=%s", command_id)
                    return dict(cmd)
        raise VoiceCommandNotFoundError(command_id)

    def delete(self, command_id: str) -> None:
        """
        Remove a command by ID.

        Raises VoiceCommandNotFoundError if not found.
        """
        with self._lock:
            original_len = len(self._commands)
            self._commands = [c for c in self._commands if c.get("id") != command_id]
            if len(self._commands) == original_len:
                raise VoiceCommandNotFoundError(command_id)
            self._save()
        self._signal_rebuild()
        log.info("Deleted voice command id=%s", command_id)

    def all_phrases(self) -> List[str]:
        """Return all phrase strings (used to build Vosk restricted vocabulary)."""
        with self._lock:
            return [c["phrase"] for c in self._commands if c.get("phrase")]

    def phrase_to_macro(self) -> Dict[str, str]:
        """Return a mapping of phrase → macro name (used by command matcher)."""
        with self._lock:
            return {c["phrase"]: c["macro"] for c in self._commands if c.get("phrase") and c.get("macro")}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_phrase(phrase: str) -> None:
    if not phrase or not phrase.strip():
        raise ValueError("phrase must be a non-empty string")
    if len(phrase) > 200:
        raise ValueError("phrase must be 200 characters or fewer")


def _validate_macro_name(macro: str) -> None:
    if not macro or not macro.strip():
        raise ValueError("macro name must be a non-empty string")
    if not all(c.isalnum() or c in "_-" for c in macro.strip()):
        raise ValueError(
            "macro name must contain only alphanumeric characters, underscores, or hyphens"
        )
