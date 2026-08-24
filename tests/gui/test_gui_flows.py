"""High-value GUI wiring checks; service and pure tests remain separate."""

import json

import pytest
from qt import Qt, QWidget

from retype.controllers.main_controller import View
from retype.services.chord_detection import BACKSPACE_KEY, ValidatedChord


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


def test_timing_observations_do_not_affect_user_facing_chord_count(
        make_controller, qtbot):
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

    assert stats.likely_chords == 1  # internal diagnostic only
    assert stats.successful_chords == 0
    assert stats.chordCountText() == "Chords: 0"
    assert "Chords: 0" in stats.accessibleDescription()
    assert "Likely" not in stats.accessibleDescription()
    assert stats.wpms_validated_chords[-4:] == [False, False, False, False]
    # Timing observations are not congratulations or user-facing chords.
    assert not book_view.chord_feedback.isVisible()

    stats.resetSession()
    assert stats.likely_chords == 0
    assert stats.chordCountText() == "Chords: 0"
    assert stats.wpms_validated_chords == []
    assert not book_view.chord_feedback.isVisible()
    assert not book_view._chord_feedback_timer.isActive()


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
    assert stats.wpms_validated_chords == [False] * 8
    # The neutral Likely count/chart remains independent of congratulations.
    assert not book_view.chord_feedback.isVisible()


def test_non_printable_input_does_not_colour_validated_segment(
        make_controller, qtbot):
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

    assert stats.wpms_validated_chords[-1] is False


def _validated_result(word='the', completed_on_line_end=False):
    return ValidatedChord(
        word, word.lower(), word, 0, 0, 20.0, 10.0,
        completed_on_line_end)


def _prepare_chord_word(controller, word, delimiter=' '):
    """Arm the real console/highlighter path for one isolated book token."""
    book_view = controller.views[View.book_view]
    controller.console.clear()
    book_view.setChords({word.lower(): 'x+y'})
    book_view.tobetyped = word + delimiter
    book_view.tobetyped_list = [word + delimiter]
    book_view.current_line = word + delimiter
    book_view.cursor_pos = 0
    book_view.persistent_pos = 0
    book_view.line_pos = 0
    book_view.progress = 0
    book_view.stats_dock.resetSession()
    return book_view, book_view.stats_dock


def _type_rapidly(qtbot, monkeypatch, console, stats, text, timestamps):
    iterator = iter(timestamps)
    monkeypatch.setattr(stats, '_eventTimestamp', lambda event: next(iterator))
    qtbot.keyClicks(console, text)


@pytest.mark.parametrize('word', [
    'the', 'and', 'for', 'was', 'without', 'because', 'with', 'at', "can't",
])
def test_study_words_congratulate_only_after_surviving_token_delimiter(
        make_controller, qtbot, monkeypatch, word):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view, stats = _prepare_chord_word(controller, word)
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))

    _type_rapidly(
        qtbot, monkeypatch, controller.console, stats, word,
        range(1, len(word) * 10, 10))
    assert successes == []
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)

    assert successes == [word]
    assert book_view.chord_feedback.isVisible()
    assert word in book_view.chord_feedback.text()


def test_validated_chord_event_drives_banner_counter_and_chart(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    stats = book_view.stats_dock
    stats.resetSession()
    # Existing word bars simulate the completed editor token at its delimiter.
    stats.wpms = [10, 20, 30]
    stats.wpms_validated_chords = [False, False, False]
    observed = []
    stats.validatedChordDetected.connect(observed.append)
    result = _validated_result('the')

    stats.validatedChordDetected.emit(result)

    assert observed == [result]
    assert stats.successful_chords == 1
    assert stats.chordCountText() == "Chords: 1"
    assert stats.wpms_validated_chords == [True, True, True]
    assert book_view.chord_feedback.isVisible()
    assert 'the' in book_view.chord_feedback.text()
    assert result.dictionary_key == 'the'
    assert result.expected_word == 'the'
    assert result.duration_ms == 20.0

    # A timing-only observation remains diagnostic: it changes neither the
    # validated counter, green outcome segments, nor the congratulations UI.
    stats.resetSession()
    for position, (character, timestamp) in enumerate(
            zip('abcd', (1, 11, 21, 31)), start=1):
        book_view.cursor_pos = position
        stats._onKeyPress(_TimedKeyEvent(character, timestamp))
        stats.onUpdate(character)
    assert stats.likely_chords == 1
    assert stats.successful_chords == 0
    assert stats.chordCountText() == "Chords: 0"
    assert stats.wpms_validated_chords == [False] * 4
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_processing_console_clear_preserves_success_feedback(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    stats = book_view.stats_dock
    stats.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()

    controller.console._processing_key_press = True
    controller.console.clear()
    controller.console._processing_key_press = False

    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_cleanup_prefixes_and_cleanup_gaps_preserve_final_word_only(
        make_controller, qtbot, monkeypatch):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    _, stats = _prepare_chord_word(controller, 'with')
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))

    # The study contains cleanup prefixes; an intentionally long pause during
    # cleanup does not count against the surviving final token's timing.
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'thiw',
                  (1, 11, 21, 31))
    for _ in range(4):
        qtbot.keyClick(controller.console, Qt.Key.Key_Backspace)
    qtbot.wait(210)
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'with',
                  (100, 110, 120, 130))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)

    assert successes == ['with']


def test_wrong_prefix_and_timing_reset_never_congratulate_suffix(
        make_controller, qtbot, monkeypatch):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    _, stats = _prepare_chord_word(controller, 'at')
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))

    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'xat',
                  (1, 201, 211))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)

    assert successes == []


def test_slow_wrong_and_wrong_cursor_outputs_do_not_congratulate(
        make_controller, qtbot, monkeypatch):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    _, stats = _prepare_chord_word(controller, 'the')
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))

    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'the',
                  (1, 50, 60))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)
    assert successes == []

    _, stats = _prepare_chord_word(controller, 'the')
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'and',
                  (100, 110, 120))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)
    assert successes == []

    book_view, stats = _prepare_chord_word(controller, 'at')
    # The token is correct in the editor but the saved book cursor is not at
    # its word boundary, so exact cursor matching must reject it.
    book_view.cursor_pos = book_view.persistent_pos = 1
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'at',
                  (200, 210))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)
    assert successes == []


def test_punctuation_newline_and_repeated_delimiters_finalize_once(
        make_controller, qtbot, monkeypatch):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    _, stats = _prepare_chord_word(controller, 'the', '.')
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'the',
                  (1, 11, 21))
    qtbot.keyClick(controller.console, Qt.Key.Key_Period)
    qtbot.keyClick(controller.console, Qt.Key.Key_Period)
    assert successes == ['the']

    book_view, stats = _prepare_chord_word(controller, 'at', '\n')
    # This is also the final line/chapter path, which clears/marks completion
    # during the Return event after the candidate has been finalized.
    book_view.chapter_pos = len(book_view.book.chapters) - 1
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'at',
                  (100, 110))
    # The non-breaking-space-stripped completion path may finalize at the
    # final character; Return must not emit a second success.
    qtbot.keyClick(controller.console, Qt.Key.Key_Return)
    assert successes == ['the', 'at']
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()
    assert book_view.progress == 100

    controller.console.clear()
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_nonempty_mutation_selection_replacement_and_reset_fail_closed(
        make_controller, qtbot, monkeypatch):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view, stats = _prepare_chord_word(controller, 'the')
    successes = []
    stats.validatedChordDetected.connect(
        lambda result: successes.append(result.word))

    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 't', (1,))
    controller.console.setText('the')  # paste/setText/IME-like mutation
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)
    assert successes == []

    _prepare_chord_word(controller, 'the')
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 't', (10,))
    cursor = controller.console.textCursor()
    cursor.select(cursor.SelectionType.Document)
    controller.console.setTextCursor(cursor)
    _type_rapidly(qtbot, monkeypatch, controller.console, stats, 'he',
                  (20, 30))
    qtbot.keyClick(controller.console, Qt.Key.Key_Space)
    assert successes == []

    stats.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()
    stats.resetSession()
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_console_clear_preserves_chord_feedback(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    stats = book_view.stats_dock
    # Console clearing is ordinary editor state now; it has no dedicated
    # lifecycle signal or automatic-mode argument for chord feedback.
    assert not hasattr(controller.console, 'cleared')
    stats.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()

    controller.console.clear()

    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_chord_feedback_hides_when_timer_expires(make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    book_view.stats_dock.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()

    qtbot.wait(3100)

    assert not book_view.chord_feedback.isVisible()
    assert not book_view._chord_feedback_timer.isActive()


def test_automatic_chapter_advance_preserves_chord_feedback(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    if len(book_view.book.chapters) < 2:
        pytest.skip('bundled book has no automatic chapter transition')
    stats = book_view.stats_dock
    stats.validatedChordDetected.emit(_validated_result('the'))
    assert stats.successful_chords == 1
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()

    book_view.nextChapter(move_cursor=True, automatic=True)

    assert stats.successful_chords == 1
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_cursor_moving_chapter_reset_preserves_chord_feedback(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    stats = book_view.stats_dock
    stats.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()

    book_view.setChapter(book_view.chapter_pos, move_cursor=True)

    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


def test_nonmoving_chapter_navigation_preserves_chord_feedback(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)

    book_view = controller.view()
    if len(book_view.book.chapters) < 2:
        pytest.skip('bundled book has no chapter navigation target')
    stats = book_view.stats_dock
    stats.validatedChordDetected.emit(_validated_result('the'))
    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()

    book_view.nextChapter()

    assert book_view.chord_feedback.isVisible()
    assert book_view._chord_feedback_timer.isActive()


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
