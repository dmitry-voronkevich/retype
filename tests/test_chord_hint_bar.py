from qt import QApplication

from retype.services.chords import Chord, parse_layout
from retype.services.theme import Theme
from retype.ui import ChordHintBar

app = QApplication.instance() or QApplication([])

CHORDS = {
    "hello": "h+e+l+o",
    "world": "w+o+r+l+d",
    "again": "a+g+n",
}


def _bar():
    return ChordHintBar(dict(CHORDS))


def _dual_bar():
    layout = parse_layout([[
        606, ord('o'), 608, ord('e'), 607, ord('h'), 605, ord('l'),
    ]])
    return ChordHintBar({
        "hello": Chord("h+e+l+o", [ord(c) for c in "lhoe"],
                        "o+e+h+l", layout),
        "world": Chord("w+o+r+l+d", [ord(c) for c in "world"],
                        "o+w+r+l+d", layout),
        "again": Chord("a+g+n", [ord(c) for c in "agn"],
                        "a+g+n", layout),
    })


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


def test_all_listed_hints_have_aligned_semantic_dual_rows():
    bar = _dual_bar()
    bar.update_("hello world again", 0)
    markup = bar.text()
    assert markup.count('class="chord-hint"') == 3
    assert markup.count('class="chord-word-order"') == 6
    assert markup.count('class="chord-device-order"') == 6
    assert "hello → h+e+l+o" in markup
    assert "o+e+h+l" in markup
    assert "world → w+o+r+l+d" in markup
    assert "o+w+r+l+d" in markup
    assert "word order:" in markup
    assert "device order:" in markup
    assert "role=\"group\"" in markup
    assert "word order" in bar.accessibleDescription()
    assert "device order" in bar.accessibleDescription()


def test_missing_device_order_omits_the_secondary_row():
    bar = _bar()
    bar.update_("hello world", 0)
    assert "device order:" not in bar.text()


def test_theme_has_separate_readable_semantic_roles():
    assert Theme.get('BookView.ChordHintBar.WordOrder').fg().isValid()
    assert Theme.get('BookView.ChordHintBar.DeviceOrder').fg().isValid()
    assert Theme.get('BookView.ChordHintBar.Active').fg().isValid()
    assert Theme.get('BookView.ChordHintBar.WordOrder').fg().name() != \
        Theme.get('BookView.ChordHintBar.DeviceOrder').fg().name()


def test_rich_text_escapes_chord_notation():
    bar = ChordHintBar({"hello": "<b>&+o"})
    bar.update_("hello", 0)
    assert "&lt;b&gt;&amp;+o" in bar.text()
    assert "<b>&+o" not in bar.text()
    assert bar.wordWrap()
