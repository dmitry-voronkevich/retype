"""Regression coverage for adaptive chord presentation refreshes."""

from qt import QFont, QTextCharFormat, QTextCursor

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
