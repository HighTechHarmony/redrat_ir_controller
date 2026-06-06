"""
Fuzzy command matcher using rapidfuzz.

Maps a transcribed phrase to a macro name by fuzzy-matching against all
registered voice commands.  Uses token_set_ratio so word order and
minor STT errors (extra words, transpositions) don't prevent a match.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

from rapidfuzz import fuzz, process, utils

log = logging.getLogger(__name__)


class CommandNotMatchedError(Exception):
    """Raised when no voice command phrase matches the transcript above threshold."""


class CommandMatcher:
    """
    Matches a transcription string to the closest registered phrase and
    dispatches the mapped macro via *run_macro*.

    Parameters
    ----------
    get_phrase_map:
        Callable returning the current {phrase: macro_name} dict.
        Called on every match so changes via the API take effect immediately.
    run_macro:
        Callable that accepts a macro name and executes it.
    threshold:
        Minimum rapidfuzz token_set_ratio score (0–100) to accept a match.
    """

    def __init__(
        self,
        get_phrase_map: Callable[[], Dict[str, str]],
        run_macro: Callable[[str], None],
        threshold: float = 70.0,
    ) -> None:
        self._get_phrase_map = get_phrase_map
        self._run_macro = run_macro
        self._threshold = threshold

    # ------------------------------------------------------------------

    def handle(self, transcript: str) -> None:
        """
        Try to match *transcript* to a command and run the mapped macro.

        If the transcript contains Vosk's out-of-vocabulary marker [unk],
        only the text *before* the first [unk] is used for matching.
        This allows trailing noise ("switch to computer [unk]") while
        rejecting cases where a key word was unrecognised ("switch to [unk]"
        should not match "switch to computer").

        Raises CommandNotMatchedError if no match is found above threshold.
        Lets macro execution exceptions propagate up to the caller.
        """
        match_text = transcript
        if "[unk]" in transcript.lower():
            idx = transcript.lower().index("[unk]")
            prefix = transcript[:idx].strip()
            if not prefix:
                log.info(
                    "Transcript starts with [unk] — rejecting (transcript=%r)",
                    transcript,
                )
                raise CommandNotMatchedError(
                    f"Transcript {transcript!r} starts with unrecognised word"
                )
            log.debug(
                "Stripped [unk] suffix from transcript: %r → prefix %r",
                transcript,
                prefix,
            )
            match_text = prefix

        phrase_map = self._get_phrase_map()
        if not phrase_map:
            log.warning("No voice commands registered — ignoring transcript %r", transcript)
            raise CommandNotMatchedError("No voice commands registered")

        matched_phrase, macro_name = self._match(transcript, phrase_map)
        if matched_phrase is None:
            log.info(
                "No command matched for transcript %r (threshold=%d)",
                transcript,
                self._threshold,
            )
            raise CommandNotMatchedError(
                f"No match found for transcript {transcript!r} (threshold={self._threshold})"
            )

        log.info(
            "Matched %r → macro %r (phrase=%r)",
            transcript,
            macro_name,
            matched_phrase,
        )
        self._run_macro(macro_name)

    def _match(
        self, transcript: str, phrase_map: Dict[str, str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Return (matched_phrase, macro_name) or (None, None) if below threshold.
        """
        result = process.extractOne(
            transcript,
            list(phrase_map.keys()),
            scorer=fuzz.token_set_ratio,
            processor=utils.default_process,   # lowercase + strip punctuation
            score_cutoff=self._threshold,
        )
        if result is None:
            return None, None

        matched_phrase, _score, _idx = result
        return matched_phrase, phrase_map[matched_phrase]
