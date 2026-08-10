import pytest

from retype.services.chord_detection import (
    BACKSPACE_KEY, KeyboardChordDetector, MAX_BURST_MILLISECONDS,
    MAX_INTERCHAR_MILLISECONDS,
)


class _TimedKeyEvent:
    def __init__(self, text, timestamp, key=None):
        self._text = text
        self._timestamp = timestamp
        self._key = key if key is not None else (ord(text) if text else 0)

    def text(self):
        return self._text

    def timestamp(self):
        return self._timestamp

    def key(self):
        return self._key


@pytest.fixture
def what_chord_cleanup_sequence():
    """The keyboard-only trace for a what chord's cleanup and final output."""
    return [
        _TimedKeyEvent(character, timestamp)
        for character, timestamp in zip("what", (1, 11, 21, 31))
    ] + [
        _TimedKeyEvent("\b", timestamp, BACKSPACE_KEY)
        for timestamp in (41, 43, 45, 47)
    ] + [
        _TimedKeyEvent(character, timestamp)
        for character, timestamp in zip("what", (57, 67, 77, 87))
    ]


def test_ordinary_character_sequence_stays_unclassified():
    detector = KeyboardChordDetector()

    assert detector.observe("a", 0) is None
    assert detector.observe("b", 100) is None
    assert detector.observe("c", 200) is None


def test_normal_what_typing_stays_unclassified():
    detector = KeyboardChordDetector()

    events = [
        _TimedKeyEvent(character, timestamp)
        for character, timestamp in zip("what", (1, 101, 201, 301))
    ]

    assert [detector.observe_event(event) for event in events] == [
        None, None, None, None]


def test_short_generated_burst_is_likely_chord_once():
    detector = KeyboardChordDetector()

    assert detector.observe("a", 0) is None
    assert detector.observe("b", 10) is None
    first = detector.observe("c", 20)
    continuation = detector.observe("d", 30)

    assert first is not None
    assert first.output == "abc"
    assert first.duration_ms == 20
    assert first.is_new
    assert continuation is not None
    assert continuation.output == "abcd"
    assert not continuation.is_new


def test_intercharacter_boundary_is_inclusive():
    detector = KeyboardChordDetector()

    detector.observe("a", 0)
    detector.observe("b", MAX_INTERCHAR_MILLISECONDS)
    result = detector.observe("c", MAX_INTERCHAR_MILLISECONDS * 2)

    assert result is not None
    assert result.duration_ms == MAX_INTERCHAR_MILLISECONDS * 2


def test_burst_boundary_is_inclusive_and_exceeding_it_resets():
    detector = KeyboardChordDetector()

    for character, timestamp in zip("abcde", (0, 30, 60, 90, 120)):
        result = detector.observe(character, timestamp)
    assert result is not None
    assert result.duration_ms == MAX_BURST_MILLISECONDS

    # The sixth character is still close to the previous one, but the full
    # burst is now too long, so it starts a fresh burst instead.
    assert detector.observe("f", MAX_BURST_MILLISECONDS + 1) is None


def test_mixed_or_unknown_output_cannot_complete_the_previous_burst():
    detector = KeyboardChordDetector()

    detector.observe("a", 0)
    assert detector.observe("", 1) is None
    assert detector.observe("b", 2) is None
    assert detector.observe("c", 3) is None
    # A gap starts another one-character burst rather than joining the
    # characters from before the unknown output.
    assert detector.observe("d", 100) is None


def test_cleanup_backspaces_keep_one_likely_chord_classification(
        what_chord_cleanup_sequence):
    detector = KeyboardChordDetector()

    observations = [
        detector.observe_event(event)
        for event in what_chord_cleanup_sequence
    ]
    new_bursts = [observation for observation in observations
                  if observation is not None and observation.is_new]

    assert len(new_bursts) == 1
    assert detector.has_reported_burst


def test_separate_cleanup_chords_are_independently_classified(
        what_chord_cleanup_sequence):
    detector = KeyboardChordDetector()
    first = list(what_chord_cleanup_sequence)
    second = [
        _TimedKeyEvent(event.text(), event.timestamp() + 200, event.key())
        for event in first
    ]

    observations = [
        detector.observe_event(event) for event in first + second]
    new_bursts = [observation for observation in observations
                  if observation is not None and observation.is_new]

    assert len(new_bursts) == 2


def test_reset_starts_a_new_session_context():
    detector = KeyboardChordDetector()

    detector.observe("a", 0)
    detector.observe("b", 1)
    detector.reset()

    assert detector.observe("c", 2) is None
    assert detector.observe("d", 3) is None
    assert detector.observe("e", 4) is not None
