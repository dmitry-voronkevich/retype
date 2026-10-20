import zipfile
from hashlib import sha256
from unittest.mock import patch

from retype.controllers.library import LibraryController


def test_nested_managed_index_does_not_abort_library_startup(tmp_path):
    managed = tmp_path / 'managed-books'
    managed.mkdir()
    (managed / '.retype-managed-index.json').write_text('{}', encoding='utf-8')

    with patch('retype.controllers.library.json.load',
               side_effect=RecursionError('too deeply nested')):
        library = LibraryController('', [], str(managed))

    assert library._library_items == {}


def test_consent_revocation_hides_managed_books_after_restart(tmp_path):
    managed = tmp_path / 'managed-books'
    managed.mkdir()
    source = managed / 'source.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(
            'mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    digest = sha256(source.read_bytes()).hexdigest()
    managed_book = managed / (digest + '.epub')
    source.rename(managed_book)

    opted_in = LibraryController('', [], str(managed),
                                 managed_library_consent=True)
    assert [item.checksum for item in opted_in._library_items.values()] == [digest]

    restarted = LibraryController('', [], str(managed),
                                  managed_library_consent=False)
    assert restarted._library_items == {}
    assert managed_book.exists()
