"""Session-only, domain-level progress tracking for validated chords.

This service intentionally consumes only :class:`ValidatedChord` results.  It
has no Qt, console, banner, or statistics-dock dependencies, so a future
mastery UI or lesson selector can consume its snapshots without inheriting UI
lifecycle concerns.
"""
from dataclasses import dataclass
import logging
from math import isfinite

from retype.services.chord_detection import ValidatedChord


logger = logging.getLogger(__name__)

MIN_SUCCESSFUL_USES_FOR_MASTERY = 10

# A validated result proves bounded rapid output, but it cannot tell whether a
# person, macro, or chord device generated that character stream.  Keep this
# explicit rather than treating a timing heuristic as evidence of direct entry.
DIRECT_ENTRY_EVIDENCE_LIMITATION = (
    "ValidatedChord timing cannot reliably establish that a word was not "
    "entered character-by-character.")


@dataclass(frozen=True)
class MasteryAssessment:
    """Conservative mastery result for one chord in the current session."""

    meets_success_threshold: bool
    direct_entry_evidence: bool | None
    is_mastered: bool
    limitation: str | None


@dataclass(frozen=True)
class ChordSessionStats:
    """A read-only per-chord snapshot accumulated during this session."""

    dictionary_key: str
    successful_uses: int
    words: tuple[str, ...]
    expected_words: tuple[str, ...]
    total_duration_ms: float
    fastest_duration_ms: float
    slowest_duration_ms: float
    last_duration_ms: float
    last_max_intercharacter_ms: float
    last_book_cursor: int
    last_editor_token_start: int
    mastery: MasteryAssessment

    @property
    def average_duration_ms(self):
        # type: (ChordSessionStats) -> float
        return self.total_duration_ms / self.successful_uses


@dataclass
class _AccumulatedChordStats:
    """Mutable implementation detail used to create immutable snapshots."""

    successful_uses: int = 0
    words: set[str] | None = None
    expected_words: set[str] | None = None
    total_duration_ms: float = 0.0
    fastest_duration_ms: float | None = None
    slowest_duration_ms: float | None = None
    last_duration_ms: float = 0.0
    last_max_intercharacter_ms: float = 0.0
    last_book_cursor: int = 0
    last_editor_token_start: int = 0

    def __post_init__(self):
        # type: (_AccumulatedChordStats) -> None
        if self.words is None:
            self.words = set()
        if self.expected_words is None:
            self.expected_words = set()


class ChordMasteryTracker:
    """Accumulate session progress from authoritative chord-success events.

    A chord needs at least ``MIN_SUCCESSFUL_USES_FOR_MASTERY`` successes and
    reliable evidence that its word is no longer entered character-by-character.
    ``ValidatedChord`` deliberately does not provide that second kind of
    evidence.  Consequently this first implementation records threshold
    candidates but never declares a chord mastered.
    """

    def __init__(self):
        # type: (ChordMasteryTracker) -> None
        self.reset()

    def reset(self):
        # type: (ChordMasteryTracker) -> None
        """Discard all session-only progress without writing persistent data."""
        self._stats = {}  # type: dict[str, _AccumulatedChordStats]
        logger.debug("Chord mastery session reset")

    @staticmethod
    def _valid_result(result):
        # type: (object) -> bool
        if not isinstance(result, ValidatedChord):
            return False
        if not all(isinstance(value, str) and value for value in (
                result.word, result.dictionary_key, result.expected_word)):
            return False
        if result.word != result.expected_word or \
                result.dictionary_key != result.word.lower():
            return False
        if not isinstance(result.completed_on_line_end, bool):
            return False
        if not all(isinstance(value, int) and not isinstance(value, bool) and
                   value >= 0 for value in (
                       result.book_cursor, result.editor_token_start)):
            return False
        if not all(isinstance(value, (int, float)) and
                   not isinstance(value, bool) and isfinite(value) and
                   value >= 0 for value in (
                       result.duration_ms, result.max_intercharacter_ms)):
            return False
        return result.max_intercharacter_ms <= result.duration_ms

    @staticmethod
    def _mastery(successful_uses):
        # type: (int) -> MasteryAssessment
        meets_threshold = successful_uses >= MIN_SUCCESSFUL_USES_FOR_MASTERY
        return MasteryAssessment(
            meets_success_threshold=meets_threshold,
            direct_entry_evidence=None,
            is_mastered=False,
            limitation=DIRECT_ENTRY_EVIDENCE_LIMITATION if meets_threshold
            else None)

    def _snapshot(self, dictionary_key, stats):
        # type: (str, _AccumulatedChordStats) -> ChordSessionStats
        return ChordSessionStats(
            dictionary_key=dictionary_key,
            successful_uses=stats.successful_uses,
            words=tuple(sorted(stats.words or ())),
            expected_words=tuple(sorted(stats.expected_words or ())),
            total_duration_ms=stats.total_duration_ms,
            fastest_duration_ms=stats.fastest_duration_ms or 0.0,
            slowest_duration_ms=stats.slowest_duration_ms or 0.0,
            last_duration_ms=stats.last_duration_ms,
            last_max_intercharacter_ms=stats.last_max_intercharacter_ms,
            last_book_cursor=stats.last_book_cursor,
            last_editor_token_start=stats.last_editor_token_start,
            mastery=self._mastery(stats.successful_uses))

    def record(self, result):
        # type: (object) -> ChordSessionStats | None
        """Record one validated success and return its updated snapshot.

        Timing observations, ordinary typing, and malformed event objects are
        rejected before they can affect a chord's successful-use count.
        """
        if not self._valid_result(result):
            logger.warning("Ignoring malformed or non-validated chord result")
            return None

        # ``_valid_result`` narrows this at runtime while preserving an object
        # input boundary that safely rejects accidental timing observations.
        assert isinstance(result, ValidatedChord)
        stats = self._stats.setdefault(result.dictionary_key,
                                       _AccumulatedChordStats())
        was_candidate = stats.successful_uses >= MIN_SUCCESSFUL_USES_FOR_MASTERY
        stats.successful_uses += 1
        assert stats.words is not None
        assert stats.expected_words is not None
        stats.words.add(result.word)
        stats.expected_words.add(result.expected_word)
        stats.total_duration_ms += float(result.duration_ms)
        stats.fastest_duration_ms = min(
            stats.fastest_duration_ms, float(result.duration_ms)) \
            if stats.fastest_duration_ms is not None else float(result.duration_ms)
        stats.slowest_duration_ms = max(
            stats.slowest_duration_ms, float(result.duration_ms)) \
            if stats.slowest_duration_ms is not None else float(result.duration_ms)
        stats.last_duration_ms = float(result.duration_ms)
        stats.last_max_intercharacter_ms = float(result.max_intercharacter_ms)
        stats.last_book_cursor = result.book_cursor
        stats.last_editor_token_start = result.editor_token_start

        snapshot = self._snapshot(result.dictionary_key, stats)
        logger.debug(
            "Chord mastery progress key=%r uses=%d/%d word=%r duration_ms=%.1f "
            "max_intercharacter_ms=%.1f mastered=%s",
            snapshot.dictionary_key, snapshot.successful_uses,
            MIN_SUCCESSFUL_USES_FOR_MASTERY, result.word,
            snapshot.last_duration_ms, snapshot.last_max_intercharacter_ms,
            snapshot.mastery.is_mastered)
        if snapshot.mastery.meets_success_threshold and not was_candidate:
            logger.debug(
                "Chord mastery candidate key=%r reached %d successful uses; "
                "not mastered: %s", snapshot.dictionary_key,
                snapshot.successful_uses, DIRECT_ENTRY_EVIDENCE_LIMITATION)
        return snapshot

    def progress_for(self, dictionary_key):
        # type: (str) -> ChordSessionStats | None
        """Return the current snapshot for a normalized chord identity."""
        stats = self._stats.get(dictionary_key)
        return self._snapshot(dictionary_key, stats) if stats is not None \
            else None

    def all_progress(self):
        # type: (ChordMasteryTracker) -> dict[str, ChordSessionStats]
        """Return a copy of all current-session snapshots keyed by chord."""
        return {key: self._snapshot(key, stats)
                for key, stats in self._stats.items()}
