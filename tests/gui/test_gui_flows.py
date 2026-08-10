"""High-value GUI wiring checks; service and pure tests remain separate."""

import json

from qt import QWidget

from retype.controllers.main_controller import View


class _TimedKeyEvent:
    def __init__(self, text, timestamp):
        self._text = text
        self._timestamp = timestamp

    def text(self):
        return self._text

    def timestamp(self):
        return self._timestamp


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
    assert book_view.chord_feedback.isVisible()
    assert "Likely chord burst" in book_view.chord_feedback.text()
    assert "Likely chords: 1" in book_view.chord_feedback.text()
    assert "Likely chords: 1" in stats.accessibleDescription()

    stats.resetSession()
    assert stats.likely_chords == 0
    assert stats.wpms_likely_chords == []
    assert not book_view.chord_feedback.isVisible()


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
