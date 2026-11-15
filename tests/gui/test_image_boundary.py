"""GUI coverage for typing across parsed image boundaries."""

import pytest
from ebooklib import epub
from qt import QImage

from retype.controllers.main_controller import View


def _show_parsed_chapter(controller, tmp_path, body):
    controller.loadBookRequested.emit(0)
    view = controller.views[View.book_view]
    book = view.book
    image_path = tmp_path / 'image.png'
    image = QImage(2, 2, QImage.Format_RGB32)
    image.fill(0xff336699)
    assert image.save(str(image_path))
    book._images = [epub.EpubImage(
        file_name='image.png', media_type='image/png',
        content=image_path.read_bytes())]
    chapter = epub.EpubHtml(file_name='chapter.xhtml')
    chapter.content = ('<html><body>' + body + '</body></html>').encode()
    parsed = book._parseChaptersContent([chapter])[0]
    assert len(parsed['images']) == body.count('<img')
    book._chapters = [parsed]
    book.progress = 0
    view.setBook(book)
    return view, parsed


@pytest.mark.parametrize('middle', [
    '<p><img src="image.png"/></p>',
    '<p><img src="image.png"/></p><p><img src="image.png"/></p>',
])
def test_isolated_image_boundary(controller, qtbot, tmp_path, middle):
    view, chapter = _show_parsed_chapter(
        controller, tmp_path,
        '<p>First sentence.</p>' + middle +
        '<p>Next sentence. Final sentence.</p>')
    qtbot.keyClicks(controller.console, 'First sentence.')
    expected = chapter['plain'].index('Next sentence.')
    assert str(view.current_line).startswith('Next sentence.')
    assert view.cursor_pos == view.persistent_pos == expected
    assert view.progress == pytest.approx(100 * expected / len(chapter['plain']))
    assert view.display.toPlainText() == chapter['plain']
    assert controller.console.text() == ''


def test_inline_image_is_not_auto_skipped(controller, qtbot, tmp_path):
    view, chapter = _show_parsed_chapter(
        controller, tmp_path,
        '<p>First <img src="image.png"/> sentence. Next sentence.</p>')
    qtbot.keyClicks(controller.console, 'First ')
    assert view.cursor_pos == len('First ')
    assert view.persistent_pos == 0
    assert view.progress == 0
    assert view.display.toPlainText() == chapter['plain']
    assert 'sentence.' in str(view.current_line)


def test_wrong_input_is_cleaned_when_skipping_image(controller, qtbot, tmp_path):
    view, chapter = _show_parsed_chapter(
        controller, tmp_path,
        '<p>First sentence.</p><p><img src="image.png"/></p>'
        '<p>Next sentence. Final sentence.</p>')
    qtbot.keyClicks(controller.console, 'First X')
    service = controller.console.highlighting_service
    assert service.wrong and 'X' in view.display.toPlainText()
    # Enter the image-only unit with a live wrong-input overlay so the
    # skip's own notifying clear must remove it before advancing.
    view.line_pos = 1
    view.cursor_pos = view.persistent_pos = len(view.tobetyped_list[0])
    view._setLine(view.line_pos)
    expected = chapter['plain'].index('Next sentence.')
    assert view.cursor_pos == view.persistent_pos == expected
    assert str(view.current_line).startswith('Next sentence.')
    assert view.progress == pytest.approx(100 * expected / len(chapter['plain']))
    assert not service.wrong and not service.wrong_text
    assert controller.console.text() == ''
    assert view.display.toPlainText() == chapter['plain']
