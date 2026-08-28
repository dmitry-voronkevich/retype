import logging
from math import nan

from retype.services.chord_detection import LikelyChordBurst, ValidatedChord
from retype.services.chord_mastery import (
    ChordMasteryTracker, DIRECT_ENTRY_EVIDENCE_LIMITATION,
    MIN_SUCCESSFUL_USES_FOR_MASTERY,
)


def _result(word='the', duration_ms=20.0, max_intercharacter_ms=10.0,
            book_cursor=4, editor_token_start=2):
    return ValidatedChord(
        word, word.lower(), word, book_cursor, editor_token_start,
        duration_ms, max_intercharacter_ms, False)


def test_records_validated_chord_progress_with_timing_and_identity(caplog):
    tracker = ChordMasteryTracker()

    with caplog.at_level(logging.DEBUG):
        first = tracker.record(_result(duration_ms=30.0,
                                       max_intercharacter_ms=15.0))
        second = tracker.record(_result(duration_ms=20.0,
                                        max_intercharacter_ms=8.0,
                                        book_cursor=20,
                                        editor_token_start=3))

    assert first is not None
    assert second is not None
    assert second.dictionary_key == 'the'
    assert second.successful_uses == 2
    assert second.words == ('the',)
    assert second.expected_words == ('the',)
    assert second.total_duration_ms == 50.0
    assert second.fastest_duration_ms == 20.0
    assert second.slowest_duration_ms == 30.0
    assert second.average_duration_ms == 25.0
    assert second.last_duration_ms == 20.0
    assert second.last_max_intercharacter_ms == 8.0
    assert second.last_book_cursor == 20
    assert second.last_editor_token_start == 3
    assert 'Chord mastery progress' in caplog.text


def test_chord_identities_accumulate_independently():
    tracker = ChordMasteryTracker()

    tracker.record(_result('the'))
    tracker.record(_result('and', duration_ms=25.0))
    tracker.record(_result('the', duration_ms=15.0))

    assert tracker.progress_for('the').successful_uses == 2
    assert tracker.progress_for('and').successful_uses == 1
    assert set(tracker.all_progress()) == {'the', 'and'}


def test_success_threshold_creates_candidate_but_not_mastery(caplog):
    tracker = ChordMasteryTracker()

    for _ in range(MIN_SUCCESSFUL_USES_FOR_MASTERY - 1):
        progress = tracker.record(_result())

    assert progress is not None
    assert not progress.mastery.meets_success_threshold
    assert not progress.mastery.is_mastered

    with caplog.at_level(logging.DEBUG):
        progress = tracker.record(_result())

    assert progress is not None
    assert progress.mastery.meets_success_threshold
    assert progress.mastery.direct_entry_evidence is None
    assert not progress.mastery.is_mastered
    assert progress.mastery.limitation == DIRECT_ENTRY_EVIDENCE_LIMITATION
    assert 'Chord mastery candidate' in caplog.text
    assert DIRECT_ENTRY_EVIDENCE_LIMITATION in caplog.text


def test_reset_discards_only_session_progress():
    tracker = ChordMasteryTracker()
    tracker.record(_result())

    tracker.reset()

    assert tracker.progress_for('the') is None
    assert tracker.all_progress() == {}


def test_non_validated_or_malformed_events_do_not_change_progress(caplog):
    tracker = ChordMasteryTracker()
    malformed = [
        None,
        LikelyChordBurst('the', 20.0, True),
        _result(duration_ms=nan),
        ValidatedChord('', 'the', '', 0, 0, 20.0, 10.0, False),
        ValidatedChord('the', 'different', 'the', 0, 0, 20.0, 10.0, False),
        ValidatedChord('the', 'the', 'other', 0, 0, 20.0, 10.0, False),
        ValidatedChord('the', 'the', 'the', -1, 0, 20.0, 10.0, False),
        _result(duration_ms=10.0, max_intercharacter_ms=20.0),
        ValidatedChord('the', 'the', 'the', 0, 0, 20.0, 10.0, None),
    ]

    with caplog.at_level(logging.WARNING):
        for result in malformed:
            assert tracker.record(result) is None

    assert tracker.all_progress() == {}
    assert caplog.text.count('Ignoring malformed or non-validated chord result') \
        == len(malformed)
