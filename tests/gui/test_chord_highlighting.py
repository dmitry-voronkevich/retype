"""Regression coverage for adaptive chord presentation refreshes."""

import pytest
from qt import QColor, QFont, QTextCharFormat, QTextCursor, Qt

from retype.services.chord_detection import ValidatedChord
from retype.services.chord_mastery import MIN_SUCCESSFUL_USES_FOR_MASTERY
from retype.services.chords import WORD_RE


CHORDS = {
    'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7',
}


def _validated_result(word):
    return ValidatedChord(word, word, word, 0, 0, 20.0, 10.0, False)


def _chapter(text, html=None):
    return {'html': html or '<p>{}</p>'.format(text), 'plain': text,
            'images': []}


def _dotted_words(document):
    words = set()
    text = document.toPlainText()
    for match in WORD_RE.finditer(text):
        cursor = QTextCursor(document)
        cursor.setPosition(match.start())
        cursor.setPosition(match.start() + 1,
                           QTextCursor.MoveMode.KeepAnchor)
        if cursor.charFormat().underlineStyle() == \
                QTextCharFormat.UnderlineStyle.DotLine:
            words.add(match.group().lower())
    return words


def _hinted_words(book_view, line):
    return [word.lower() for word, _, _ in
            book_view.chord_hint_bar.upcomingHints(line, 0)]


def _show_chapter(book_view, chapter):
    text = chapter['plain']
    book_view.current_line = text
    book_view.cursor_pos = book_view.persistent_pos = 0
    book_view.setSource(chapter)


def test_adaptive_mode_toggle_refreshes_hints_and_document_highlights(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    chapter = _chapter(
        'one two three four five six seven',
        '<p><strong>one</strong> two three four five six seven</p>')

    for _ in range(MIN_SUCCESSFUL_USES_FOR_MASTERY):
        book_view.chord_progress.record(_validated_result('two'))
    book_view.setChords(CHORDS)
    _show_chapter(book_view, chapter)
    assert set(book_view.chords) == {
        'five', 'four', 'one', 'seven', 'six', 'two'}
    assert _dotted_words(book_view.display.document()) == \
        set(book_view.chords)
    assert _hinted_words(book_view, chapter['plain']) == \
        ['one', 'two', 'four', 'five']

    book_view.highlight(full=True)
    book_view.setAdaptiveChordLessons(False)
    assert set(book_view.chords) == set(CHORDS)
    assert _dotted_words(book_view.display.document()) == set(CHORDS)
    assert _hinted_words(book_view, chapter['plain']) == \
        ['one', 'two', 'three', 'four']

    # The map and hint bar already narrow here; before the regression fix the
    # old full-map dotted formats were the first state to diverge.
    book_view.setAdaptiveChordLessons(True)
    assert set(book_view.chords) == {
        'five', 'four', 'one', 'seven', 'six', 'two'}
    assert _hinted_words(book_view, chapter['plain']) == \
        ['one', 'two', 'four', 'five']
    assert _dotted_words(book_view.display.document()) == \
        set(book_view.chords)
    assert book_view.display.full_highlight

    one = QTextCursor(book_view.display.document())
    one.setPosition(0)
    one.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
    assert one.charFormat().fontWeight() > QFont.Normal

    book_view.setAdaptiveChordLessons(False)
    assert _dotted_words(book_view.display.document()) == set(CHORDS)


def test_replacing_chords_and_chapters_refreshes_active_highlights(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    first = _chapter('one two three four five six seven')

    book_view.setChords(CHORDS)
    _show_chapter(book_view, first)
    book_view.setAdaptiveChordLessons(False)
    assert _dotted_words(book_view.display.document()) == set(CHORDS)

    book_view.setChords({'seven': '7'})
    assert book_view.chords == {'seven': '7'}
    assert _dotted_words(book_view.display.document()) == {'seven'}
    assert _hinted_words(book_view, first['plain']) == ['seven']

    second = _chapter('two two two seven')
    book_view.setChords(CHORDS)
    book_view.setAdaptiveChordLessons(True)
    _show_chapter(book_view, second)
    assert 'two' in book_view.chords
    assert _dotted_words(book_view.display.document()) == \
        set(book_view.chords).intersection({'two', 'seven'})
    assert _hinted_words(book_view, second['plain'])[0] == 'two'


def test_case_mistake_removal_preserves_chord_highlight(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    chapter = _chapter('one')

    book_view.setChords({})
    _show_chapter(book_view, chapter)
    highlighting_service = controller.console.highlighting_service
    highlighting_service.setIgnoreCapitalLetters(True)
    controller.console.setText('O')

    book_view.setChords({'one': '1'})
    controller.console.setText('')

    cursor = QTextCursor(book_view.display.document())
    cursor.setPosition(0)
    cursor.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
    assert cursor.charFormat().underlineStyle() == \
        QTextCharFormat.UnderlineStyle.DotLine


def test_source_underlines_survive_chord_highlight_refresh(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    chapter = _chapter('one two', '<p><u>one</u> two</p>')

    book_view.setChords({'one': '1', 'two': '2'})
    _show_chapter(book_view, chapter)
    cursor = QTextCursor(book_view.display.document())
    cursor.setPosition(0)
    cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    assert cursor.charFormat().underlineStyle() == \
        QTextCharFormat.UnderlineStyle.DotLine

    book_view.setChords({'two': '2'})
    cursor = QTextCursor(book_view.display.document())
    cursor.setPosition(0)
    cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    assert cursor.charFormat().underlineStyle() == \
        QTextCharFormat.UnderlineStyle.SingleUnderline


def test_empty_adaptive_lesson_map_clears_chord_formats(
        make_controller, qtbot):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    book_view = controller.view()
    chapter = _chapter('one two')

    book_view.setChords({'one': '1', 'two': '2'})
    _show_chapter(book_view, chapter)
    book_view.setAdaptiveChordLessons(False)
    assert _dotted_words(book_view.display.document()) == {'one', 'two'}

    # A zero-target lesson is a valid partial-selection edge case: no hint or
    # inline chord formatting may survive the map becoming empty.
    book_view.chord_exposure.limit = 0
    book_view.setAdaptiveChordLessons(True)
    assert book_view.chords == {}
    assert not book_view.chord_hint_bar.isVisible()
    assert _dotted_words(book_view.display.document()) == set()


def _format(book_view, position):
    cursor = QTextCursor(book_view.display.document())
    cursor.setPosition(position)
    cursor.setPosition(position + 1, QTextCursor.KeepAnchor)
    return cursor.charFormat()


def _prepare_outcomes(make_controller, qtbot, text, html=None):
    controller = make_controller()
    controller.loadBookRequested.emit(0)
    qtbot.wait(20)
    view = controller.view()
    controller.console.clear()
    view.setAdaptiveChordLessons(False)
    view.setChords({'one': '1', 'two': '2'})
    _show_chapter(view, _chapter(text, html))
    view.tobetyped = text
    view.tobetyped_list = [text]
    view.line_pos = 0
    view.progress = 0
    view.setCursor()
    view.stats_dock.resetSession()
    return controller, view


def _type_at_speed(qtbot, monkeypatch, controller, view, text, gap, start=1000):
    timestamps = iter(range(start, start + gap * len(text), gap))
    monkeypatch.setattr(view.stats_dock, '_eventTimestamp',
                        lambda event: next(timestamps))
    qtbot.keyClicks(controller.console, text)


@pytest.mark.parametrize('gap,chorded', [(10, True), (100, False)])
def test_completed_word_underline_uses_validated_outcome(
        make_controller, qtbot, monkeypatch, gap, chorded):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two plain tail',
        '<p><b>one</b> two <i>plain</i> tail</p>')
    future = _format(view, 4)
    plain = _format(view, 8)
    text = view.display.toPlainText()
    _type_at_speed(qtbot, monkeypatch, controller, view, 'one ', gap)

    expected = view.c_chordable.fg() if chorded else view.c_case_mistake.bg()
    for position in range(3):
        result = _format(view, position)
        assert result.underlineColor() == expected
        assert result.underlineStyle() == QTextCharFormat.DotLine
        assert result.fontWeight() > QFont.Normal
    assert view.stats_dock.successful_chords == int(chorded)
    assert _format(view, 4) == future
    assert _format(view, 8) == plain
    assert view.display.toPlainText() == text


def test_outcome_changes_only_affected_span_in_large_chapter(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two plain ' * 1500)
    document = view.display.document()
    changes = []
    document.contentsChange.connect(
        lambda position, removed, added: changes.append(
            (position, removed, added)))

    _type_at_speed(qtbot, monkeypatch, controller, view, 'on', 10)
    assert changes == []  # No word end crossed: not even a format write.
    _type_at_speed(qtbot, monkeypatch, controller, view, 'e ', 10, start=1020)
    assert changes
    assert all(position >= 0 and position + max(removed, added) <= 3
               for position, removed, added in changes)
    assert _format(view, 0).underlineColor() == view.c_chordable.fg()

    changes.clear()
    # Membership has not changed: reloading the same map is also a no-op.
    view.setChords({'one': '1', 'two': '2'})
    assert changes == []


def test_backtracking_and_wrong_text_do_not_damage_other_underlines(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two tail')
    _type_at_speed(qtbot, monkeypatch, controller, view, 'one ', 10)
    controller.console.setText('onX')
    assert view.display.toPlainText() == 'onXe two tail'
    # Future text is shifted by the inserted red mistake, not recolored.
    assert _format(view, 5).underlineColor() == view.c_chordable.fg()
    controller.console.setText('one ')
    assert view.display.toPlainText() == 'one two tail'
    assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
    assert _format(view, 4).underlineColor() == view.c_chordable.fg()


def test_refreshes_rebase_underlines_around_wrong_text(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two tail')
    controller.console.setText('onX')

    view.setChords({'one': '1'})
    view.setChords({'one': '1', 'two': '2'})
    view.setAdaptiveChordLessonLimit(1)
    view.setAdaptiveChordLessons(True)
    view.setAdaptiveChordLessons(False)
    monkeypatch.setattr(view.c_chordable.fg, 'c', QColor('#123456'))
    view.themeUpdate()

    assert view.display.toPlainText() == 'onXe two tail'
    controller.console.setText('one ')
    assert view.display.toPlainText() == 'one two tail'
    assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
    assert _format(view, 4).underlineColor() == view.c_chordable.fg()


def test_refresh_inside_wrong_word_preserves_tail_and_removal(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two tail')
    controller.console.setText('onX')
    view.chord_highlighting.outcomes[0] = False

    monkeypatch.setattr(view.c_chordable.fg, 'c', QColor('#123456'))
    view.themeUpdate()

    assert view.display.toPlainText() == 'onXe two tail'
    for position in (0, 1, 3):
        assert _format(view, position).underlineColor() == \
            view.c_case_mistake.bg()

    controller.console.setText('on')
    assert view.display.toPlainText() == 'one two tail'
    assert _format(view, 2).underlineColor() == view.c_case_mistake.bg()


@pytest.mark.parametrize('ignore_case,expected_position', [(True, 4), (False, 0)])
def test_case_only_input_never_becomes_a_green_success(
        make_controller, qtbot, monkeypatch, ignore_case, expected_position):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'One two tail')
    controller.console.highlighting_service.setIgnoreCapitalLetters(ignore_case)
    _type_at_speed(qtbot, monkeypatch, controller, view, 'one ', 10)
    assert view.cursor_pos == expected_position
    assert view.stats_dock.successful_chords == 0
    if ignore_case:
        assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
        assert _format(view, 0).background().color() == view.c_case_mistake.bg()


def test_projected_line_end_success_survives_console_clear(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(make_controller, qtbot, 'one\n')
    view.chapter_pos = len(view.book.chapters) - 1
    _type_at_speed(qtbot, monkeypatch, controller, view, 'one', 10)
    qtbot.keyClick(controller.console, Qt.Key_Return)
    assert view.progress == 100
    assert view.stats_dock.successful_chords == 1
    assert _format(view, 0).underlineColor() == view.c_chordable.fg()


def test_chapter_and_lesson_refresh_preserve_or_reset_outcomes(
        make_controller, qtbot):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two tail',
        '<p><u>one</u> two tail</p>')
    controller.console.setText('one ')
    assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
    view.setChords({'two': '2'})
    assert _format(view, 0).underlineStyle() == QTextCharFormat.SingleUnderline
    view.setChords({'one': '1', 'two': '2'})
    assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
    view.themeUpdate()
    assert _format(view, 0).underlineColor() == view.c_case_mistake.bg()
    _show_chapter(view, _chapter('one two tail'))
    assert _format(view, 0).underlineColor() == view.c_chordable.fg()


def test_unicode_prose_and_non_chordable_spans_remain_unchanged(
        make_controller, qtbot):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, '\U0001f642 one plain tail')
    unchanged_positions = [0, 1, 2, 6, 7, 8, 9, 10, 11]
    before = {position: _format(view, position)
              for position in unchanged_positions}
    controller.console.setText('\U0001f642 one ')
    for position in (3, 4, 5):
        assert _format(view, position).underlineColor() == \
            view.c_case_mistake.bg()
    for position, source in before.items():
        assert _format(view, position) == source


def test_mastery_transition_adds_only_new_lesson_spans(
        make_controller, qtbot, monkeypatch):
    controller, view = _prepare_outcomes(
        make_controller, qtbot, 'one two plain tail')
    view.setAdaptiveChordLessonLimit(1)
    view.setAdaptiveChordLessons(True)
    assert set(view.chords) == {'one'}
    for _ in range(MIN_SUCCESSFUL_USES_FOR_MASTERY - 1):
        view.chord_progress.record(_validated_result('one'))
    changes = []
    view.display.document().contentsChange.connect(
        lambda position, removed, added: changes.append(
            (position, removed, added)))
    _type_at_speed(qtbot, monkeypatch, controller, view, 'one ', 10)
    assert set(view.chords) == {'one', 'two'}
    assert changes
    assert all(position + max(removed, added) <= 7
               for position, removed, added in changes)
    assert _format(view, 0).underlineColor() == view.c_chordable.fg()
    assert _format(view, 4).underlineStyle() == QTextCharFormat.DotLine
    assert _format(view, 8).underlineStyle() == QTextCharFormat.NoUnderline
