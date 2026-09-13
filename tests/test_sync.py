import json
import os
from hashlib import md5
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
import time
import zipfile

import pytest

from retype.services.sync import (
    HLC, LearningSync, SyncError, _copy_atomic, apply_learning_settings,
    atomic_write_json, learning_settings_from_config, merge_replicas,
    validate_envelope,
)


BOOK_A = 'a' * 32
BOOK_B = 'b' * 32


def _config():
    return {
        'user_dir': '/never/sync/this/path',
        'library_paths': ['/never/sync/this/either'],
        'window': {'x': 2, 'w': 300},
        'icon_set': 'local-icons',
        'console_font': 'local-font',
        'bookview': {'font': 'local-font'},
        'sdict': {'. ': {'keep': True}},
        'rdict': {'—': ['-']},
        'auto_newline': True,
        'adaptive_chord_lessons': True,
        'adaptive_chord_lesson_limit': 5,
        'steno': {'kdict': {'S': ['A']}},
    }


def _legacy(directory, *, progress=None, chords=None, config=None):
    directory.mkdir(parents=True, exist_ok=True)
    if progress is not None:
        (directory / 'save.json').write_text(json.dumps(progress), encoding='utf-8')
    if chords is not None:
        (directory / 'chord-mastery.json').write_text(json.dumps(chords), encoding='utf-8')
    if config is not None:
        (directory / 'config.json').write_text(json.dumps(config), encoding='utf-8')


def _enable(tmp_path, name, sync_root, *, progress=None, chords=None, config=None):
    legacy = tmp_path / (name + '-legacy')
    _legacy(legacy, progress=progress, chords=chords, config=config)
    sync = LearningSync(tmp_path / (name + '-local'), legacy)
    assert sync.configure(sync_root).state == 'ready'
    return sync, legacy


def _epub(path: Path, data=b'book'):
    content = data.decode('utf-8', errors='replace')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>''')
        archive.writestr('OEBPS/content.opf', '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0"
    unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">test-book</dc:identifier>
    <dc:title>Test Book</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest><item id="content" href="content.xhtml"
    media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="content"/></spine>
</package>''')
        archive.writestr('OEBPS/content.xhtml',
                         '<html xmlns="http://www.w3.org/1999/xhtml"><body>' +
                         content + '</body></html>')


def test_legacy_path_progress_is_imported_using_file_identity(tmp_path):
    source = tmp_path / 'legacy.epub'
    source.write_bytes(b'legacy book')
    progress = {
        'persistent_pos': 8, 'chapter_pos': 1, 'progress': 20,
    }
    sync, _ = _enable(
        tmp_path, 'one', tmp_path / 'folder',
        progress={str(source): progress}, config=_config())

    identity = md5(source.read_bytes()).hexdigest()
    assert sync.sync_now().save[identity]['progress'] == 20


def test_legacy_progress_recovery_survives_sync_state_write_failures(tmp_path):
    sync, legacy = _enable(tmp_path, 'one', tmp_path / 'folder', config=_config())
    sync.sync_now()
    (legacy / 'chord-mastery.json').write_text(
        json.dumps({'version': 2, 'progress': {'word': 1}}), encoding='utf-8')

    with patch.object(sync, '_touch', side_effect=OSError('disk full')), \
            patch.object(sync, '_defer', return_value=False), \
            patch.object(sync, '_save_bootstrap', side_effect=OSError('disk full')):
        sync.record_chords({'word': 1}, {})

    restarted = LearningSync(tmp_path / 'one-local', legacy)
    assert restarted.sync_now().chord_counts == {'word': 1}


def test_copy_atomic_rejects_changed_bytes_before_replacing_destination(tmp_path):
    source = tmp_path / 'source.epub'
    destination = tmp_path / 'destination.epub'
    source.write_bytes(b'new')
    destination.write_bytes(b'old')

    with pytest.raises(OSError, match='digest'):
        _copy_atomic(source, destination, '0' * 64, 3)

    assert destination.read_bytes() == b'old'
    assert not list(tmp_path.glob('.destination.epub.*.tmp'))


def test_managed_book_progress_accepts_sha256_identity(tmp_path):
    sync, _ = _enable(tmp_path, 'one', tmp_path / 'folder', config=_config())
    identity = 'c' * 64
    sync.record_book(identity, {
        'persistent_pos': 8, 'chapter_pos': 1, 'progress': 20})

    assert sync.sync_now().save[identity]['progress'] == 20


def test_initial_legacy_chord_count_is_used_as_callback_baseline(tmp_path):
    sync, _ = _enable(
        tmp_path, 'one', tmp_path / 'folder',
        chords={'version': 2, 'progress': {'word': 3}}, config=_config())
    sync.record_chords({'word': 4}, {})

    assert sync.sync_now().chord_counts == {'word': 4}


def test_migration_keeps_legacy_files_and_materializes_syncable_state(tmp_path):
    original_save = {BOOK_A: {
        'persistent_pos': 8, 'chapter_pos': 1, 'progress': 20,
        'friendly_name': 'Book.epub',
    }}
    original_chords = {
        'version': 2,
        'progress': {'the': 3, 'ignored': 'future'},
        'manual_overrides': {'the': False},
        'future': {'keep': True},
    }
    sync, legacy = _enable(
        tmp_path, 'one', tmp_path / 'folder', progress=original_save,
        chords=original_chords, config=_config())

    result = sync.sync_now()

    assert result.save[BOOK_A]['persistent_pos'] == 8
    assert result.chord_counts == {'the': 3}
    assert result.chord_overrides == {'the': False}
    assert set(result.settings) == {
        'sdict', 'rdict', 'auto_newline', 'adaptive_chord_lessons',
        'adaptive_chord_lesson_limit', 'steno.kdict'}
    # Migration is copy/import only; the legacy source remains available.
    assert json.loads((legacy / 'save.json').read_text()) == original_save
    legacy_chords = json.loads((legacy / 'chord-mastery.json').read_text())
    assert legacy_chords['future'] == {'keep': True}
    assert legacy_chords['progress']['ignored'] == 'future'


def test_offline_replicas_merge_furthest_progress_and_gcounter_components(tmp_path):
    root = tmp_path / 'folder'
    first, _ = _enable(
        tmp_path, 'first', root,
        progress={BOOK_A: {'persistent_pos': 80, 'chapter_pos': 2, 'progress': 60}},
        chords={'version': 2, 'progress': {'word': 4}}, config=_config())
    first_result = first.sync_now()
    assert first_result.chord_counts == {'word': 4}
    # Local callbacks report totals; each later callback is one new G-counter
    # increment, not another copy of every prior local use.
    first.record_chords({'word': 5}, {})
    first.record_chords({'word': 6}, {})
    assert first.sync_now().chord_counts == {'word': 6}

    second, _ = _enable(
        tmp_path, 'second', root,
        progress={BOOK_A: {'persistent_pos': 12, 'chapter_pos': 0, 'progress': 9}},
        chords={'version': 2, 'progress': {'word': 2}}, config=_config())
    second_result = second.sync_now()
    assert second_result.save[BOOK_A]['persistent_pos'] == 80
    assert second_result.chord_counts == {'word': 8}

    # The second installation sees a total of eight and adds two locally. Its
    # own component changes by two; it does not re-add the first replica's six.
    second.record_chords({'word': 10}, {})
    second.sync_now()
    merged = first.sync_now()
    assert merged.chord_counts == {'word': 10}
    assert merged.save[BOOK_A]['persistent_pos'] == 80

    # ``persistent_pos`` is chapter-local: a short offset in a later chapter
    # is farther than a large offset in an earlier chapter.
    first.record_book(BOOK_B, {
        'persistent_pos': 900, 'chapter_pos': 0, 'progress': 10})
    second.record_book(BOOK_B, {
        'persistent_pos': 3, 'chapter_pos': 3, 'progress': 70})
    second.sync_now()
    assert first.sync_now().save[BOOK_B]['chapter_pos'] == 3


def test_typing_callback_queues_without_waiting_for_a_folder_scan(tmp_path):
    sync, _ = _enable(tmp_path, 'one', tmp_path / 'folder', config=_config())
    sync.sync_now()
    locked = Event()
    release = Event()

    def hold_sync_lock():
        with sync._lock:
            locked.set()
            release.wait()

    worker = Thread(target=hold_sync_lock)
    worker.start()
    assert locked.wait(1)
    start = time.monotonic()
    sync.record_chords({'word': 1}, {})
    assert time.monotonic() - start < 0.1
    assert sync.has_deferred_changes
    release.set()
    worker.join(1)
    assert sync.sync_now().chord_counts == {'word': 1}


def test_pending_chord_use_is_not_counted_again_on_reenable(tmp_path):
    root = tmp_path / 'folder'
    sync, legacy = _enable(
        tmp_path, 'one', root,
        chords={'version': 2, 'progress': {'word': 3}}, config=_config())
    sync.sync_now()
    sync.record_chords({'word': 4}, {})
    local_chords = json.loads((legacy / 'chord-mastery.json').read_text())
    local_chords['progress']['word'] = 4
    legacy.joinpath('chord-mastery.json').write_text(
        json.dumps(local_chords), encoding='utf-8')
    sync.disable()
    assert sync.configure(root).state == 'ready'
    assert sync.sync_now().chord_counts == {'word': 4}


def test_reenable_does_not_restamp_unchanged_remote_settings(tmp_path):
    root = tmp_path / 'folder'
    first, first_legacy = _enable(tmp_path, 'first', root, config=_config())
    first.sync_now()

    remote_config = _config()
    remote_config['auto_newline'] = False
    second, _ = _enable(tmp_path, 'second', root, config=remote_config)
    second.sync_now()
    first.sync_now()

    local_config = json.loads((first_legacy / 'config.json').read_text())
    local_config['auto_newline'] = False
    (first_legacy / 'config.json').write_text(
        json.dumps(local_config), encoding='utf-8')
    first.disable()
    assert first.configure(root).state == 'ready'
    assert first.sync_now().settings['auto_newline'] is False


def test_disable_without_delete_queues_only_new_local_chord_uses_on_reenable(tmp_path):
    root = tmp_path / 'folder'
    first, _ = _enable(
        tmp_path, 'first', root,
        chords={'version': 2, 'progress': {'word': 3}}, config=_config())
    first.sync_now()
    second, _ = _enable(
        tmp_path, 'second', root,
        chords={'version': 2, 'progress': {'word': 2}}, config=_config())
    assert second.sync_now().chord_counts == {'word': 5}

    second.disable()
    local_chords = json.loads((tmp_path / 'second-legacy' / 'chord-mastery.json').read_text())
    local_chords['progress']['word'] = 7
    (tmp_path / 'second-legacy' / 'chord-mastery.json').write_text(
        json.dumps(local_chords), encoding='utf-8')
    assert second.configure(root).state == 'ready'
    second.sync_now()
    assert first.sync_now().chord_counts == {'word': 7}
    # Folder data was not deleted when this installation went local-only.
    assert (root / 'replicas' / (first.replica_id + '.json')).exists()


def test_merge_is_duplicate_reorder_independent_and_lww_ties_are_deterministic(tmp_path):
    root = tmp_path / 'folder'
    first, _ = _enable(tmp_path, 'first', root, config=_config())
    first.sync_now()
    second, _ = _enable(tmp_path, 'second', root, config=_config())
    second.sync_now()
    replicas = []
    for path in (root / 'replicas').glob('*.json'):
        replicas.append(validate_envelope(json.loads(path.read_text()), first.collection_id))

    normal = merge_replicas(replicas)
    reverse_duplicate = merge_replicas(list(reversed(replicas)) + replicas)
    assert normal.chord_counts == reverse_duplicate.chord_counts
    assert normal.settings == reverse_duplicate.settings
    assert normal.save == reverse_duplicate.save
    # Repeating a visible file never repeats its one replica-owned counter.
    assert reverse_duplicate.chord_counts == normal.chord_counts

    # HLC/replcia ID is a deterministic register order rather than filesystem mtime.
    low, high = sorted([str(item['replica_id']) for item in replicas])
    payload = {'books': {}, 'chords': {'counts': {}, 'overrides': {}},
               'settings': {}, 'managed_books': {}}
    left = {
        'schema': 'retype-learning-sync-replica', 'version': 1,
        'collection_id': first.collection_id, 'replica_id': low, 'sequence': 1,
        'hlc': HLC(10, 0).to_data(), 'predecessor_digest': None,
        'payload': dict(payload, settings={'auto_newline': {
            'value': False, 'hlc': HLC(10, 0).to_data(), 'replica_id': low}}),
    }
    right = {
        'schema': 'retype-learning-sync-replica', 'version': 1,
        'collection_id': first.collection_id, 'replica_id': high, 'sequence': 1,
        'hlc': HLC(10, 0).to_data(), 'predecessor_digest': None,
        'payload': dict(payload, settings={'auto_newline': {
            'value': True, 'hlc': HLC(10, 0).to_data(), 'replica_id': high}}),
    }
    from retype.services.sync import _digest
    left['payload_digest'] = _digest(left['payload'])
    right['payload_digest'] = _digest(right['payload'])
    assert merge_replicas([validate_envelope(left), validate_envelope(right)]).settings == {
        'auto_newline': True}

    # A G-counter contribution is still one component when an ordinary folder
    # temporarily exposes the same complete file twice.
    counted = dict(left)
    counted_payload = dict(payload, chords={'counts': {'word': 7}, 'overrides': {}})
    counted['payload'] = counted_payload
    counted['payload_digest'] = _digest(counted_payload)
    assert merge_replicas([validate_envelope(counted), validate_envelope(counted)]).chord_counts == {
        'word': 7}


def test_restart_retains_last_publication_when_provider_is_stale(tmp_path):
    root = tmp_path / 'folder'
    local = tmp_path / 'one-local'
    legacy = tmp_path / 'one-legacy'
    _legacy(legacy, config=_config())
    first = LearningSync(local, legacy)
    assert first.configure(root).state == 'ready'
    first.record_chords({'word': 2}, {})
    first.sync_now()
    stale = json.loads((root / 'replicas' /
                        (first.replica_id + '.json')).read_text())
    first.record_chords({'word': 3}, {})
    assert first.sync_now().chord_counts == {'word': 3}
    (root / 'replicas' / (first.replica_id + '.json')).write_text(
        json.dumps(stale), encoding='utf-8')

    restarted = LearningSync(local, legacy)
    result = restarted.sync_now()

    assert result.chord_counts == {'word': 3}
    assert any('provider replica is stale' in item
               for item in result.status.diagnostics)


def test_deferred_mutations_survive_restart(tmp_path):
    root = tmp_path / 'folder'
    local = tmp_path / 'one-local'
    legacy = tmp_path / 'one-legacy'
    _legacy(legacy, config=_config())
    first = LearningSync(local, legacy)
    assert first.configure(root).state == 'ready'
    first.sync_now()
    locked = Event()
    release = Event()

    def hold_sync_lock():
        with first._lock:
            locked.set()
            release.wait()

    worker = Thread(target=hold_sync_lock)
    worker.start()
    assert locked.wait(1)
    first.record_chords({'word': 1}, {})
    release.set()
    worker.join(1)
    assert (local / 'deferred-sync-mutations.json').exists()

    restarted = LearningSync(local, legacy)
    assert restarted.sync_now().chord_counts == {'word': 1}
    assert not (local / 'deferred-sync-mutations.json').exists()


def test_deferred_chord_callback_during_final_materialization_is_retained(tmp_path):
    root = tmp_path / 'folder'
    sender, _ = _enable(
        tmp_path, 'sender', root,
        chords={'version': 2, 'progress': {'word': 5}}, config=_config())
    sender.sync_now()
    receiver, _ = _enable(tmp_path, 'receiver', root, config=_config())
    original_materialize = receiver._materialize_legacy_state
    triggered = False

    def materialize(result):
        nonlocal triggered
        if not triggered:
            triggered = True
            callback = Thread(target=receiver.record_chords,
                              args=({'word': 1}, {}))
            callback.start()
            callback.join(1)
            assert not callback.is_alive()
        original_materialize(result)

    receiver._materialize_legacy_state = materialize
    result = receiver.sync_now()

    assert result.chord_counts == {'word': 6}


def test_stale_deferred_marker_cleanup_failure_is_diagnostic(tmp_path):
    sync, _ = _enable(tmp_path, 'one', tmp_path / 'folder', config=_config())
    sync.sync_now()
    sync._payload['_applied_deferred'] = ['stale-event']

    with patch.object(sync, '_touch', side_effect=OSError('disk full')):
        sync._prune_deferred_markers()

    assert any('Stale deferred markers' in item
               for item in sync.status.diagnostics)


def test_post_scan_deferred_settings_are_included_in_result(tmp_path):
    root = tmp_path / 'folder'
    sync, _ = _enable(tmp_path, 'one', root, config=_config())
    sync.sync_now()
    original_scan = sync._scan_replicas
    triggered = False
    changed = _config()
    changed['auto_newline'] = False

    def scan(directory, collection):
        nonlocal triggered
        result = original_scan(directory, collection)
        if not triggered:
            triggered = True
            callback = Thread(target=sync.record_settings,
                              args=(changed, _config()))
            callback.start()
            callback.join(1)
        return result

    sync._scan_replicas = scan
    result = sync.sync_now()

    assert result.settings['auto_newline'] is False


def test_restart_restores_published_replica_without_fork_recovery(tmp_path):
    root = tmp_path / 'folder'
    local = tmp_path / 'one-local'
    legacy = tmp_path / 'one-legacy'
    _legacy(legacy, config=_config())
    first = LearningSync(local, legacy)
    assert first.configure(root).state == 'ready'
    first.record_chords({'word': 2}, {})
    first.sync_now()
    replica_id = first.replica_id

    restarted = LearningSync(local, legacy)
    result = restarted.sync_now()

    assert restarted.replica_id == replica_id
    assert result.chord_counts == {'word': 2}
    restarted.record_chords({'word': 3}, {})
    assert restarted.sync_now().chord_counts == {'word': 3}
    assert not any('duplicated' in item for item in result.status.diagnostics)


def test_remote_managed_books_are_not_materialized_without_consent(tmp_path):
    root = tmp_path / 'folder'
    source = tmp_path / 'private.epub'
    _epub(source)
    sender, _ = _enable(tmp_path, 'sender', root, config=_config())
    sender.set_managed_library_consent(True)
    metadata = sender.import_book(source)
    sender.sync_now()

    receiver, _ = _enable(tmp_path, 'receiver', root, config=_config())
    result = receiver.sync_now()

    assert not (receiver.managed_library_dir /
                (metadata['digest'] + '.epub')).exists()
    assert any('consent' in item for item in result.status.diagnostics)


def test_invalid_replica_is_recovered_and_provider_absence_keeps_local_operation(tmp_path):
    sync, _ = _enable(tmp_path, 'one', tmp_path / 'folder', config=_config())
    sync.sync_now()
    invalid = tmp_path / 'folder' / 'replicas' / 'foreign.json'
    invalid.write_text('{not json', encoding='utf-8')

    result = sync.sync_now()
    assert result.status.state == 'synced'
    assert any('foreign.json' in item for item in result.status.diagnostics)
    assert list((tmp_path / 'one-local' / 'recovery' / 'sync').glob('*.rejected'))

    os.rename(tmp_path / 'folder', tmp_path / 'unavailable-folder')
    unavailable = sync.sync_now()
    assert unavailable.status.state == 'waiting'
    # The pending local component is retained for a later provider retry.
    sync.record_chords({'word': 1}, {})
    assert (tmp_path / 'one-local' / 'pending-sync-replica.json').exists()


def test_atomic_write_failure_leaves_previous_complete_file_and_no_temp(tmp_path):
    target = tmp_path / 'state.json'
    target.write_text('{"old":true}', encoding='utf-8')
    with patch('retype.services.sync.os.replace', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            atomic_write_json(target, {'new': True}, tmp_path / 'backups')
    assert target.read_text(encoding='utf-8') == '{"old":true}'
    assert not list(tmp_path.glob('.state.json.*.tmp'))
    assert list((tmp_path / 'backups').glob('state.json.*.bak'))


def test_settings_allowlist_never_roams_paths_or_visual_preferences():
    config = _config()
    values = learning_settings_from_config(config)
    assert 'user_dir' not in values
    assert 'library_paths' not in values
    assert 'window' not in values
    assert 'icon_set' not in values
    assert 'bookview' not in values

    target = _config()
    target['user_dir'] = '/local/other'
    target['window']['x'] = 999
    updated = apply_learning_settings(target, {
        'auto_newline': False,
        'adaptive_chord_lesson_limit': 7,
        'window': {'x': 0},
        'user_dir': '/bad',
    })
    assert updated['auto_newline'] is False
    assert updated['adaptive_chord_lesson_limit'] == 7
    assert updated['user_dir'] == '/local/other'
    assert updated['window']['x'] == 999


def test_managed_books_need_consent_are_content_addressed_and_never_auto_imported(tmp_path):
    root = tmp_path / 'folder'
    sync, _ = _enable(tmp_path, 'one', root, config=_config())
    sync.sync_now()
    source = tmp_path / 'private.epub'
    _epub(source)

    with pytest.raises(SyncError, match='consent'):
        sync.import_book(source)
    assert not (root / 'books').exists()

    sync.set_managed_library_consent(True)
    metadata = sync.import_book(source)
    digest = metadata['digest']
    assert (root / 'books' / 'sha256' / (digest + '.epub')).exists()
    assert (root / 'books' / 'sha256' / (digest + '.json')).exists()
    assert (tmp_path / 'one-local' / 'managed-books' / (digest + '.epub')).exists()

    changed = tmp_path / 'private-copy.epub'
    _epub(changed, b'changed edition')
    # Explicitly imported changed bytes are a distinct managed book, never a
    # silent progress mapping to the old edition.
    second = sync.import_book(changed, title='Private')
    assert second['digest'] != digest

    corrupt = tmp_path / 'corrupt.epub'
    corrupt.write_bytes(b'not an epub')
    with pytest.raises(SyncError, match='corrupt'):
        sync.import_book(corrupt)


def test_merged_managed_library_limit_preserves_oversized_metadata(tmp_path):
    root = tmp_path / 'folder'
    first, _ = _enable(tmp_path, 'first', root, config=_config())
    second, _ = _enable(tmp_path, 'second', root, config=_config())
    first.set_managed_library_consent(True)
    second.set_managed_library_consent(True)
    first_book = tmp_path / 'first.epub'
    second_book = tmp_path / 'second.epub'
    _epub(first_book, b'first')
    _epub(second_book, b'second')
    first_size = first_book.stat().st_size
    second_size = second_book.stat().st_size

    with patch('retype.services.sync.MAX_MANAGED_LIBRARY_BYTES',
               first_size + second_size - 1):
        first_metadata = first.import_book(first_book)
        second_metadata = second.import_book(second_book)
        assert first.sync_now().status.state == 'synced'
        result = second.sync_now()
        third_book = tmp_path / 'third.epub'
        _epub(third_book, b'third')
        with pytest.raises(SyncError, match='managed library limit'):
            second.import_book(third_book)

    assert result.status.state == 'synced'
    assert first_metadata['digest'] in result.managed_books
    assert second_metadata['digest'] in result.managed_books
    assert any('exceeds the configured 1 GiB limit' in item
               for item in result.status.diagnostics)
    assert (second.managed_library_dir /
            (second_metadata['digest'] + '.epub')).exists()
    assert (root / 'books' / 'sha256' /
            (second_metadata['digest'] + '.epub')).exists()


def test_managed_book_hash_mismatch_is_diagnosed_without_indexing_bad_bytes(tmp_path):
    root = tmp_path / 'folder'
    one, _ = _enable(tmp_path, 'one', root, config=_config())
    one.set_managed_library_consent(True)
    book = tmp_path / 'book.epub'
    _epub(book)
    metadata = one.import_book(book)
    one.sync_now()

    two, _ = _enable(tmp_path, 'two', root, config=_config())
    two.set_managed_library_consent(True)
    (root / 'books' / 'sha256' / (metadata['digest'] + '.epub')).write_bytes(b'bad')
    result = two.sync_now()
    assert any('hash or size mismatch' in item for item in result.status.diagnostics)
    assert not (tmp_path / 'two-local' / 'managed-books' /
                (metadata['digest'] + '.epub')).exists()
