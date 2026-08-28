"""Persistent chord-practice progress and chapter lesson selection.

This is deliberately a domain service: it consumes only ``ValidatedChord``
results and exposes a lesson map to callers.  Qt presentation, banners, and
session typing statistics remain outside this module.
"""
from dataclasses import dataclass
import json
import logging
import os
from typing import Mapping

from retype.services.chord_detection import ValidatedChord
from retype.services.chord_mastery import (MIN_SUCCESSFUL_USES_FOR_MASTERY,
                                           is_validated_chord_result)
from retype.services.chords import WORD_RE


logger = logging.getLogger(__name__)

MASTERY_PROGRESS_FILENAME = 'chord-mastery.json'
MASTERY_PROGRESS_FORMAT = 1
DEFAULT_LESSON_CHORD_LIMIT = 5


class ChordMasteryStorage:
    """Store cumulative validated-use counts in the user's data directory.

    Unsupported formats and corrupt files are left untouched and made
    read-only for this run.  Valid entries in a partly malformed v1 file are
    usable; malformed entries are preserved verbatim when later counts are
    saved.  This avoids replacing progress that a newer version might know how
    to interpret.
    """

    def __init__(self, user_dir=None):
        # type: (str | None) -> None
        self.path = os.path.join(user_dir, MASTERY_PROGRESS_FILENAME) \
            if user_dir else None
        self._raw_progress = {}  # type: dict[object, object]
        self.writable = True

    def load(self):
        # type: () -> dict[str, int]
        if not self.path or not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError, TypeError) as error:
            self.writable = False
            logger.warning('Chord mastery progress is unreadable; preserving '
                           'it without overwriting: %s', error)
            return {}

        if not isinstance(data, dict) or \
           data.get('version') != MASTERY_PROGRESS_FORMAT or \
           not isinstance(data.get('progress'), dict):
            self.writable = False
            logger.warning('Chord mastery progress has an unsupported format; '
                           'preserving it without overwriting')
            return {}

        self._raw_progress = dict(data['progress'])
        progress = {}
        for key, uses in self._raw_progress.items():
            if isinstance(key, str) and key and \
               isinstance(uses, int) and not isinstance(uses, bool) and \
               uses >= 0:
                progress[key] = uses
            else:
                logger.warning('Ignoring malformed chord mastery progress '
                               'entry %r while preserving it on disk', key)
        return progress

    def save(self, progress):
        # type: (Mapping[str, int]) -> bool
        if not self.path or not self.writable:
            return False
        merged = dict(self._raw_progress)
        merged.update(progress)
        data = {'version': MASTERY_PROGRESS_FORMAT, 'progress': merged}
        try:
            with open(self.path, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2, sort_keys=True)
        except OSError as error:
            logger.warning('Unable to save chord mastery progress: %s', error)
            return False
        self._raw_progress = merged
        return True


@dataclass(frozen=True)
class ChordProgress:
    """Cumulative progress used to complete a lesson target.

    ``is_mastered`` means that the lesson's validated-use target is complete.
    It does not claim device attribution or direct-entry evidence; the
    session-level ``MasteryAssessment`` continues to expose that limitation.
    """

    dictionary_key: str
    successful_uses: int

    @property
    def is_mastered(self):
        # type: (ChordProgress) -> bool
        return self.successful_uses >= MIN_SUCCESSFUL_USES_FOR_MASTERY


class ChordMasteryProgress:
    """Cumulative validated-success counts independent of session statistics."""

    def __init__(self, storage=None):
        # type: (ChordMasteryStorage | None) -> None
        self.storage = storage or ChordMasteryStorage()
        self._uses = self.storage.load()

    def record(self, result):
        # type: (object) -> ChordProgress | None
        """Persist one authoritative success, rejecting every other input."""
        if not is_validated_chord_result(result):
            logger.warning('Ignoring malformed or non-validated chord result '
                           'for persistent mastery progress')
            return None
        assert isinstance(result, ValidatedChord)
        key = result.dictionary_key
        self._uses[key] = self._uses.get(key, 0) + 1
        self.storage.save(self._uses)
        return self.progress_for(key)

    def progress_for(self, dictionary_key):
        # type: (str) -> ChordProgress
        return ChordProgress(dictionary_key, self._uses.get(dictionary_key, 0))

    def all_progress(self):
        # type: () -> dict[str, ChordProgress]
        return {key: ChordProgress(key, uses)
                for key, uses in self._uses.items()}


@dataclass(frozen=True)
class ChordLesson:
    """The chapter's teaching targets and all chord keys eligible for hints."""

    teaching_keys: tuple[str, ...]
    mastered_hint_keys: tuple[str, ...]

    @property
    def hint_keys(self):
        # type: () -> tuple[str, ...]
        return self.teaching_keys + self.mastered_hint_keys


class AdaptiveChordExposure:
    """Choose a small useful lesson without depending on any UI widget."""

    def __init__(self, progress, limit=DEFAULT_LESSON_CHORD_LIMIT):
        # type: (ChordMasteryProgress, int) -> None
        self.progress = progress
        self.limit = max(0, limit)

    @staticmethod
    def _frequencies(chapter_text):
        # type: (str) -> dict[str, int]
        frequencies = {}  # type: dict[str, int]
        for match in WORD_RE.finditer(chapter_text or ''):
            key = match.group().lower()
            frequencies[key] = frequencies.get(key, 0) + 1
        return frequencies

    def select(self, chords, chapter_text='', full_chord_list=False):
        # type: (Mapping[str, object], str, bool) -> ChordLesson
        """Select at most ``limit`` incomplete targets for ``chapter_text``.

        Work already under way precedes unseen words.  New words are ordered
        by their frequency in the chapter, then dictionary key for stable
        results.  Completed targets remain in the hint map but are never
        counted as a new teaching target.  The explicit full-list mode is an
        opt-out and returns every loaded chord as a hint.
        """
        if not chords:
            return ChordLesson((), ())
        keys = sorted(key for key in chords if isinstance(key, str) and key)
        if full_chord_list:
            return ChordLesson((), tuple(keys))

        frequencies = self._frequencies(chapter_text)
        mastered = []
        started = []
        new = []
        for key in keys:
            uses = self.progress.progress_for(key).successful_uses
            if uses >= MIN_SUCCESSFUL_USES_FOR_MASTERY:
                mastered.append(key)
            elif uses:
                started.append(key)
            else:
                new.append(key)

        # Finish started targets first. Frequency remains a useful stable
        # tie-breaker; more completed repetitions are closest to completion.
        started.sort(key=lambda key: (-self.progress.progress_for(key).
                                      successful_uses,
                                      -frequencies.get(key, 0), key))
        new.sort(key=lambda key: (-frequencies.get(key, 0), key))
        return ChordLesson(tuple((started + new)[:self.limit]),
                           tuple(mastered))
