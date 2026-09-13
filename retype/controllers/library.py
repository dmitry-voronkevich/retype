import os
import json
import logging
import re
import traceback
from hashlib import sha256
from copy import deepcopy
from lxml.html import fromstring, builder, tostring, xhtml_to_html
from lxml.etree import _Element
from ebooklib import epub
from qt import QTextBrowser, QMessageBox

from typing import TYPE_CHECKING

from retype.extras.space import isspaceorempty
from retype.extras.hashing import generate_file_md5
from retype.services.sync import (MAX_MANAGED_BOOK_BYTES, _is_epub_file,
                                  _is_loadable_epub_file, _validate_save)

logger = logging.getLogger(__name__)

_MANAGED_BOOK_FILENAME = re.compile(r'^[0-9a-f]{64}\.epub$')


def _is_within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except ValueError:
        return False


def _file_sha256(path):
    digest = sha256()
    with open(path, 'rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_managed_book(path, checksum):
    try:
        stat = os.stat(path)
        return stat.st_size <= MAX_MANAGED_BOOK_BYTES and \
            _file_sha256(path) == checksum and _is_loadable_epub_file(path)
    except OSError:
        return False


def _save_position_key(data):
    # type: (object) -> tuple[float, int, int] | None
    data = _validate_save(data)
    if data is None:
        return None
    return (data['progress'], data['chapter_pos'], data['persistent_pos'])


class LibraryController(object):
    def __init__(self, user_dir, library_paths, managed_library_path=None,
                 on_save=None, managed_library_consent=True):
        # type: (LibraryController, str, list[str], str | None, object | None, bool) -> None
        self.user_dir = user_dir
        self.library_paths = list(library_paths)
        self.managed_library_path = managed_library_path
        self.managed_library_consent = bool(managed_library_consent)
        self.on_save = on_save
        self._library_items = self.indexLibrary(self.library_paths)
        self.indexManagedLibrary(self._library_items)
        self.books = None  # type: dict[int, BookWrapper] | None
        self.save_file_contents = None  # type: Save | None

    @property
    def user_dir(self):
        # type: (LibraryController) -> str
        return self._user_dir

    @user_dir.setter
    def user_dir(self, value):
        # type: (LibraryController, str) -> None
        self._user_dir = value
        self.save_abs_path = os.path.join(value, 'save.json')

    def checksum(self, path):
        # type: (LibraryController, str) -> str | None
        checksum = None
        try:
            checksum = generate_file_md5(path)
        except OSError as e:
            s = (f'Unable to read epub {self.idn}:\n{self.path}.\n\n'
                 'This is not fatal, but the book will not be loaded.')
            logger.error(f"{s}\n{e}", exc_info=True)
            msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
            msg.setDetailedText(f'Path: {path}\n\n'
                                f'{traceback.format_exc()}')
            msg.exec()
        return checksum

    def indexLibrary(self, library_paths):
        # type: (LibraryController, list[str]) -> dict[int, LibraryItem]
        book_checksum_list = []
        library_items = {}
        idn = 0
        managed_root = os.path.realpath(self.managed_library_path) \
            if self.managed_library_path else None
        for library_path in library_paths:
            for root, dirs, files in os.walk(library_path):
                if managed_root is not None:
                    if _is_within(root, managed_root):
                        continue
                    dirs[:] = [directory for directory in dirs
                               if not _is_within(
                                   os.path.join(root, directory), managed_root)]
                for f in files:
                    if f.lower().endswith(".epub"):
                        path = os.path.join(root, f)
                        if managed_root is not None and _is_within(
                                path, managed_root):
                            continue
                        checksum = self.checksum(path)
                        if not checksum or checksum in book_checksum_list:
                            continue
                        book_checksum_list.append(checksum)
                        library_items[idn] = LibraryItem(idn, path, checksum)
                        idn += 1
        return library_items

    def _managed_index_path(self):
        return os.path.join(self.managed_library_path,
                            '.retype-managed-index.json')

    def _load_managed_index(self):
        try:
            with open(self._managed_index_path(), 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            checksum: record for checksum, record in data.items()
            if isinstance(checksum, str) and
            _MANAGED_BOOK_FILENAME.fullmatch(checksum + '.epub') and
            isinstance(record, dict) and isinstance(record.get('size'), int) and
            isinstance(record.get('mtime_ns'), int) and
            isinstance(record.get('ctime_ns'), int) and
            isinstance(record.get('inode'), int)
        }

    def _save_managed_index(self, index):
        temporary = self._managed_index_path() + '.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as file:
                json.dump(index, file, sort_keys=True)
            os.replace(temporary, self._managed_index_path())
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def indexManagedLibrary(self, library_items):
        # type: (LibraryController, dict[int, LibraryItem]) -> None
        if not self.managed_library_path or not self.managed_library_consent:
            return
        try:
            index = self._load_managed_index()
            index_changed = False
            with os.scandir(self.managed_library_path) as entries:
                managed_entries = sorted(entries, key=lambda entry: entry.name)
                existing = {item.checksum for item in library_items.values()}
                next_id = max(library_items, default=-1) + 1
                for entry in managed_entries:
                    if not entry.is_file() or not _MANAGED_BOOK_FILENAME.fullmatch(
                            entry.name):
                        continue
                    checksum = entry.name[:-len('.epub')]
                    try:
                        stat = entry.stat()
                        if stat.st_size > MAX_MANAGED_BOOK_BYTES:
                            logger.warning('Ignoring oversized managed EPUB: %s',
                                           entry.path)
                            continue
                        if not _is_epub_file(entry.path):
                            logger.warning('Ignoring invalid managed EPUB: %s',
                                           entry.path)
                            continue
                        record = index.get(checksum)
                        unchanged = isinstance(record, dict) and \
                            record.get('size') == stat.st_size and \
                            record.get('mtime_ns') == stat.st_mtime_ns and \
                            record.get('ctime_ns') == stat.st_ctime_ns and \
                            record.get('inode') == stat.st_ino
                        if not unchanged:
                            index[checksum] = {
                                'size': stat.st_size,
                                'mtime_ns': stat.st_mtime_ns,
                                'ctime_ns': stat.st_ctime_ns,
                                'inode': stat.st_ino,
                            }
                            index_changed = True
                    except OSError as error:
                        logger.warning('Unable to verify managed EPUB %s: %s',
                                       entry.path, error)
                        continue
                    if checksum in existing:
                        continue
                    library_items[next_id] = LibraryItem(
                        next_id, entry.path, checksum)
                    existing.add(checksum)
                    next_id += 1
            if index_changed:
                self._save_managed_index(index)
        except OSError:
            return

    def setManagedLibraryConsent(self, consent):
        # type: (LibraryController, bool) -> None
        consent = bool(consent)
        if consent == self.managed_library_consent:
            return
        self.managed_library_consent = consent
        if consent:
            self.indexManagedLibrary(self._library_items)
            return
        managed_root = os.path.realpath(self.managed_library_path) \
            if self.managed_library_path else None
        managed_ids = [idn for idn, item in self._library_items.items()
                       if managed_root is not None and
                       _is_within(item.path, managed_root)]
        for idn in managed_ids:
            self._library_items.pop(idn, None)
            if self.books is not None:
                self.books.pop(idn, None)

    def instantiateBooks(self, include_managed=True):
        # type: (LibraryController, bool) -> None
        self.books = {}
        if not include_managed and self._library_items:
            self.loadSaveFile()
        for idn, item in self._library_items.items():
            if not include_managed and _MANAGED_BOOK_FILENAME.fullmatch(
                    item.checksum + '.epub'):
                continue
            save_data = _validate_save(self.load(item))
            book = BookWrapper(item, save_data)
            self.books[idn] = book

    def managedBookLoadData(self):
        # type: (LibraryController) -> list[tuple[LibraryItem, SaveData | None]]
        if not self.managed_library_consent:
            return []
        if self.save_file_contents is None:
            self.loadSaveFile()
        save = self.save_file_contents or {}
        return [(item, _validate_save(deepcopy(save.get(item.checksum))))
                for item in self._library_items.values()
                if _MANAGED_BOOK_FILENAME.fullmatch(item.checksum + '.epub')]

    def installManagedBooks(self, books):
        # type: (LibraryController, dict[int, BookWrapper]) -> list[BookWrapper]
        if self.books is None:
            self.books = {}
        if self.save_file_contents is None:
            self.loadSaveFile()
        existing = {book.checksum for book in self.books.values()}
        installed = []
        for idn, book in books.items():
            if not book.valid:
                self._library_items.pop(idn, None)
            elif book.checksum in existing:
                self._library_items.pop(idn, None)
            else:
                current = self.save_file_contents.get(book.checksum) \
                    if self.save_file_contents else None
                current = _validate_save(current)
                if current is not None:
                    book.save_data = current
                    book.updateProgress(current['progress'])
                self.books[idn] = book
                existing.add(book.checksum)
                installed.append(book)
        return installed

    def addManagedBooks(self, managed_books, validated_checksums=None,
                        loaded_books=None):
        # type: (LibraryController, dict[str, dict[str, object]], set[str] | None, dict[str, object] | None) -> list[BookWrapper]
        if self.books is None or not self.managed_library_path or \
                not self.managed_library_consent:
            return []
        existing = {book.checksum for book in self.books.values()}
        next_id = max(self._library_items, default=-1) + 1
        added = []
        for checksum, metadata in managed_books.items():
            if checksum in existing or not _MANAGED_BOOK_FILENAME.fullmatch(
                    checksum + '.epub'):
                continue
            if not isinstance(metadata, dict) or metadata.get('digest') != checksum:
                continue
            path = os.path.join(self.managed_library_path, checksum + '.epub')
            try:
                stat = os.stat(path)
                if stat.st_size > MAX_MANAGED_BOOK_BYTES or \
                        stat.st_size != metadata.get('size'):
                    logger.warning('Ignoring invalid managed EPUB: %s', path)
                    continue
                if checksum not in (validated_checksums or ()) and \
                        (_file_sha256(path) != checksum or
                         not _is_loadable_epub_file(path)):
                    logger.warning('Ignoring invalid managed EPUB: %s', path)
                    continue
            except OSError as error:
                logger.warning('Unable to verify managed EPUB %s: %s', path, error)
                continue
            item = LibraryItem(next_id, path, checksum)
            loaded_book = (loaded_books or {}).get(checksum)
            save_data = _validate_save(self.load(item))
            book = BookWrapper(item, save_data, loaded_book,
                               report_errors=False)
            if not book.valid:
                logger.warning('Ignoring invalid managed EPUB: %s', path)
                continue
            self._library_items[next_id] = item
            self.books[next_id] = book
            added.append(book)
            existing.add(checksum)
            next_id += 1
        return added

    def setBook(self, book_id, book_view, switchView):
        # type: (LibraryController, int, BookView, pyqtBoundSignal) -> None
        if book_view.book:
            book_view.maybeSave()

        if self.books and book_id in self.books:
            book = self.books[book_id]
            logger.info("Loading book {}: {}".format(book_id, book.title))
        else:
            logging.error("book_id {} cannot be found".format(book_id))
            logging.debug("books: {}".format(self.books))
            return

        save_data = book.save_data
        logger.info("Save data: {}".format(save_data))
        book_view.setBook(book, save_data)
        switchView.emit(2)
        book_view.display.centreAroundCursor()

    def applyMergedSave(self, merged_save):
        # type: (LibraryController, Save) -> set[str]
        if self.save_file_contents is None:
            self.loadSaveFile()
        assert self.save_file_contents is not None
        changed = set()
        for key, data in merged_save.items():
            valid_data = _validate_save(data)
            if valid_data is None:
                continue
            current = self.save_file_contents.get(key)
            current_key = _save_position_key(current)
            if current_key is None or current_key < _save_position_key(valid_data):
                self.save_file_contents[key] = valid_data
                changed.add(key)
        if self.books is None:
            return changed
        for book in self.books.values():
            data = self.save_file_contents.get(book.checksum)
            if isinstance(data, dict):
                book.save_data = data
                if isinstance(data.get('progress'), (int, float)):
                    book.updateProgress(data['progress'])
        return changed

    def save(self, book, data):
        # type: (LibraryController, BookWrapper, SaveData) -> bool
        book.save_data = data

        self.addFriendlyName(data, book.path)
        key = book.checksum
        save = self.save_file_contents
        if (save):
            save[key] = data
        else:
            save = self.save_file_contents = {key: data}

        try:
            with open(self.save_abs_path, 'w', encoding='utf-8') as f:
                json.dump(save, f, indent=2)
        except (OSError, ValueError, TypeError) as e:
            s = 'Unable to save progress to disk.'
            if e is FileNotFoundError:
                s += f' Unable to find user_dir {self._user_dir}.'
            logger.error(f"{s}\n{e}", exc_info=True)
            msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
            msg.setDetailedText(f'Path: {self.save_abs_path}\n\n'
                                f'{traceback.format_exc()}')
            msg.exec()
            return False
        if callable(self.on_save):
            self.on_save(key, dict(data))
        return True

    def migrateV1Save(self, save):
        # type: (LibraryController, Save) -> Save
        book_checksum_list = []
        new_save = {}
        for key in save:
            checksum = None
            if key.lower().endswith(".epub"):
                if (not os.path.exists(key)):
                    logger.warning("Save file contains v1 format save data "
                                   f"for a file that cannot be found: {key}")
                    # Not a checksum; can’t generate it as we cannot find the
                    #  file, but setting it anyway in order that we keep the
                    #  save entry rather than delete it from the save. This
                    #  save data entry cannot be used by retype, but we should
                    #  not delete it as that would be unnecessary data loss and
                    #  user could correct the issue by fixing the path,
                    #  replacing it with a checksum, or moving the file back.
                    checksum = key
                else:
                    checksum = self.checksum(key)
                    if not checksum:  # Other OSError happened
                        checksum = key
                    self.addFriendlyName(save[key], key)
            else:  # assume it’s a checksum
                checksum = key
            if checksum in book_checksum_list:
                logger.warning("Save file contains several entries for the "
                               f"same book (checksum {checksum}). The lowest "
                               "one in the file will be used")
            else:
                book_checksum_list.append(checksum)
            new_save[checksum] = save[key]
        return new_save

    def addFriendlyName(self, data, path):
        # type: (LibraryController, SaveData, str) -> None
        data['friendly_name'] = os.path.basename(path)

    def loadSaveFile(self):
        # type: (LibraryController) -> Save
        if os.path.exists(self.save_abs_path):
            logger.info(f'Read save: {self.save_abs_path}')
            try:
                with open(self.save_abs_path, 'r') as f:
                    save = json.load(f)  # type: Save
            except (OSError, ValueError, TypeError) as e:
                s = 'Unable to read save file.'
                logger.error(f"{s}\n{e}", exc_info=True)
                msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
                msg.setDetailedText(f'Path: {self.save_abs_path}\n\n'
                                    f'{traceback.format_exc()}')
                msg.exec()
                # Keep the unreadable legacy copy for recovery; callers can
                # continue with an empty in-memory library state.
                save = {}
        else:
            logger.debug(
                f'Save path {self.save_abs_path} not found.\n'
                'This is normal if the save file has not been created yet.')
            save = {}

        save = self.migrateV1Save(save)
        self.save_file_contents = save
        return save

    def load(self, item):
        # type: (LibraryController, LibraryItem) -> SaveData | None
        save = None
        if self.save_file_contents is not None:
            save = self.save_file_contents
        else:
            save = self.loadSaveFile()

        key = item.checksum

        if save and key in save:
            return save[key]

        return None


class LibraryItem:
    def __init__(self, idn, path, checksum):
        # type: (LibraryItem, int, str, str) -> None
        self.idn = idn
        self.path = path
        self.checksum = checksum


class BookWrapper(object):
    def __init__(self, library_item, save_data=None, loaded_book=None,
                 report_errors=True):
        # type: (BookWrapper, LibraryItem, SaveData | None, object | None, bool) -> None
        self.valid = False
        self._library_item = library_item
        self.path = library_item.path
        self.idn = library_item.idn
        self.checksum = library_item.checksum
        self._book = loaded_book if loaded_book is not None else self._readEpub(
            report_errors)
        if loaded_book is not None:
            self.valid = True
        self.title = self._book.title
        self._chapters = []  # type: list[Chapter]
        self._images = []  # type: list[epub.EpubImage]
        self._author = ''
        self._cover = None  # type: epub.EpubCover | epub.EpubImage | None
        self.documents = {}  # type: dict[str, epub.EpubHtml]
        self._unparsed_chapters = []  # type: list[epub.EpubHtml]
        self.save_data = save_data
        self.dirty = False
        self.progress = save_data['progress'] if save_data else 0.0
        self.progress_subscribers = []  # type: list[Callable[[float], None]]

    def _readEpub(self, report_errors=True):
        # type: (BookWrapper, bool) -> epub.EpubBook
        ret = None
        try:
            ret = epub.read_epub(self.path, options={'ignore_ncx': True})
            self.valid = True
        except Exception as e:
            s = (f'Unable to read epub {self.idn}:\n{self.path}.\n\n'
                 'This is not fatal, but the book will not be loaded.')
            logger.error(f"{s}\n{e}", exc_info=True)
            if report_errors:
                msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
                msg.setDetailedText(f'Path: {self.path}\n\n'
                                    f'{traceback.format_exc()}')
                msg.exec()
        return ret or epub.EpubBook()

    def _parseChaptersContent(self, chapters):
        # type: (BookWrapper, list[epub.EpubHtml]) -> list[Chapter]
        parsed_chapters = []
        self.chapter_lookup = {}  # type: dict[str, int]
        for i, chapter in enumerate(chapters):
            parsed_chapters.append(self.__parseChapterContent(chapter))
            self.chapter_lookup[chapter.file_name.split('/')[-1]] = i

        return parsed_chapters

    def __parseChapterContent(self, chapter):
        # type: (BookWrapper, epub.EpubHtml) -> Chapter
        raw = chapter.content
        # FIXME: This ugly workaround is the only way I found to make lxml
        #  use the correct encoding when an lxml declaration is absent from
        #  the document. Also note this causes lxml to get rid of html and
        #  body tags for some reason, which may be a problem in future.
        declaration = """<?xml version="1.0" encoding="utf-8"?>"""
        tree = fromstring(bytes(declaration, 'utf-8') + raw)

        # Replace xml svg elements with valid html
        svg_elements = tree.xpath('//svg')
        if isinstance(svg_elements, list):
            for svg in svg_elements:
                if not isinstance(svg, _Element):
                    continue
                imgs = svg.xpath('//image')
                if not isinstance(imgs, list):
                    continue
                imaged = imgs[0]
                if not isinstance(imaged, _Element):
                    continue
                attrs = {item[0]: item[1] for item in imaged.items()
                         }  # type: dict[str, str]
                try:
                    href = attrs['xlink:href']
                    del attrs['xlink:href']
                    attrs['src'] = href
                except AttributeError:
                    pass
                proper_img = builder.IMG(**attrs)
                parent = svg.getparent()
                if parent is not None:
                    parent.replace(svg, proper_img)

        # Ensure figure elements appear in their own line
        figure_elements = tree.xpath('//figure')
        if isinstance(figure_elements, list):
            for figure in figure_elements:
                if isinstance(figure, _Element):
                    figure.addprevious(figure.makeelement('div'))

        xhtml_to_html(tree)
        html = tostring(tree, method='xml', encoding='unicode')

        # Get rid of invisible garbage characters
        html = html.replace('\ufeff', '')

        links = tree.xpath('//a/@href')
        image_links = tree.xpath('//img/@src')

        images = []  # type: list[ImageData]
        if isinstance(image_links, list):
            for image_link in image_links:
                for image in self._images:
                    if not isinstance(image_link, str):
                        continue
                    if image_link.lstrip('./') in image.file_name:
                        images.append({'item': image,
                                       'link': image_link,
                                       'raw': image.content})

        # We to store the length of the plain text of all chapters for
        #  progress-calculation purposes
        dummy_display = QTextBrowser()
        dummy_display.setHtml(html)
        plain = dummy_display.toPlainText()

        return {'html': html, 'plain': plain, 'len': len(plain),
                'links': links, 'images': images}

    @property
    def chapters(self):
        # type: (BookWrapper) -> list[Chapter]
        if not self._unparsed_chapters:
            self._getItems(self._book)
        if not self._chapters:
            self._chapters = self._parseChaptersContent(
                self._unparsed_chapters)
        return self._chapters or []

    @property
    def images(self):
        # type: (BookWrapper) -> list[epub.EpubImage]
        if not self._images:
            self._getItems(self._book)
        return self._images

    @property
    def cover(self):
        # type: (BookWrapper) -> epub.EpubCover | epub.EpubImage | None
        if not self._cover:
            self._getItems(self._book)
        return self._cover

    def _getItems(self, book):
        # type: (BookWrapper, epub.EpubBook) -> None
        logger.debug("_getItems called for '{}'".format(book.title))

        # Reset lists
        self._images = []
        self._unparsed_chapters = []

        # Get items
        for item in book.get_items():
            if isinstance(item, epub.EpubCover):
                self._cover = item
            elif isinstance(item, epub.EpubImage):
                if 'cover' in item.id and not self._cover:
                    self._cover = item
                self._images.append(item)
            elif isinstance(item, epub.EpubHtml):
                self.documents[item.id] = item

        # Get chapters
        for spine_item in book.spine:
            uid = spine_item[0]
            if uid in self.documents.keys():
                self._unparsed_chapters.append(self.documents[uid])

        # Workaround to catch some edge cases where the cover is not marked but
        #  is present on the first page
        if not self._cover and len(self._unparsed_chapters):
            first_page = self.__parseChapterContent(self._unparsed_chapters[0])
            if len(first_page['images']) == 1 and \
               isspaceorempty(first_page['plain'], True):
                self._cover = first_page['images'][0]['item']

    @property
    def author(self):
        # type: (BookWrapper) -> str
        book = self._book
        if not self._author:
            for namespace in book.metadata.keys():
                data = book.metadata[namespace]
                for key, value in data.items():
                    if key == 'creator':
                        self._author = value[0][0]
        return self._author

    def updateProgress(self, progress):
        # type: (BookWrapper, float) -> None
        self.progress = progress

        for subscriber in self.progress_subscribers:
            subscriber(progress)


if TYPE_CHECKING:
    from typing import Callable  # noqa: F401
    from qt import pyqtBoundSignal  # noqa: F401
    from retype.ui import BookView, Cover  # noqa: F401
    from retype.extras.metatypes import (  # noqa: F401
        SaveData, Save, ImageData, Chapter)
