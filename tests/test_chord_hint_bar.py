from qt import QApplication

from retype.ui import ChordHintBar

app = QApplication.instance() or QApplication([])

CHORDS = {
    "hello": "h+e+l+o",
    "world": "w+o+r+l+d",
    "again": "a+g+n",
}


def _bar():
    return ChordHintBar(dict(CHORDS))


def test_active_word_is_the_one_being_typed():
    bar = _bar()
    # Cursor 3 chars into "hello" -> "hello" is the active hint.
    hints = bar.upcomingHints("hello world again", 3)
    assert hints[0] == ("hello", "h+e+l+o", True)


def test_finished_word_is_skipped_next_becomes_active():
    bar = _bar()
    # Just finished "hello " (offset 6, on the space) -> "world" is active.
    hints = bar.upcomingHints("hello world again", 6)
    assert hints[0] == ("world", "w+o+r+l+d", True)
    assert ("again", "a+g+n", False) in hints


def test_words_without_a_known_chord_are_omitted():
    bar = _bar()
    # "the" and "of" have no chord; only "world" should show.
    hints = bar.upcomingHints("the world of", 0)
    assert hints == [("world", "w+o+r+l+d", True)]


def test_case_insensitive_lookup():
    bar = _bar()
    hints = bar.upcomingHints("Hello", 0)
    assert hints == [("Hello", "h+e+l+o", True)]


def test_no_hints_when_map_empty():
    bar = ChordHintBar({})
    assert bar.upcomingHints("hello world", 0) == []
    assert not bar.isVisible()
