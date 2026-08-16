"""High-value GUI wiring checks; service and pure tests remain separate."""

import json

import pytest
from qt import QWidget

from retype.controllers.main_controller import View
from retype.services.chord_detection import BACKSPACE_KEY


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


def test_launches_shelf_with_bundled_books(controller, qtbot):
    window = controller._window
    shelf = controller.views[View.shelf_view]
    # The controller starts on the shelf and the bundled library is indexed.
    assert window.objectName() == "main-window"
    assert controller.view().objectName() == "shelf-view"
    assert len(controller.library.books) >= 1
    assert shelf.findChild(QWidget, "shelf-item-0") is not None


def test_opens_bundled_book_and_bounded_typing(controller, qtbot):
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.views[View.book_view]
    # View is the public enum in the module; avoid relying on screen position.
    assert controller.view().objectName() == "book-view"
    assert book_view.book is not None
    assert book_view.display.toPlainText()

    initial_position = book_view.cursor_pos
    text = str(book_view.current_line)[:3]
    qtbot.keyClicks(controller.console, text)
    qtbot.wait(20)
    assert book_view.cursor_pos is not None
    assert initial_position is not None
    assert book_view.cursor_pos >= initial_position


def test_opens_customisation_dialog_without_blocking(controller, qtbot):
    controller.customisationDialogRequested.emit()
    dialog = controller.customisation_dialog
    qtbot.wait(20)

    assert dialog.objectName() == "customisation-dialog"
    assert dialog.isVisible()
    assert dialog.isModal()
    dialog.reject()
    qtbot.wait(20)
    assert not dialog.isVisible()


def test_likely_chord_feedback_count_and_chart_segment(make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    stats.resetSession()

    for position, (character, timestamp) in enumerate(
            zip("abcd", (1, 11, 21, 31)), start=1):
        book_view.cursor_pos = position
        stats._onKeyPress(_TimedKeyEvent(character, timestamp))
        stats.onUpdate(character)

    assert stats.likely_chords == 1
    assert stats.wpms_likely_chords[-4:] == [True, True, True, True]
    # Heuristic bursts remain in statistics, but do not get educational
    # feedback without a dictionary and an expected-word match.
    assert not book_view.chord_feedback.isVisible()
    assert "Likely chords: 1" in stats.accessibleDescription()

    stats.resetSession()
    assert stats.likely_chords == 0
    assert stats.wpms_likely_chords == []
    assert not book_view.chord_feedback.isVisible()
    assert not book_view._chord_feedback_timer.isActive()


def test_likely_chord_feedback_hides_after_three_seconds_and_resets_timer(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    stats.resetSession()

    def burst(start, word):
        for position, (character, timestamp) in enumerate(
                zip(word, (start + 1, start + 11, start + 21, start + 31)),
                start=1):
            book_view.cursor_pos = position
            stats._onKeyPress(_TimedKeyEvent(character, timestamp))
            stats.onUpdate(character)

    burst(0, "abcd")
    assert not book_view.chord_feedback.isVisible()
    qtbot.wait(1500)
    burst(200, "efgh")
    assert stats.likely_chords == 2
    assert not book_view.chord_feedback.isVisible()


def test_cleanup_backspaces_count_once_and_mark_one_burst(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    stats.resetSession()
    encouragements = []
    stats.likelyChordDetected.connect(encouragements.append)

    events = [
        _TimedKeyEvent(character, timestamp)
        for character, timestamp in zip("what", (1, 11, 21, 31))
    ] + [
        _TimedKeyEvent("\b", timestamp, BACKSPACE_KEY)
        for timestamp in (41, 43, 45, 47)
    ] + [
        _TimedKeyEvent(character, timestamp)
        for character, timestamp in zip("what", (57, 67, 77, 87))
    ]
    text = ""
    cursor = 0
    for event in events:
        if event.key() == BACKSPACE_KEY:
            text = text[:-1]
            cursor -= 1
        else:
            text += event.text()
            cursor += 1
        book_view.cursor_pos = cursor
        stats._onKeyPress(event)
        # Exercise the textChanged boundary where the final cleanup Backspace
        # would otherwise reset the detector after the key event.
        stats._onTextChanged(text)
        stats.onUpdate(text)

    assert stats.likely_chords == 1
    assert encouragements == [1]
    assert stats.wpms_likely_chords == [True] * 8
    assert not book_view.chord_feedback.isVisible()


def test_known_expected_burst_gets_educational_feedback(make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    book_view.setChords({'the': 't+h+e'})
    book_view.current_line = 'The cat'
    book_view.persistent_pos = 0
    book_view.cursor_pos = 0
    stats.resetSession()

    for character, timestamp in zip('THE', (1, 11, 21)):
        stats._onKeyPress(_TimedKeyEvent(character, timestamp))
        book_view.cursor_pos += 1
        stats.onUpdate(character)

    # The word is not validated at the heuristic's three-character crossing;
    # the delimiter validates the complete final token.
    assert stats.successful_chords == 0
    stats._onKeyPress(_TimedKeyEvent(' ', 31))
    book_view.cursor_pos += 1
    stats.onUpdate(' ')
    assert stats.successful_chords == 1
    assert book_view.chord_feedback.isVisible()
    assert book_view.chord_feedback.text() == 'Chord matched “THE” — nice!'
    assert 'device' not in book_view.chord_feedback.text().lower()


@pytest.mark.parametrize('output, line, chords', [
    ('xyz', 'xyz now', {'the': 't+h+e'}),
    ('the', 'other the', {'the': 't+h+e'}),
    ('the', 'the now', {}),
])
def test_burst_without_known_expected_chord_gets_no_success_feedback(
        make_controller, qtbot, output, line, chords):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    book_view.setChords(chords)
    book_view.current_line = line
    book_view.persistent_pos = 0
    book_view.cursor_pos = 0
    stats.resetSession()

    for character, timestamp in zip(output, (1, 11, 21, 31)):
        stats._onKeyPress(_TimedKeyEvent(character, timestamp))
        book_view.cursor_pos += 1
        stats.onUpdate(character)

    assert stats.successful_chords == 0
    assert not book_view.chord_feedback.isVisible()


@pytest.mark.parametrize('word', ['at', 'with', 'without', 'because'])
@pytest.mark.parametrize('cleanup_gap', [10, 200])
def test_corrected_final_tokens_succeed_once_at_delimiter(
        make_controller, qtbot, word, cleanup_gap):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    book_view.setChords({word: word})
    book_view.current_line = word + ' next'
    book_view.persistent_pos = 0
    book_view.cursor_pos = 0
    stats.resetSession()

    cursor = 0
    timestamp = 1

    def key(text, is_backspace=False):
        nonlocal cursor, timestamp
        event = _TimedKeyEvent(
            '\b' if is_backspace else text, timestamp,
            BACKSPACE_KEY if is_backspace else None)
        stats._onKeyPress(event)
        cursor += -1 if is_backspace else len(text)
        book_view.cursor_pos = cursor
        stats.onUpdate(text)
        timestamp += 10

    # Simulate a corrected output: the prefix is deleted, then the final
    # surviving token is emitted rapidly and validated only at its delimiter.
    prefix = word[:2]
    for character in prefix:
        key(character)
    for _ in prefix:
        key('', is_backspace=True)
        timestamp += cleanup_gap
    for character in word:
        key(character)
    key(' ')

    assert stats.successful_chords == 1
    assert book_view.chord_feedback.text() == \
        'Chord matched “{}” — nice!'.format(word)

    # Continuation observations and another delimiter cannot double-count it.
    key(' ')
    assert stats.successful_chords == 1


def test_slow_final_token_and_wrong_position_do_not_succeed(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    book_view.setChords({'because': 'b+e+c+a+u+s+e'})
    book_view.current_line = 'other because'
    book_view.persistent_pos = 0
    book_view.cursor_pos = 0
    stats.resetSession()

    for index, character in enumerate('because', start=1):
        stats._onKeyPress(_TimedKeyEvent(character, index * 100))
        book_view.cursor_pos = index
        stats.onUpdate(character)
    stats._onKeyPress(_TimedKeyEvent(' ', 801))
    book_view.cursor_pos = 8
    stats.onUpdate(' ')

    assert stats.successful_chords == 0
    assert not book_view.chord_feedback.isVisible()


def test_non_printable_input_clears_pending_likely_segment(make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.views[View.book_view]
    stats = book_view.stats_dock
    stats.resetSession()

    for position, (character, timestamp) in enumerate(
            zip("abc", (1, 11, 21)), start=1):
        book_view.cursor_pos = position
        stats._onKeyPress(_TimedKeyEvent(character, timestamp))
        stats.onUpdate(character)

    stats._onKeyPress(_TimedKeyEvent("", 22))
    book_view.cursor_pos = 4
    stats._onKeyPress(_TimedKeyEvent("d", 23))
    stats.onUpdate("d")

    assert stats.wpms_likely_chords[-1] is False


def test_loads_chords_and_updates_hint_state(make_controller, qtbot, tmp_path):
    chords_path = tmp_path / "chords.json"
    chords_path.write_text(
        json.dumps({"history": [[
            {"type": "chords", "chords": [
                [[116, 104, 101], [116, 104, 101]],
            ]},
            {"type": "layout", "layout": [[
                606, 116, 608, 104, 607, 101,
            ]]},
        ]]}),
        encoding="utf-8",
    )
    controller = make_controller(chords_path)
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    assert book_view.chords == {"the": "t+h+e"}
    assert book_view.chords["the"].device_order == "t+h+e"
    assert book_view.chord_hint_bar.isVisible()
    assert book_view.chord_hint_bar.objectName() == "chord-hint-bar"
    book_view.chord_hint_bar.update_("the", 0)
    assert "device order:" in book_view.chord_hint_bar.text()
