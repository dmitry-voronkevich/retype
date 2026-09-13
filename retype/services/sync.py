"""Provider-neutral learning-state synchronization for ordinary folders.

The sync protocol deliberately knows nothing about iCloud, accounts, or a
network.  A provider merely makes a user-selected directory appear locally.
Every installation owns one replica file, so an eventually-consistent provider
never has to merge a shared ``save.json`` file.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import md5, sha256
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import zipfile
from math import isfinite
from typing import Mapping
from uuid import UUID, uuid4

from ebooklib import epub


logger = logging.getLogger(__name__)

SYNC_SCHEMA = 'retype-learning-sync-replica'
COLLECTION_SCHEMA = 'retype-learning-sync-collection'
BOOTSTRAP_SCHEMA = 'retype-local-sync-bootstrap'
SYNC_VERSION = 1
MAX_REPLICA_BYTES = 5 * 1024 * 1024
MAX_DEFERRED_BYTES = MAX_REPLICA_BYTES
MAX_MANAGED_BOOK_BYTES = 100 * 1024 * 1024
MAX_MANAGED_LIBRARY_BYTES = 1024 * 1024 * 1024
MAX_BACKUPS = 5
VALID_SETTINGS = (
    'sdict', 'rdict', 'auto_newline', 'adaptive_chord_lessons',
    'adaptive_chord_lesson_limit', 'steno.kdict',
)
_BOOK_IDENTITY = re.compile(r'^(?:[0-9a-f]{32}|[0-9a-f]{64})$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class SyncError(RuntimeError):
    """An expected sync/recovery condition, safe to show to the user."""


class ValidationError(SyncError):
    """An untrusted file does not satisfy the current protocol."""


@dataclass(frozen=True, order=True)
class HLC:
    """Small serialisable hybrid logical clock used for deterministic LWW."""

    wall_ms: int
    counter: int

    def to_data(self) -> dict[str, int]:
        return {'wall_ms': self.wall_ms, 'counter': self.counter}

    @classmethod
    def from_data(cls, data: object) -> 'HLC':
        if not isinstance(data, dict):
            raise ValidationError('timestamp is not an object')
        wall = data.get('wall_ms')
        counter = data.get('counter')
        if not _is_int(wall) or wall < 0 or not _is_int(counter) or counter < 0:
            raise ValidationError('timestamp is malformed')
        return cls(wall, counter)


@dataclass
class SyncStatus:
    state: str = 'local-only'
    message: str = 'Sync is off. Learning data is stored only on this device.'
    last_checked: float | None = None
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    status: SyncStatus
    save: dict[str, dict[str, object]] = field(default_factory=dict)
    chord_counts: dict[str, int] = field(default_factory=dict)
    chord_overrides: dict[str, bool] = field(default_factory=dict)
    settings: dict[str, object] = field(default_factory=dict)
    settings_revisions: dict[str, int] = field(default_factory=dict)
    managed_books: dict[str, dict[str, object]] = field(default_factory=dict)
    # Kept separate from ``save`` because legacy BookView accepts every save
    # mapping key as an attribute.  V1 opens the furthest position, while this
    # retained LWW value makes a later "recent position" recovery UX possible.
    book_resumes: dict[str, dict[str, object]] = field(default_factory=dict)
    managed_books_materialized: bool = False
    managed_books_ready: set[str] = field(default_factory=set)
    managed_books_loaded: dict[str, object] = field(default_factory=dict)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_count_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: count for key, count in value.items()
        if isinstance(key, str) and key and _is_int(count) and count >= 0
    }


def _valid_override_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        key: mastered for key, mastered in value.items()
        if isinstance(key, str) and key and isinstance(mastered, bool)
    }


def _json_bytes(data: object) -> bytes:
    try:
        return json.dumps(data, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False).encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValidationError('JSON contains invalid Unicode') from error


def _digest(data: object) -> str:
    return sha256(_json_bytes(data)).hexdigest()


def _safe_basename(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    name = os.path.basename(value)
    if name != value or name in ('.', '..'):
        return None
    return name


def _read_json(path: Path, maximum: int = MAX_REPLICA_BYTES) -> object:
    try:
        if path.stat().st_size > maximum:
            raise ValidationError('file exceeds the size limit')
        with path.open('r', encoding='utf-8') as file:
            return json.load(file)
    except ValidationError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ValidationError(str(error)) from error


def atomic_write_json(path: Path, data: object, backup_dir: Path | None = None,
                      max_backups: int = MAX_BACKUPS) -> None:
    """Durably replace JSON in one filesystem directory.

    The provider only observes a completed rename.  Previous bytes are copied
    to a bounded *local* recovery directory before replacement, never into the
    selected sync folder.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup_dir is not None and path.exists():
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = '{}-{}'.format(int(time.time() * 1000), uuid4().hex[:8])
            backup = backup_dir / '{}.{}.bak'.format(path.name, stamp)
            shutil.copy2(path, backup)
            backups = sorted(backup_dir.glob(path.name + '.*.bak'),
                             key=lambda item: item.stat().st_mtime,
                             reverse=True)
            for stale in backups[max_backups:]:
                stale.unlink(missing_ok=True)
        except OSError as error:
            # A backup failure must not turn a healthy local write into a
            # destructive truncate/write operation.  Continue with replace
            # and leave a diagnostic in the normal log.
            logger.warning('Could not create sync backup for %s: %s', path, error)

    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
        with os.fdopen(descriptor, 'wb') as file:
            descriptor = None
            file.write(_json_bytes(data))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Not every platform/filesystem permits fsync on a directory.
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _copy_atomic(source: Path, destination: Path,
                 expected_digest: str | None = None,
                 expected_size: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix='.' + destination.name + '.', suffix='.tmp',
            dir=str(destination.parent))
        with source.open('rb') as input_file, os.fdopen(descriptor, 'wb') as output:
            descriptor = None
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        temporary_path = Path(temporary)
        if expected_size is not None and temporary_path.stat().st_size != expected_size:
            raise OSError('copied file size does not match its metadata')
        if expected_digest is not None and _file_sha256(temporary_path) != expected_digest:
            raise OSError('copied file digest does not match its metadata')
        os.replace(temporary, destination)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _file_md5(path: Path) -> str:
    digest = md5()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_payload() -> dict[str, object]:
    return {
        'books': {},
        'chords': {'counts': {}, 'overrides': {}},
        'settings': {},
        'managed_books': {},
    }


def _progress_key(data: Mapping[str, object]) -> tuple[float, int, int]:
    """Order a legacy save by overall progress before chapter-local offset."""
    return (float(data['progress']), int(data['chapter_pos']),
            int(data['persistent_pos']))


def _validate_save(data: object) -> dict[str, object] | None:
    if not isinstance(data, dict):
        return None
    persistent = data.get('persistent_pos')
    chapter = data.get('chapter_pos')
    progress = data.get('progress')
    if (not _is_int(persistent) or persistent < 0 or not _is_int(chapter) or
            chapter < 0 or not isinstance(progress, (int, float)) or
            isinstance(progress, bool) or progress < 0 or progress > 100):
        return None
    try:
        progress_value = float(progress)
    except (OverflowError, ValueError):
        return None
    if not isfinite(progress_value):
        return None
    out: dict[str, object] = {
        'persistent_pos': persistent,
        'chapter_pos': chapter,
        'progress': progress_value,
    }
    friendly = _safe_basename(data.get('friendly_name'))
    if friendly is not None:
        out['friendly_name'] = friendly
    return out


def _validate_settings_value(name: str, value: object) -> bool:
    if name == 'auto_newline' or name == 'adaptive_chord_lessons':
        return isinstance(value, bool)
    if name == 'adaptive_chord_lesson_limit':
        return _is_int(value) and 1 <= value <= 99
    if name == 'sdict':
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, dict) and
            isinstance(item.get('keep'), bool) for key, item in value.items())
    if name == 'rdict':
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, list) and
            all(isinstance(part, str) for part in item)
            for key, item in value.items())
    if name == 'steno.kdict':
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, list) and
            all(isinstance(part, str) for part in item)
            for key, item in value.items())
    return False


def learning_settings_from_config(config: Mapping[str, object]) -> dict[str, object]:
    """Return only typed, portable learning preferences from a mixed config."""
    result = {}
    for name in VALID_SETTINGS:
        if name == 'steno.kdict':
            steno = config.get('steno')
            value = steno.get('kdict') if isinstance(steno, dict) else None
        else:
            value = config.get(name)
        if _validate_settings_value(name, value):
            result[name] = deepcopy(value)
    return result


def apply_learning_settings(config: Mapping[str, object],
                            settings: Mapping[str, object]) -> dict[str, object]:
    """Apply the allowlisted settings without ever touching local-only keys."""
    result = deepcopy(dict(config))
    for name, value in settings.items():
        if name not in VALID_SETTINGS or not _validate_settings_value(name, value):
            continue
        if name == 'steno.kdict':
            steno = result.get('steno')
            if not isinstance(steno, dict):
                steno = {}
                result['steno'] = steno
            steno['kdict'] = deepcopy(value)
        else:
            result[name] = deepcopy(value)
    return result


def _register_stamp(value: object) -> tuple[HLC, str] | None:
    if not isinstance(value, dict):
        return None
    try:
        stamp = HLC.from_data(value.get('hlc'))
    except ValidationError:
        return None
    replica_id = value.get('replica_id')
    if not _valid_uuid(replica_id):
        return None
    return stamp, replica_id


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def validate_envelope(data: object, collection_id: str | None = None) -> dict[str, object]:
    """Validate an untrusted replica before it can influence local state."""
    if not isinstance(data, dict):
        raise ValidationError('replica is not a JSON object')
    if data.get('schema') != SYNC_SCHEMA or data.get('version') != SYNC_VERSION:
        raise ValidationError('unsupported replica schema/version')
    if not _valid_uuid(data.get('collection_id')):
        raise ValidationError('replica has no valid collection id')
    if collection_id is not None and data['collection_id'] != collection_id:
        raise ValidationError('replica belongs to another collection')
    if not _valid_uuid(data.get('replica_id')):
        raise ValidationError('replica has no valid replica id')
    if not _is_int(data.get('sequence')) or data['sequence'] < 1:
        raise ValidationError('replica sequence is invalid')
    HLC.from_data(data.get('hlc'))
    predecessor = data.get('predecessor_digest')
    if predecessor is not None and (not isinstance(predecessor, str) or
                                   not _SHA256.fullmatch(predecessor)):
        raise ValidationError('replica predecessor digest is invalid')
    payload = data.get('payload')
    digest = data.get('payload_digest')
    if not isinstance(payload, dict) or not isinstance(digest, str) or \
            not _SHA256.fullmatch(digest) or _digest(payload) != digest:
        raise ValidationError('replica payload digest does not match')
    _validate_payload(payload)
    return data


def _validate_payload(payload: Mapping[str, object]) -> None:
    books = payload.get('books', {})
    if not isinstance(books, dict):
        raise ValidationError('books is malformed')
    for identity, entry in books.items():
        if not isinstance(identity, str) or not _BOOK_IDENTITY.fullmatch(identity) or \
                not isinstance(entry, dict) or _validate_save(entry) is None:
            raise ValidationError('book progress is malformed')
        last = entry.get('last_resume')
        if last is not None:
            if not isinstance(last, dict) or _validate_save(last) is None or \
                    _register_stamp(last) is None:
                raise ValidationError('book resume marker is malformed')

    chords = payload.get('chords', {})
    if not isinstance(chords, dict):
        raise ValidationError('chords is malformed')
    counts = chords.get('counts', {})
    overrides = chords.get('overrides', {})
    if not isinstance(counts, dict) or not isinstance(overrides, dict):
        raise ValidationError('chord data is malformed')
    for key, count in counts.items():
        if not isinstance(key, str) or not key or not _is_int(count) or count < 0:
            raise ValidationError('chord count is malformed')
    for key, register in overrides.items():
        value = register.get('value') if isinstance(register, dict) else None
        if not isinstance(key, str) or not key or not isinstance(register, dict) or \
                (value is not None and not isinstance(value, bool)) or \
                _register_stamp(register) is None:
            raise ValidationError('chord override is malformed')

    settings = payload.get('settings', {})
    if not isinstance(settings, dict):
        raise ValidationError('settings is malformed')
    for key, register in settings.items():
        if not isinstance(key, str) or key not in VALID_SETTINGS or \
                not isinstance(register, dict) or \
                not _validate_settings_value(key, register.get('value')) or \
                _register_stamp(register) is None:
            raise ValidationError('learning setting is malformed')

    applied_deferred = payload.get('_applied_deferred', [])
    if not isinstance(applied_deferred, list) or any(
            not isinstance(item, str) or not _valid_uuid(item)
            for item in applied_deferred):
        raise ValidationError('deferred application markers are malformed')

    books_meta = payload.get('managed_books', {})
    if not isinstance(books_meta, dict):
        raise ValidationError('managed books is malformed')
    for digest, metadata in books_meta.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or \
                not _validate_book_metadata(digest, metadata):
            raise ValidationError('managed book metadata is malformed')


def _validate_book_metadata(digest: str, metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    return (metadata.get('schema') == 1 and metadata.get('digest') == digest and
            _safe_basename(metadata.get('original_filename')) is not None and
            isinstance(metadata.get('title'), str) and
            len(metadata['title']) <= 512 and _is_int(metadata.get('size')) and
            0 < metadata['size'] <= MAX_MANAGED_BOOK_BYTES)


def merge_replicas(replicas: list[Mapping[str, object]]) -> SyncResult:
    """Merge complete envelopes; reductions are commutative and idempotent."""
    # A folder provider can briefly expose duplicate/conflict-copy files.  One
    # component per replica identity is counted at most once.  Equal sequence
    # conflicts are deterministic here; a local owner additionally performs
    # the stronger replica-fork recovery in ``_publish``.
    unique: dict[str, Mapping[str, object]] = {}
    for envelope in replicas:
        replica_id = str(envelope['replica_id'])
        previous = unique.get(replica_id)
        if previous is None or int(envelope['sequence']) > int(previous['sequence']) or \
                (int(envelope['sequence']) == int(previous['sequence']) and
                 str(envelope['payload_digest']) > str(previous['payload_digest'])):
            unique[replica_id] = envelope

    book_candidates: dict[str, list[tuple[dict[str, object], HLC, str]]] = {}
    resume_candidates: dict[str, list[tuple[dict[str, object], HLC, str]]] = {}
    chord_counts: dict[str, int] = {}
    chord_registers: dict[str, tuple[HLC, str, object]] = {}
    setting_registers: dict[str, tuple[HLC, str, object]] = {}
    managed: dict[str, dict[str, object]] = {}

    for envelope in unique.values():
        replica_id = str(envelope['replica_id'])
        payload = envelope['payload']
        assert isinstance(payload, dict)
        envelope_stamp = HLC.from_data(envelope['hlc'])
        books = payload.get('books', {})
        assert isinstance(books, dict)
        for identity, item in books.items():
            assert isinstance(identity, str) and isinstance(item, dict)
            candidate = _validate_save(item)
            if candidate is not None:
                # The envelope timestamp is a deterministic tie breaker for
                # legacy-style maximum position entries.
                book_candidates.setdefault(identity, []).append(
                    (candidate, envelope_stamp, replica_id))
            last = item.get('last_resume')
            if isinstance(last, dict):
                last_save = _validate_save(last)
                stamp_data = _register_stamp(last)
                if last_save is not None and stamp_data is not None:
                    stamp, author = stamp_data
                    resume_candidates.setdefault(identity, []).append(
                        (last_save, stamp, author))

        chords = payload.get('chords', {})
        assert isinstance(chords, dict)
        counts = chords.get('counts', {})
        overrides = chords.get('overrides', {})
        assert isinstance(counts, dict) and isinstance(overrides, dict)
        for key, count in counts.items():
            assert isinstance(key, str) and _is_int(count)
            # One scalar belongs to one replica.  Sum one contribution from
            # each independently owned replica (a G-counter reduction).
            chord_counts[key] = chord_counts.get(key, 0) + count
        for key, register in overrides.items():
            assert isinstance(key, str) and isinstance(register, dict)
            stamp_data = _register_stamp(register)
            assert stamp_data is not None
            stamp, author = stamp_data
            candidate = (stamp, author, register.get('value'))
            if key not in chord_registers or candidate[:2] > chord_registers[key][:2]:
                chord_registers[key] = candidate

        settings = payload.get('settings', {})
        assert isinstance(settings, dict)
        for key, register in settings.items():
            assert isinstance(key, str) and isinstance(register, dict)
            stamp_data = _register_stamp(register)
            assert stamp_data is not None
            stamp, author = stamp_data
            candidate = (stamp, author, register.get('value'))
            if key not in setting_registers or candidate[:2] > setting_registers[key][:2]:
                setting_registers[key] = candidate

        metas = payload.get('managed_books', {})
        assert isinstance(metas, dict)
        for digest, metadata in metas.items():
            assert isinstance(digest, str) and isinstance(metadata, dict)
            # Identical digests must have identical bytes; deterministic
            # metadata selection prevents filename/title races changing data.
            prior = managed.get(digest)
            if prior is None or _json_bytes(metadata) < _json_bytes(prior):
                managed[digest] = deepcopy(metadata)

    save = {}
    book_resumes = {}
    for identity, candidates in book_candidates.items():
        # Furthest *overall* progress wins. ``persistent_pos`` is only an
        # offset inside one chapter, so it is deliberately the final tie-break.
        chosen, _, _ = max(candidates, key=lambda value: (
            _progress_key(value[0]), value[1], value[2]))
        save[identity] = chosen
    for identity, candidates in resume_candidates.items():
        resume, _, _ = max(candidates, key=lambda value: (value[1], value[2]))
        book_resumes[identity] = resume

    overrides = {
        key: value for key, (_, _, value) in chord_registers.items()
        if isinstance(value, bool)
    }
    settings = {key: deepcopy(value) for key, (_, _, value)
                in setting_registers.items()}
    return SyncResult(status=SyncStatus(), save=save,
                      chord_counts=chord_counts, chord_overrides=overrides,
                      settings=settings, managed_books=managed,
                      book_resumes=book_resumes)


class LearningSync:
    """Own the local replica and synchronise it through an ordinary folder.

    Public mutation methods are intentionally cheap and only update a local
    pending replica.  ``sync_now`` is the I/O-heavy operation a controller can
    run in a worker thread.
    """

    def __init__(self, local_root: str | Path, legacy_dir: str | Path):
        self.local_root = Path(local_root)
        self.legacy_dir = Path(legacy_dir)
        self.bootstrap_path = self.local_root / 'local-bootstrap.json'
        self.pending_path = self.local_root / 'pending-sync-replica.json'
        self.published_path = self.local_root / 'last-published-sync-replica.json'
        self.deferred_path = self.local_root / 'deferred-sync-mutations.json'
        self.deferred_parts_dir = self.local_root / 'deferred-sync-mutations.parts'
        self.recovery_dir = self.local_root / 'recovery' / 'sync'
        self.managed_library_dir = self.local_root / 'managed-books'
        self._loaded_managed_books: dict[str, object] = {}
        self._lock = threading.RLock()
        # UI callbacks must never wait on a provider-folder scan. A callback
        # that arrives while the worker owns ``_lock`` is replayed before the
        # next scan/publish instead.
        self._deferred_lock = threading.Lock()
        self._deferred: list[tuple[str, str, str, tuple[object, ...]]] = []
        self._finalizing_counts_baseline: dict[str, int] | None = None
        self._bootstrap_recovered = False
        self.status = SyncStatus()
        self._bootstrap = self._load_bootstrap()
        self._payload: dict[str, object] = _empty_payload()
        self._sequence = 0
        self._predecessor: str | None = None
        self._clock = HLC(0, 0)
        self._dirty = False
        self._legacy_capture_needed = False
        self._settings_revisions: dict[str, int] = {}
        baseline = self._bootstrap.get('last_materialized')
        baseline_counts = baseline.get('chord_counts') \
            if isinstance(baseline, dict) else None
        baseline_overrides = baseline.get('chord_overrides') \
            if isinstance(baseline, dict) else None
        self._materialized_counts = _valid_count_map(baseline_counts)
        self._materialized_overrides = _valid_override_map(baseline_overrides)
        self._local_chord_counts = _valid_count_map(
            self._bootstrap.get('local_chord_counts'))
        self._load_published()
        self._load_pending()
        self._initialize_materialized_count_baseline()
        self._load_deferred()
        self._prune_deferred_markers()

    @property
    def enabled(self) -> bool:
        sync = self._bootstrap.get('sync')
        return isinstance(sync, dict) and sync.get('enabled') is True

    @property
    def replica_id(self) -> str:
        return str(self._bootstrap['replica_id'])

    @property
    def collection_id(self) -> str | None:
        sync = self._bootstrap.get('sync')
        value = sync.get('collection_id') if isinstance(sync, dict) else None
        return value if _valid_uuid(value) else None

    @property
    def sync_root(self) -> Path | None:
        sync = self._bootstrap.get('sync')
        value = sync.get('root') if isinstance(sync, dict) else None
        return Path(value) if isinstance(value, str) and value else None

    @property
    def managed_library_consent(self) -> bool:
        sync = self._bootstrap.get('sync')
        return bool(sync.get('managed_library_consent')) if isinstance(sync, dict) else False

    def _load_bootstrap(self) -> dict[str, object]:
        if not self.bootstrap_path.exists():
            return {
                'schema': BOOTSTRAP_SCHEMA,
                'version': SYNC_VERSION,
                'replica_id': str(uuid4()),
                'sync': {'enabled': False, 'managed_library_consent': False},
                'legacy_migrated': False,
            }
        try:
            data = _read_json(self.bootstrap_path)
            sync = data.get('sync') if isinstance(data, dict) else None
            valid_sync = isinstance(sync, dict) and \
                (('enabled' not in sync) or isinstance(sync['enabled'], bool)) and \
                (('root' not in sync) or isinstance(sync['root'], str)) and \
                (('collection_id' not in sync) or _valid_uuid(sync['collection_id'])) and \
                (('managed_library_consent' not in sync) or
                 isinstance(sync['managed_library_consent'], bool))
            if (isinstance(data, dict) and data.get('schema') == BOOTSTRAP_SCHEMA
                    and data.get('version') == SYNC_VERSION and
                    _valid_uuid(data.get('replica_id')) and valid_sync and
                    (('legacy_migrated' not in data) or
                     isinstance(data['legacy_migrated'], bool)) and
                    (('last_materialized' not in data) or
                     isinstance(data['last_materialized'], dict)) and
                    (('local_chord_counts' not in data) or
                     isinstance(data['local_chord_counts'], dict))):
                return data
        except ValidationError as error:
            self._bootstrap_recovered = True
            self._recover_candidate(
                self.bootstrap_path,
                'local sync bootstrap could not be read: {}'.format(error))
        else:
            self._bootstrap_recovered = True
            self._recover_candidate(
                self.bootstrap_path, 'local sync bootstrap is malformed')
        return {
            'schema': BOOTSTRAP_SCHEMA,
            'version': SYNC_VERSION,
            'replica_id': str(uuid4()),
            'sync': {'enabled': False, 'managed_library_consent': False},
            'legacy_migrated': False,
        }

    def _save_bootstrap(self) -> None:
        atomic_write_json(self.bootstrap_path, self._bootstrap,
                          self.recovery_dir / 'bootstrap')

    def _install_published(self, data: Mapping[str, object]) -> None:
        self._payload = deepcopy(data['payload'])  # type: ignore[arg-type]
        self._sequence = int(data['sequence'])
        self._predecessor = data.get('predecessor_digest')  # type: ignore[assignment]
        self._clock = HLC.from_data(data['hlc'])

    def _read_published_candidate(self, path: Path,
                                  collection: str) -> dict[str, object] | None:
        if not path.exists():
            return None
        try:
            data = validate_envelope(_read_json(path), collection)
            if data['replica_id'] != self.replica_id:
                raise ValidationError('published replica file ownership is invalid')
            return data
        except ValidationError as error:
            self._recover_candidate(
                path,
                'published local sync data could not be read: {}'.format(error))
            return None

    def _remember_published_envelope(self, data: Mapping[str, object]) -> None:
        atomic_write_json(self.published_path, data,
                          self.recovery_dir / 'published')

    def _load_published(self) -> None:
        root = self.sync_root
        collection = self.collection_id
        if collection is None:
            return
        local = self._read_published_candidate(self.published_path, collection)
        provider = None
        if root is not None:
            provider = self._read_published_candidate(
                root / 'replicas' / (self.replica_id + '.json'), collection)
        selected = local
        if provider is not None and (local is None or
                                     int(provider['sequence']) > int(local['sequence'])):
            selected = provider
        elif provider is not None and local is not None:
            if int(provider['sequence']) < int(local['sequence']):
                self._diagnose('The provider replica is stale; retaining the last local publication.')
            elif provider['payload_digest'] != local['payload_digest']:
                self._diagnose('The provider replica conflicts with the last local publication.')
        if selected is None:
            return
        self._install_published(selected)
        if provider is selected and local is not selected:
            try:
                self._remember_published_envelope(selected)
            except OSError as error:
                self._diagnose(
                    'Last published sync state could not be saved: {}'.format(error))

    def _load_pending(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            data = _read_json(self.pending_path)
            collection = self.collection_id
            if collection is not None:
                data = validate_envelope(data, collection)
                if data['replica_id'] == self.replica_id and \
                        int(data['sequence']) >= self._sequence:
                    self._payload = deepcopy(data['payload'])  # type: ignore[arg-type]
                    self._sequence = int(data['sequence'])
                    self._predecessor = data.get('predecessor_digest')  # type: ignore[assignment]
                    self._clock = HLC.from_data(data['hlc'])
                    self._dirty = True
        except ValidationError as error:
            self._recover_candidate(
                self.pending_path,
                'pending local sync data could not be read: {}'.format(error))

    def _deferred_record(self, event: tuple[str, str, str, tuple[object, ...]]) -> list[object]:
        collection, event_id, kind, args = event
        return [collection, event_id, kind, list(args)]

    def _read_deferred_events(self, data: object, collection: str | None,
                              legacy: bool = False) -> list[tuple[str, str, str, tuple[object, ...]]]:
        events = data.get('events') if isinstance(data, dict) else None
        if not isinstance(events, list):
            raise ValidationError('deferred sync mutations are malformed')
        loaded = []
        for raw in events:
            if legacy:
                if not isinstance(raw, list) or len(raw) != 2:
                    raise ValidationError('deferred sync mutations are malformed')
                event_collection = collection or ''
                event_id = str(uuid4())
                kind, args = raw
            else:
                if not isinstance(raw, list) or len(raw) != 4:
                    raise ValidationError('deferred sync mutations are malformed')
                event_collection, event_id, kind, args = raw
            if not isinstance(event_collection, str) or not isinstance(event_id, str) or \
                    not _valid_uuid(event_id) or not isinstance(kind, str) or \
                    kind not in ('book', 'chords', 'settings') or \
                    not isinstance(args, list) or len(args) not in (2, 3) or \
                    (len(args) == 3 and kind != 'chords'):
                raise ValidationError('deferred sync mutations are malformed')
            if kind == 'book' and (not isinstance(args[0], str) or
                                   not isinstance(args[1], dict)):
                raise ValidationError('deferred book mutation is malformed')
            if kind in ('chords', 'settings') and \
                    (not isinstance(args[0], dict) or not isinstance(args[1], dict)):
                raise ValidationError('deferred sync mutation is malformed')
            if kind == 'chords' and len(args) == 3:
                baseline = args[2]
                if not isinstance(baseline, dict) or any(
                        not isinstance(key, str) or not key or
                        not _is_int(value) or value < 0
                        for key, value in baseline.items()):
                    raise ValidationError('deferred chord baseline is malformed')
            elif len(args) != 2:
                raise ValidationError('deferred sync mutation is malformed')
            loaded.append((event_collection, event_id, kind, tuple(args)))
        return loaded

    def _deferred_part_path(self, generation: str, index: int) -> Path:
        if not isinstance(generation, str) or not re.fullmatch(
                r'[A-Za-z0-9_-]{1,128}', generation):
            raise ValidationError('deferred sync generation is malformed')
        return self.deferred_parts_dir / '{}-{:08d}.json'.format(
            generation, index)

    def _read_deferred_generation(self, generation: str, count: int) -> list[tuple[str, str, str, tuple[object, ...]]]:
        if not isinstance(generation, str) or not generation or not _is_int(count) or \
                count < 1 or count > MAX_DEFERRED_BYTES:
            raise ValidationError('deferred sync generation is malformed')
        new_paths = [self._deferred_part_path(generation, index)
                     for index in range(count)]
        if all(path.exists() for path in new_paths):
            paths = new_paths
        else:
            paths = [self.deferred_parts_dir / '{:08d}.json'.format(index)
                     for index in range(count)]
        loaded = []
        for index, path in enumerate(paths):
            data = _read_json(path, MAX_DEFERRED_BYTES)
            if not isinstance(data, dict) or data.get('generation') != generation or \
                    data.get('index') != index or data.get('count') != count:
                raise ValidationError('deferred sync generation is incomplete')
            loaded.extend(self._read_deferred_events(data, self.collection_id))
        return loaded

    def _complete_deferred_generations(self) -> list[tuple[str, list[tuple[str, str, str, tuple[object, ...]]]]]:
        groups: dict[str, dict[int, tuple[int, object]]] = {}
        if not self.deferred_parts_dir.is_dir():
            return []
        for path in sorted(self.deferred_parts_dir.glob('*.json')):
            try:
                data = _read_json(path, MAX_DEFERRED_BYTES)
                if not isinstance(data, dict):
                    continue
                generation = data.get('generation')
                index = data.get('index')
                count = data.get('count')
                if not isinstance(generation, str) or not _is_int(index) or \
                        not _is_int(count) or index < 0 or count < 1 or index >= count:
                    continue
                groups.setdefault(generation, {})[index] = (count, data)
            except ValidationError:
                continue
        complete = []
        for generation, parts in groups.items():
            counts = {count for count, _ in parts.values()}
            count = next(iter(counts)) if len(counts) == 1 else 0
            if count < 1 or len(parts) != count or set(parts) != set(range(count)):
                continue
            try:
                events = []
                for index in range(count):
                    events.extend(self._read_deferred_events(
                        parts[index][1], self.collection_id))
                complete.append((generation, events))
            except ValidationError:
                continue
        return complete

    def _load_deferred(self) -> None:
        pointer_exists = self.deferred_path.exists()
        if not pointer_exists and not self.deferred_parts_dir.exists():
            return
        pointer_data = None
        pointer_generation = None
        pointer_usable = False
        if pointer_exists:
            try:
                pointer_data = _read_json(self.deferred_path, MAX_DEFERRED_BYTES)
                if not isinstance(pointer_data, dict):
                    raise ValidationError('deferred sync pointer is malformed')
                pointer_generation = pointer_data.get('generation')
                if pointer_generation is not None and not isinstance(pointer_generation, str):
                    raise ValidationError('deferred sync generation is malformed')
                if 'count' in pointer_data and not _is_int(pointer_data.get('count')):
                    raise ValidationError('deferred sync generation is malformed')
                pointer_usable = True
            except (OSError, SyncError, ValidationError) as error:
                self._recover_candidate(
                    self.deferred_path,
                    'deferred sync pointer could not be read: {}'.format(error))

        try:
            candidates = self._complete_deferred_generations()
            legacy = pointer_usable and isinstance(pointer_data, dict) and \
                'count' not in pointer_data
            if pointer_usable and isinstance(pointer_data, dict):
                if _is_int(pointer_data.get('count')):
                    try:
                        candidates.append((pointer_generation or '',
                                          self._read_deferred_generation(
                                              pointer_generation,
                                              pointer_data['count'])))
                    except ValidationError:
                        pass
                else:
                    try:
                        candidates.append((pointer_generation or '',
                                          self._read_deferred_events(
                                              pointer_data, self.collection_id, legacy)))
                        if pointer_generation is not None:
                            for path in sorted(self.deferred_parts_dir.glob('*.json')):
                                part = _read_json(path, MAX_DEFERRED_BYTES)
                                if not isinstance(part, dict) or \
                                        part.get('generation') != pointer_generation or \
                                        'count' in part:
                                    continue
                                candidates[-1][1].extend(self._read_deferred_events(
                                    part, self.collection_id))
                    except ValidationError:
                        pass
            if not candidates:
                raise ValidationError('deferred sync mutations are incomplete')
            selected_generation, loaded = max(
                candidates,
                key=lambda item: (len(item[0]) > 21 and item[0][:20].isdigit(),
                                  item[0]))
            with self._deferred_lock:
                self._deferred = loaded
            if not pointer_usable or not pointer_exists or legacy or \
                    selected_generation != pointer_generation:
                try:
                    with self._deferred_lock:
                        self._persist_deferred_locked()
                except (OSError, SyncError) as error:
                    self._diagnose(
                        'Deferred sync mutations could not be rewritten: {}'.format(error))
        except (OSError, SyncError, ValidationError) as error:
            self._diagnose(
                'Deferred sync mutations could not be recovered: {}'.format(error))

    def _persist_deferred_locked(self) -> None:
        if not self._deferred:
            self.deferred_path.unlink(missing_ok=True)
            if self.deferred_parts_dir.exists():
                for path in self.deferred_parts_dir.glob('*.json'):
                    path.unlink(missing_ok=True)
                self.deferred_parts_dir.rmdir()
            return

        generation = '{:020d}-{}'.format(time.time_ns(), uuid4().hex)
        chunks: list[list[list[object]]] = [[]]
        for event in self._deferred:
            record = self._deferred_record(event)
            candidate = chunks[-1] + [record]
            if len(_json_bytes({'generation': generation, 'index': 0,
                                'count': MAX_DEFERRED_BYTES,
                                'events': candidate})) > MAX_DEFERRED_BYTES:
                if not chunks[-1]:
                    raise SyncError('a deferred sync mutation exceeds the size limit')
                chunks.append([record])
            else:
                chunks[-1] = candidate
        count = len(chunks)
        if any(len(_json_bytes({'generation': generation, 'index': index,
                                'count': count, 'events': chunk})) >
               MAX_DEFERRED_BYTES for index, chunk in enumerate(chunks)):
            raise SyncError('a deferred sync mutation exceeds the size limit')

        self.deferred_parts_dir.mkdir(parents=True, exist_ok=True)
        current_parts = set()
        for index, chunk in enumerate(chunks):
            path = self._deferred_part_path(generation, index)
            current_parts.add(path)
            atomic_write_json(
                path, {'generation': generation, 'index': index, 'count': count,
                       'events': chunk}, self.recovery_dir / 'deferred')
        atomic_write_json(
            self.deferred_path,
            {'generation': generation, 'count': count},
            self.recovery_dir / 'deferred')
        for path in self.deferred_parts_dir.glob('*.json'):
            if path not in current_parts:
                path.unlink(missing_ok=True)

    def _now(self) -> HLC:
        now = int(time.time() * 1000)
        if now > self._clock.wall_ms:
            self._clock = HLC(now, 0)
        else:
            self._clock = HLC(self._clock.wall_ms, self._clock.counter + 1)
        return self._clock

    def _observe(self, stamp: HLC) -> None:
        if stamp.wall_ms > self._clock.wall_ms:
            self._clock = HLC(stamp.wall_ms, stamp.counter)
        elif stamp.wall_ms == self._clock.wall_ms:
            self._clock = HLC(stamp.wall_ms, max(stamp.counter, self._clock.counter))

    def _envelope(self) -> dict[str, object]:
        if self.collection_id is None:
            raise SyncError('no sync collection is configured')
        if self._sequence < 1:
            self._sequence = 1
        stamp = self._clock if self._clock.wall_ms else self._now()
        return {
            'schema': SYNC_SCHEMA,
            'version': SYNC_VERSION,
            'collection_id': self.collection_id,
            'replica_id': self.replica_id,
            'sequence': self._sequence,
            'hlc': stamp.to_data(),
            'predecessor_digest': self._predecessor,
            'payload_digest': _digest(self._payload),
            'payload': deepcopy(self._payload),
        }

    def _touch(self) -> None:
        self._now()
        self._sequence += 1
        self._dirty = True
        if self.enabled:
            try:
                self._bootstrap['local_chord_counts'] = dict(self._local_chord_counts)
                self._save_bootstrap()
                envelope = self._envelope()
                if len(_json_bytes(envelope)) > MAX_REPLICA_BYTES:
                    raise OSError('local learning state exceeds the 5 MiB replica limit')
                atomic_write_json(self.pending_path, envelope,
                                  self.recovery_dir / 'pending')
            except OSError as error:
                self._diagnose(
                    'Local pending sync state could not be written: {}'.format(error))
                self.status = SyncStatus(
                    'waiting', 'Local sync state could not be saved; retrying.',
                    time.time(), list(self.status.diagnostics))
                raise

    def _touch_or_defer(self, kind: str, *args: object,
                        deferred_id: str | None = None) -> None:
        try:
            self._touch()
        except OSError:
            if deferred_id is not None:
                raise
            if self._defer(kind, *args):
                return
            try:
                self._bootstrap['legacy_migrated'] = False
                self._save_bootstrap()
            except OSError as error:
                self._diagnose(
                    'Local sync recovery state could not be written: {}'.format(error))

    def _diagnose(self, message: str) -> None:
        logger.warning('Learning sync: %s', message)
        self.status.diagnostics.append(message)
        self.status.diagnostics = self.status.diagnostics[-20:]

    def _defer(self, kind: str, *args: object) -> bool:
        event_args = args
        with self._deferred_lock:
            if kind == 'chords' and self._finalizing_counts_baseline is not None:
                baseline = dict(self._finalizing_counts_baseline)
                progress = args[0] if args and isinstance(args[0], dict) else {}
                for key, total in progress.items():
                    if isinstance(key, str) and key and _is_int(total) and total >= 0:
                        self._finalizing_counts_baseline[key] = max(
                            self._finalizing_counts_baseline.get(key, 0), total)
                event_args = args + (baseline,)
            event = (self.collection_id or '', str(uuid4()), kind, event_args)
            self._deferred.append(event)
            try:
                self._persist_deferred_locked()
                return True
            except (OSError, SyncError) as error:
                logger.warning('Deferred sync mutation could not be persisted: %s', error)
                return False

    def _prune_deferred_markers(self) -> None:
        markers = self._payload.get('_applied_deferred')
        if not isinstance(markers, list):
            return
        with self._deferred_lock:
            active = {event[1] for event in self._deferred}
        retained = [marker for marker in markers if marker in active]
        if retained == markers:
            return
        if retained:
            self._payload['_applied_deferred'] = retained
        else:
            self._payload.pop('_applied_deferred', None)
        if self.enabled:
            try:
                self._touch()
            except OSError as error:
                self._diagnose(
                    'Stale deferred markers could not be cleared: {}'.format(error))

    def _deferred_marker_present(self, event_id: str) -> bool:
        markers = self._payload.get('_applied_deferred', [])
        return isinstance(markers, list) and event_id in markers

    def _mark_deferred_applied(self, event_id: str) -> None:
        markers = self._payload.setdefault('_applied_deferred', [])
        if isinstance(markers, list) and event_id not in markers:
            markers.append(event_id)

    def _forget_deferred_marker(self, event_id: str) -> None:
        markers = self._payload.get('_applied_deferred')
        if isinstance(markers, list) and event_id in markers:
            markers.remove(event_id)
            if not markers:
                self._payload.pop('_applied_deferred', None)
            self._touch()

    def _apply_deferred(self) -> None:
        while True:
            with self._deferred_lock:
                if not self._deferred:
                    return
                event = self._deferred[0]
            collection, event_id, kind, args = event
            if collection == (self.collection_id or ''):
                if kind == 'book':
                    self.record_book(str(args[0]), args[1], _deferred_id=event_id)  # type: ignore[arg-type]
                elif kind == 'chords':
                    baseline = args[2] if len(args) == 3 else None
                    self.record_chords(
                        args[0], args[1], _deferred_id=event_id,
                        _deferred_baseline=baseline)  # type: ignore[arg-type]
                elif kind == 'settings':
                    self.record_settings(args[0], args[1], _deferred_id=event_id)  # type: ignore[arg-type]
            with self._deferred_lock:
                if self._deferred and self._deferred[0] == event:
                    self._deferred.pop(0)
                    self._persist_deferred_locked()
            self._forget_deferred_marker(event_id)

    @property
    def has_deferred_changes(self) -> bool:
        with self._deferred_lock:
            return bool(self._deferred)

    def _recover_candidate(self, path: Path, reason: str) -> bool:
        recovered = True
        try:
            self.recovery_dir.mkdir(parents=True, exist_ok=True)
            bytes_ = path.read_bytes()
            name = '{}.{}.rejected'.format(path.name, sha256(bytes_).hexdigest()[:12])
            (self.recovery_dir / name).write_bytes(bytes_)
        except OSError as error:
            recovered = False
            self._diagnose('{} could not be preserved: {}'.format(path.name, error))
        self._diagnose('{}: {}'.format(path.name, reason))
        return recovered

    def _recover_deferred_state(self, reason: str) -> bool:
        paths = []
        if self.deferred_path.exists():
            paths.append(self.deferred_path)
        if self.deferred_parts_dir.is_dir():
            paths.extend(sorted(self.deferred_parts_dir.glob('*.json')))
        return all(self._recover_candidate(path, reason) for path in paths)

    def configure(self, sync_root: str | Path,
                  managed_library_consent: bool = False) -> SyncStatus:
        """Opt in to a user-selected directory without deleting anything."""
        root = Path(sync_root).expanduser()
        with self._lock:
            try:
                root.mkdir(parents=True, exist_ok=True)
                if not root.is_dir():
                    raise SyncError('the selected path is not a folder')
                manifest_path = root / 'retype-sync.json'
                if manifest_path.exists():
                    manifest = _read_json(manifest_path)
                    if not isinstance(manifest, dict) or \
                            manifest.get('schema') != COLLECTION_SCHEMA or \
                            manifest.get('version') != SYNC_VERSION or \
                            not _valid_uuid(manifest.get('collection_id')):
                        raise SyncError('the selected folder has an unsupported retype collection')
                    collection_id = str(manifest['collection_id'])
                else:
                    collection_id = str(uuid4())
                    atomic_write_json(manifest_path, {
                        'schema': COLLECTION_SCHEMA,
                        'version': SYNC_VERSION,
                        'collection_id': collection_id,
                    }, self.recovery_dir / 'manifest')
                previous_collection = self.collection_id
                if previous_collection != collection_id:
                    if self.pending_path.exists() and not self._recover_candidate(
                            self.pending_path,
                            'pending state was retained while switching sync collections'):
                        raise SyncError(
                            'pending sync state could not be preserved while switching collections')
                    if (self.deferred_path.exists() or self.deferred_parts_dir.is_dir()) and \
                            not self._recover_deferred_state(
                                'deferred state was retained while switching sync collections'):
                        raise SyncError(
                            'deferred sync state could not be preserved while switching collections')
                    with self._deferred_lock:
                        self._deferred.clear()
                        self._persist_deferred_locked()
                    # A different folder is a different collection, not an
                    # opportunity to reuse old G-counter components.
                    self._payload = _empty_payload()
                    self._sequence = 0
                    self._predecessor = None
                    self._materialized_counts = {}
                    self._materialized_overrides = {}
                    self._local_chord_counts = {}
                    self._bootstrap.pop('last_materialized', None)
                    self._bootstrap.pop('local_chord_counts', None)
                    self.pending_path.unlink(missing_ok=True)
                    self._bootstrap['legacy_migrated'] = False
                    self._legacy_capture_needed = False
                self._bootstrap['sync'] = {
                    'enabled': True,
                    'root': str(root),
                    'collection_id': collection_id,
                    'managed_library_consent': bool(managed_library_consent),
                }
                self._save_bootstrap()
                self._prune_deferred_markers()
                if previous_collection == collection_id and \
                        self._bootstrap.get('legacy_migrated') is True:
                    self._capture_local_only_changes()
                    self._legacy_capture_needed = False
                else:
                    self._migrate_legacy_once()
                self.status = SyncStatus('ready',
                    'Local changes are queued for the selected sync folder.')
                return self.status
            except (OSError, SyncError, ValidationError) as error:
                self.status = SyncStatus('waiting',
                    'Waiting for the selected sync folder: {}'.format(error))
                return self.status

    def set_managed_library_consent(self, consent: bool) -> SyncStatus:
        # type: (LearningSync, bool) -> SyncStatus
        with self._lock:
            sync = self._bootstrap.get('sync')
            if not isinstance(sync, dict) or not self.enabled:
                return self.status
            sync['managed_library_consent'] = bool(consent)
            try:
                self._save_bootstrap()
            except OSError as error:
                self._diagnose(
                    'Local sync settings could not be saved: {}'.format(error))
                self.status = SyncStatus(
                    'waiting', 'Local sync settings could not be saved; retrying.',
                    time.time(), list(self.status.diagnostics))
            return self.status

    def disable(self) -> SyncStatus:
        """Return to local-only operation and intentionally retain folder data."""
        with self._lock:
            sync = self._bootstrap.setdefault('sync', {})
            assert isinstance(sync, dict)
            sync['enabled'] = False
            try:
                self._save_bootstrap()
            except OSError as error:
                self._diagnose(
                    'Local sync settings could not be saved: {}'.format(error))
                self.status = SyncStatus(
                    'waiting', 'Local sync settings could not be saved; retrying.',
                    time.time(), list(self.status.diagnostics))
                return self.status
            self.status = SyncStatus()
            return self.status

    def set_legacy_dir(self, directory: str | Path) -> None:
        with self._lock:
            directory = Path(directory)
            self._legacy_capture_needed = self._legacy_capture_needed or \
                directory != self.legacy_dir
            self.legacy_dir = directory

    def _migrate_legacy_once(self) -> None:
        if self._bootstrap.get('legacy_migrated') is True:
            return
        self._import_legacy_state()
        self._bootstrap['legacy_migrated'] = True
        self._save_bootstrap()

    def _last_materialized(self) -> dict[str, object]:
        data = self._bootstrap.get('last_materialized')
        return data if isinstance(data, dict) else {}

    def settings_baseline(self) -> dict[str, object]:
        baseline = self._last_materialized().get('settings')
        if isinstance(baseline, dict):
            return deepcopy(baseline)
        registers = self._payload.get('settings', {})
        if not isinstance(registers, dict):
            return {}
        return {
            key: deepcopy(register.get('value'))
            for key, register in registers.items()
            if key in VALID_SETTINGS and isinstance(register, dict) and
            _validate_settings_value(key, register.get('value'))
        }

    def _legacy_save_entries(self, raw_save: object):
        if not isinstance(raw_save, dict):
            return
        for identity, item in raw_save.items():
            if not isinstance(identity, str) or not isinstance(item, dict):
                continue
            resolved_identity = identity
            if identity.lower().endswith('.epub'):
                try:
                    if not os.path.isfile(identity):
                        continue
                    resolved_identity = _file_md5(Path(identity))
                except OSError as error:
                    self._diagnose('Legacy progress file could not be hashed: {}'.format(error))
                    continue
            if _BOOK_IDENTITY.fullmatch(resolved_identity):
                valid = _validate_save(item)
                if valid is not None:
                    yield resolved_identity, valid

    def _capture_local_only_changes(self) -> None:
        """Queue changes made while sync was disabled without re-counting it."""
        baseline = self._last_materialized()
        baseline_counts = _valid_count_map(baseline.get('chord_counts', {}))
        baseline_overrides = _valid_override_map(baseline.get('chord_overrides', {}))
        baseline_settings = baseline.get('settings', {})
        self._materialized_counts = {
            **baseline_counts, **self._materialized_counts}
        self._materialized_overrides = {
            **baseline_overrides, **self._materialized_overrides}

        chord_path = self.legacy_dir / 'chord-mastery.json'
        try:
            raw = _read_json(chord_path)
            if isinstance(raw, dict):
                progress = raw.get('progress', {})
                overrides = raw.get('manual_overrides', {})
                self.record_chords(progress if isinstance(progress, dict) else {},
                                   overrides if isinstance(overrides, dict) else {})
        except ValidationError as error:
            if chord_path.exists():
                self._diagnose('Local-only chord changes could not be queued: {}'.format(error))

        config_path = self.legacy_dir / 'config.json'
        try:
            config = _read_json(config_path)
            if isinstance(config, dict):
                previous = apply_learning_settings({}, baseline_settings) \
                    if isinstance(baseline_settings, dict) else {}
                self.record_settings(config, previous)
        except ValidationError as error:
            if config_path.exists():
                self._diagnose('Local-only learning settings could not be queued: {}'.format(error))

        save_path = self.legacy_dir / 'save.json'
        try:
            raw_save = _read_json(save_path)
            if isinstance(raw_save, dict):
                for identity, value in self._legacy_save_entries(raw_save):
                    prior = self._payload.get('books', {})
                    prior_value = prior.get(identity) if isinstance(prior, dict) else None
                    if _validate_save(prior_value) is None or \
                            _progress_key(value) > _progress_key(prior_value):
                        self.record_book(identity, value)
        except ValidationError as error:
            if save_path.exists():
                self._diagnose('Local-only book progress could not be queued: {}'.format(error))

    def _import_legacy_state(self) -> None:
        books = self._payload.setdefault('books', {})
        chords = self._payload.setdefault('chords', {'counts': {}, 'overrides': {}})
        settings = self._payload.setdefault('settings', {})
        if not isinstance(books, dict) or not isinstance(chords, dict) or \
                not isinstance(settings, dict):
            raise SyncError('local pending state is malformed')
        counts = chords.setdefault('counts', {})
        overrides = chords.setdefault('overrides', {})
        if not isinstance(counts, dict) or not isinstance(overrides, dict):
            raise SyncError('local chord state is malformed')

        save_path = self.legacy_dir / 'save.json'
        try:
            raw_save = _read_json(save_path)
            if isinstance(raw_save, dict):
                for identity, valid in self._legacy_save_entries(raw_save):
                    prior = books.get(identity)
                    if _validate_save(prior) is None or \
                            _progress_key(valid) > _progress_key(prior):
                        books[identity] = valid
        except ValidationError as error:
            if save_path.exists():
                self._diagnose('Legacy progress was not imported: {}'.format(error))

        chord_path = self.legacy_dir / 'chord-mastery.json'
        try:
            raw_chords = _read_json(chord_path)
            if isinstance(raw_chords, dict) and isinstance(raw_chords.get('progress'), dict):
                for key, count in raw_chords['progress'].items():
                    if isinstance(key, str) and key and _is_int(count) and count >= 0:
                        counts[key] = max(int(counts.get(key, 0)), count)
                raw_overrides = raw_chords.get('manual_overrides', {})
                if isinstance(raw_overrides, dict):
                    for key, value in raw_overrides.items():
                        if isinstance(key, str) and key and isinstance(value, bool):
                            stamp = self._now()
                            overrides[key] = {
                                'value': value, 'hlc': stamp.to_data(),
                                'replica_id': self.replica_id,
                            }
        except ValidationError as error:
            if chord_path.exists():
                self._diagnose('Legacy chord progress was not imported: {}'.format(error))

        config_path = self.legacy_dir / 'config.json'
        try:
            config = _read_json(config_path)
            if isinstance(config, dict):
                for key, value in learning_settings_from_config(config).items():
                    stamp = self._now()
                    settings[key] = {'value': value, 'hlc': stamp.to_data(),
                                     'replica_id': self.replica_id}
        except ValidationError as error:
            if config_path.exists():
                self._diagnose('Legacy learning settings were not imported: {}'.format(error))
        self._initialize_materialized_count_baseline()
        self._touch()

    def _initialize_materialized_count_baseline(self) -> None:
        chords = self._payload.get('chords', {})
        counts = chords.get('counts', {}) if isinstance(chords, dict) else {}
        if not isinstance(counts, dict):
            return
        last = self._last_materialized()
        last_counts = _valid_count_map(last.get('chord_counts', {}))
        for key, count in {**last_counts, **counts}.items():
            if isinstance(key, str) and key and _is_int(count) and count >= 0:
                self._local_chord_counts[key] = max(
                    self._local_chord_counts.get(key, 0), count)

    def deferred_settings(self) -> dict[str, object]:
        pending: dict[str, object] = {}
        collection = self.collection_id or ''
        with self._deferred_lock:
            events = list(self._deferred)
        for event_collection, _, kind, args in events:
            if event_collection != collection or kind != 'settings' or len(args) < 2:
                continue
            current = args[0]
            previous = args[1]
            if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
                continue
            values = learning_settings_from_config(current)
            before = learning_settings_from_config(previous)
            for key, value in values.items():
                if value != before.get(key):
                    pending[key] = deepcopy(value)
        return pending

    def record_book(self, identity: str, data: Mapping[str, object],
                    _deferred_id: str | None = None) -> None:
        if not _BOOK_IDENTITY.fullmatch(identity):
            return
        valid = _validate_save(dict(data))
        if valid is None:
            return
        if not self._lock.acquire(blocking=False):
            self._defer('book', identity, dict(data))
            return
        try:
            if not self.enabled:
                return
            if _deferred_id is not None and self._deferred_marker_present(_deferred_id):
                return
            books = self._payload.setdefault('books', {})
            assert isinstance(books, dict)
            prior = books.get(identity)
            if isinstance(prior, dict):
                prior_valid = _validate_save(prior)
                if prior_valid and _progress_key(prior_valid) > _progress_key(valid):
                    # Preserve a locally reached furthest position while still
                    # recording a timestamped last-resume point for diagnostics.
                    valid = prior_valid
            stamp = self._now()
            valid['last_resume'] = dict(data)
            valid['last_resume']['hlc'] = stamp.to_data()
            valid['last_resume']['replica_id'] = self.replica_id
            # Last resume has the same strict scalar validation plus metadata.
            if _validate_save(valid['last_resume']) is None:
                valid.pop('last_resume', None)
            books[identity] = valid
            if _deferred_id is not None:
                self._mark_deferred_applied(_deferred_id)
            self._touch_or_defer('book', identity, dict(data),
                                  deferred_id=_deferred_id)
        finally:
            self._lock.release()

    def record_chords(self, progress: Mapping[str, int],
                      overrides: Mapping[str, bool],
                      _deferred_id: str | None = None,
                      _deferred_baseline: Mapping[str, int] | None = None) -> None:
        if not self._lock.acquire(blocking=False):
            self._defer('chords', dict(progress), dict(overrides))
            return
        try:
            if not self.enabled:
                return
            if _deferred_id is not None and self._deferred_marker_present(_deferred_id):
                return
            chords = self._payload.setdefault('chords', {'counts': {}, 'overrides': {}})
            assert isinstance(chords, dict)
            contributions = chords.setdefault('counts', {})
            registers = chords.setdefault('overrides', {})
            assert isinstance(contributions, dict) and isinstance(registers, dict)
            changed = False
            for key, total in progress.items():
                if not isinstance(key, str) or not key or not _is_int(total) or total < 0:
                    continue
                old_total = (_deferred_baseline.get(key, 0)
                             if _deferred_baseline is not None else max(
                                 self._materialized_counts.get(key, 0),
                                 self._local_chord_counts.get(key, 0)))
                delta = total - old_total
                if delta > 0:
                    contributions[key] = int(contributions.get(key, 0)) + delta
                    changed = True
                # Subsequent local callbacks carry a global total. Remember
                # this observation so each newly recorded use contributes one,
                # rather than repeatedly adding all local uses since a scan.
                self._materialized_counts[key] = max(
                    self._materialized_counts.get(key, 0), total)
                self._local_chord_counts[key] = max(
                    self._local_chord_counts.get(key, 0), total)
            current = {key: value for key, value in overrides.items()
                       if isinstance(key, str) and key and isinstance(value, bool)}
            for key in set(current) | set(self._materialized_overrides):
                value = current.get(key)
                was_present = key in self._materialized_overrides
                is_present = key in current
                if was_present == is_present and \
                        self._materialized_overrides.get(key) == value:
                    continue
                stamp = self._now()
                registers[key] = {'value': value, 'hlc': stamp.to_data(),
                                  'replica_id': self.replica_id}
                if value is None:
                    self._materialized_overrides.pop(key, None)
                else:
                    self._materialized_overrides[key] = value
                changed = True
            if _deferred_id is not None:
                self._mark_deferred_applied(_deferred_id)
                changed = True
            if changed:
                self._touch_or_defer('chords', dict(progress), dict(overrides),
                                      deferred_id=_deferred_id)
        finally:
            self._lock.release()

    def record_settings(self, config: Mapping[str, object],
                        previous: Mapping[str, object],
                        _deferred_id: str | None = None) -> None:
        if not self._lock.acquire(blocking=False):
            self._defer('settings', deepcopy(dict(config)),
                        deepcopy(dict(previous)))
            return
        try:
            if not self.enabled:
                return
            if _deferred_id is not None and self._deferred_marker_present(_deferred_id):
                return
            values = learning_settings_from_config(config)
            before = learning_settings_from_config(previous)
            registers = self._payload.setdefault('settings', {})
            assert isinstance(registers, dict)
            changed = False
            for key, value in values.items():
                if value == before.get(key):
                    continue
                stamp = self._now()
                registers[key] = {'value': value, 'hlc': stamp.to_data(),
                                  'replica_id': self.replica_id}
                self._settings_revisions[key] = \
                    self._settings_revisions.get(key, 0) + 1
                changed = True
            if _deferred_id is not None:
                self._mark_deferred_applied(_deferred_id)
                changed = True
            if changed:
                self._touch_or_defer('settings', deepcopy(dict(config)),
                                      deepcopy(dict(previous)),
                                      deferred_id=_deferred_id)
        finally:
            self._lock.release()

    def _collection_manifest(self, root: Path) -> str:
        manifest = _read_json(root / 'retype-sync.json')
        if not isinstance(manifest, dict) or manifest.get('schema') != COLLECTION_SCHEMA or \
                manifest.get('version') != SYNC_VERSION or \
                not _valid_uuid(manifest.get('collection_id')):
            raise SyncError('the sync folder collection manifest is unavailable or invalid')
        return str(manifest['collection_id'])

    def sync_now(self) -> SyncResult:
        """Publish the local replica then merge all valid visible replicas."""
        with self._lock:
            self._finalizing_counts_baseline = None
            try:
                self._apply_deferred()
                if not self.enabled:
                    self.status = SyncStatus()
                    return SyncResult(self.status)
                root = self.sync_root
                collection = self.collection_id
                if root is None or collection is None:
                    self.status = SyncStatus('waiting', 'Waiting for sync configuration.')
                    return SyncResult(self.status)
                if not root.is_dir():
                    raise SyncError('the selected folder is unavailable')
                if self._bootstrap.get('legacy_migrated') is True and \
                        self._bootstrap.get('last_materialized') is not None:
                    self._capture_local_only_changes()
                    self._legacy_capture_needed = False
                elif self._legacy_capture_needed and \
                        self._bootstrap.get('last_materialized') is not None:
                    self._capture_local_only_changes()
                    self._legacy_capture_needed = False
                if self._collection_manifest(root) != collection:
                    raise SyncError('the selected folder belongs to a different collection')
                replicas_dir = root / 'replicas'
                replicas_dir.mkdir(parents=True, exist_ok=True)
                self._migrate_legacy_once()
                self._publish(replicas_dir)
                replicas = self._scan_replicas(replicas_dir, collection)
                result = merge_replicas(replicas)
                while self.has_deferred_changes:
                    self._apply_deferred()
                    self._publish(replicas_dir)
                    replicas = self._scan_replicas(replicas_dir, collection)
                    result = merge_replicas(replicas)
                result.status = SyncStatus('synced',
                    'Local changes are saved in the selected sync folder.',
                    time.time(), list(self.status.diagnostics))
                self.status = result.status
                with self._deferred_lock:
                    self._finalizing_counts_baseline = dict(self._materialized_counts)
                legacy_materialized = self._materialize_legacy_state(result)
                if legacy_materialized:
                    self._remember_local_chord_baseline(result)
                result.managed_books_ready = self._materialize_managed_books(
                    root, result)
                result.managed_books_materialized = bool(result.managed_books_ready)
                while self.has_deferred_changes:
                    self._apply_deferred()
                    self._publish(replicas_dir)
                    replicas = self._scan_replicas(replicas_dir, collection)
                    result = merge_replicas(replicas)
                    result.status = SyncStatus('synced',
                        'Local changes are saved in the selected sync folder.',
                        time.time(), list(self.status.diagnostics))
                    self.status = result.status
                    legacy_materialized = self._materialize_legacy_state(result)
                    if legacy_materialized:
                        self._remember_local_chord_baseline(result)
                    result.managed_books_ready = self._materialize_managed_books(
                        root, result)
                    result.managed_books_materialized = bool(result.managed_books_ready)
                with self._deferred_lock:
                    baseline = dict(self._materialized_counts)
                    self._finalizing_counts_baseline = dict(baseline)
                    pending_chords = [
                        args[0] for collection_id, _, kind, args in self._deferred
                        if collection_id == (self.collection_id or '') and
                        kind == 'chords' and args and isinstance(args[0], dict)]
                    for progress in pending_chords:
                        for key, total in progress.items():
                            if isinstance(key, str) and key and _is_int(total) and total >= 0:
                                self._finalizing_counts_baseline[key] = max(
                                    self._finalizing_counts_baseline.get(key, 0), total)
                    self._materialized_counts = dict(result.chord_counts)
                self._materialized_overrides = dict(result.chord_overrides)
                if legacy_materialized:
                    self._remember_materialized(result)
                result.status.diagnostics = list(self.status.diagnostics)
                result.settings_revisions = {
                    key: self._settings_revisions.get(key, 0)
                    for key in result.settings
                }
                if result.status.diagnostics:
                    result.status.message = (
                        'Sync completed with recovery notices. Show recovery '
                        'diagnostics for details.')
                return result
            except (OSError, SyncError, ValidationError) as error:
                self.status = SyncStatus('waiting',
                    'Waiting for the selected sync folder: {}'.format(error),
                    time.time(), list(self.status.diagnostics))
                return SyncResult(self.status)

    def _publish(self, replicas_dir: Path) -> None:
        target = replicas_dir / (self.replica_id + '.json')
        envelope = self._envelope()
        if len(_json_bytes(envelope)) > MAX_REPLICA_BYTES:
            raise SyncError('local learning state exceeds the 5 MiB replica limit')
        existing = None
        if target.exists():
            try:
                existing = validate_envelope(_read_json(target), self.collection_id)
            except ValidationError as error:
                self._recover_candidate(target, str(error))
        if existing is not None:
            if existing['replica_id'] != self.replica_id:
                raise SyncError('replica file ownership is invalid')
            existing_digest = str(existing['payload_digest'])
            local_digest = str(envelope['payload_digest'])
            if existing['sequence'] == envelope['sequence'] and existing_digest != local_digest:
                self._handle_replica_fork(existing, envelope)
                raise SyncError('replica identity conflict requires recovery')
            if int(existing['sequence']) > int(envelope['sequence']):
                self._handle_replica_fork(existing, envelope)
                raise SyncError('replica identity conflict requires recovery')
            if existing_digest == local_digest and \
                    existing['sequence'] == envelope['sequence']:
                self._remember_published_envelope(envelope)
                self._dirty = False
                self.pending_path.unlink(missing_ok=True)
                return
            envelope['predecessor_digest'] = existing_digest
            self._predecessor = existing_digest
        atomic_write_json(target, envelope, self.recovery_dir / 'replicas')
        self._remember_published_envelope(envelope)
        self._dirty = False
        self.pending_path.unlink(missing_ok=True)

    def _handle_replica_fork(self, remote: Mapping[str, object],
                             local: Mapping[str, object]) -> None:
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.recovery_dir / ('replica-fork-{}.json'.format(
            int(time.time() * 1000))), {'remote': remote, 'local': local})
        old = self.replica_id
        self._bootstrap['replica_id'] = str(uuid4())
        self._payload = _empty_payload()
        self._sequence = 0
        self._predecessor = None
        self._dirty = False
        self._save_bootstrap()
        self._diagnose('Replica {} was duplicated. A new identity was created; '
                       'the divergent state is in recovery diagnostics.'.format(old))

    def _scan_replicas(self, directory: Path, collection: str) -> list[dict[str, object]]:
        replicas = []
        for path in sorted(directory.glob('*.json')):
            try:
                envelope = validate_envelope(_read_json(path), collection)
                self._observe(HLC.from_data(envelope['hlc']))
                replicas.append(envelope)
            except ValidationError as error:
                self._recover_candidate(path, str(error))
        return replicas

    def _remember_local_chord_baseline(self, result: SyncResult) -> None:
        self._local_chord_counts = dict(result.chord_counts)
        self._bootstrap['local_chord_counts'] = dict(self._local_chord_counts)
        self._save_bootstrap()

    def _remember_materialized(self, result: SyncResult) -> None:
        self._bootstrap['last_materialized'] = {
            'chord_counts': dict(result.chord_counts),
            'chord_overrides': dict(result.chord_overrides),
            'settings': deepcopy(result.settings),
        }
        self._save_bootstrap()

    def _materialize_legacy_state(self, result: SyncResult) -> bool:
        """Keep existing local consumers working from a merged cache.

        These files are a materialisation, not the protocol and never get
        uploaded directly.  Unknown legacy save entries remain local so an
        older/path-based entry is not silently discarded.
        """
        success = True
        try:
            self.legacy_dir.mkdir(parents=True, exist_ok=True)
            save_path = self.legacy_dir / 'save.json'
            save_valid = True
            try:
                current = _read_json(save_path)
            except ValidationError as error:
                current = {}
                save_valid = not save_path.exists()
                success = success and save_valid
                self._diagnose('Existing local progress was left untouched: {}'.format(error))
            if save_path.exists() and not isinstance(current, dict):
                save_valid = False
                success = False
            materialized = dict(current) if isinstance(current, dict) else {}
            for identity, data in result.save.items():
                current_data = materialized.get(identity)
                if _validate_save(current_data) is None or \
                        _progress_key(data) > _progress_key(current_data):
                    materialized[identity] = deepcopy(data)
            if save_valid and (result.save or save_path.exists()):
                atomic_write_json(save_path, materialized,
                                  self.recovery_dir / 'materialized')

            chord_path = self.legacy_dir / 'chord-mastery.json'
            chord_valid = True
            try:
                chord_current = _read_json(chord_path)
            except ValidationError as error:
                chord_current = {}
                chord_valid = not chord_path.exists()
                success = success and chord_valid
                self._diagnose('Existing local chord progress was left untouched: {}'.format(error))
            if not isinstance(chord_current, dict):
                chord_data = {}
                chord_valid = False
                success = False
            else:
                chord_data = dict(chord_current)
            if isinstance(chord_current, dict) and chord_path.exists() and \
                    chord_data.get('version') not in (1, 2):
                chord_valid = False
                success = False
                self._diagnose('Existing local chord progress has an unsupported format and was left untouched.')
            raw_progress = chord_data.get('progress', {})
            if chord_path.exists() and ('progress' not in chord_data or
                                         not isinstance(raw_progress, dict)):
                chord_valid = False
                success = False
                self._diagnose('Existing local chord progress has an unsupported format and was left untouched.')
            raw_overrides = chord_data.get('manual_overrides', {})
            if chord_path.exists() and (
                    not isinstance(raw_overrides, dict) or any(
                        not isinstance(key, str) or not key or
                        not isinstance(value, bool)
                        for key, value in raw_overrides.items())):
                chord_valid = False
                success = False
                self._diagnose('Existing local chord progress has an unsupported format and was left untouched.')
            progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
            for key, count in result.chord_counts.items():
                current_count = progress.get(key)
                if not _is_int(current_count) or count > current_count:
                    progress[key] = count
            if chord_valid and (result.chord_counts or result.chord_overrides or chord_path.exists()):
                chord_data['version'] = 2
                chord_data['progress'] = progress
                if result.chord_overrides:
                    chord_data['manual_overrides'] = dict(result.chord_overrides)
                else:
                    chord_data.pop('manual_overrides', None)
                atomic_write_json(chord_path, chord_data,
                                  self.recovery_dir / 'materialized')
        except OSError as error:
            success = False
            self._diagnose('Merged learning data could not be materialized locally: {}'.format(error))
        return success

    def _materialize_managed_books(self, root: Path, result: SyncResult) -> set[str]:
        if not self.managed_library_consent:
            if result.managed_books:
                self._diagnose('Managed books are unavailable until managed-library consent is enabled.')
            return set()
        total = sum(int(meta['size']) for meta in result.managed_books.values())
        if total > MAX_MANAGED_LIBRARY_BYTES:
            self._diagnose('Managed library exceeds the configured 1 GiB limit.')
            return set()
        ready = set()
        for digest, metadata in result.managed_books.items():
            source = root / 'books' / 'sha256' / (digest + '.epub')
            destination = self.managed_library_dir / (digest + '.epub')
            try:
                if not source.exists():
                    self._diagnose('Managed book {} is missing; its progress is retained.'.format(
                        metadata['original_filename']))
                    continue
                if source.stat().st_size != metadata['size'] or _file_sha256(source) != digest:
                    self._recover_candidate(source, 'managed book hash or size mismatch')
                    continue
                if not _is_loadable_epub_file(source):
                    self._recover_candidate(source, 'managed book is not a valid EPUB')
                    continue
                if not destination.exists() or destination.stat().st_size != metadata['size'] or \
                        _file_sha256(destination) != digest:
                    _copy_atomic(source, destination, digest, int(metadata['size']))
                try:
                    loaded = _load_epub(destination)
                except Exception:
                    self._recover_candidate(destination, 'managed book could not be loaded')
                    continue
                result.managed_books_loaded[digest] = loaded
                ready.add(digest)
            except OSError as error:
                self._diagnose('Managed book {} is unavailable: {}'.format(
                    metadata['original_filename'], error))
        return ready

    def import_book(self, source: str | Path, title: str | None = None) -> dict[str, object]:
        """Explicitly copy one user-selected EPUB into the managed library."""
        path = Path(source)
        with self._lock:
            created_paths = []
            digest = None
            managed = None
            missing = object()
            previous_metadata = missing
            try:
                if not self.enabled:
                    raise SyncError('turn on sync before importing a managed book')
                if not self.managed_library_consent:
                    raise SyncError('managed-library consent is required before copying books')
                if path.suffix.lower() != '.epub' or not path.is_file():
                    raise SyncError('select a readable EPUB file')
                try:
                    from retype.resource_handler import getLibraryPath
                    bundled = Path(getLibraryPath()).resolve()
                    if os.path.commonpath((str(path.resolve()), str(bundled))) == str(bundled):
                        raise SyncError('bundled EPUBs are already available and are never uploaded')
                except ValueError:
                    pass
                try:
                    is_epub_archive = _is_epub_file(path)
                except OSError as error:
                    raise SyncError('the selected EPUB cannot be read: {}'.format(error)) from error
                if not is_epub_archive or not _is_loadable_epub_file(path):
                    raise SyncError('the selected EPUB is corrupt or unsupported')
                size = path.stat().st_size
                if size <= 0 or size > MAX_MANAGED_BOOK_BYTES:
                    raise SyncError('a managed EPUB must be at most 100 MiB')
                root = self.sync_root
                if root is None or not root.is_dir():
                    raise SyncError('the selected sync folder is unavailable')
                digest = _file_sha256(path)
                managed = self._payload.setdefault('managed_books', {})
                assert isinstance(managed, dict)
                previous_metadata = managed.get(digest, missing)
                visible = merge_replicas([self._envelope()]).managed_books
                replicas_dir = root / 'replicas'
                if replicas_dir.is_dir() and self.collection_id is not None:
                    visible.update(merge_replicas(
                        self._scan_replicas(replicas_dir, self.collection_id)).managed_books)
                all_books = dict(visible)
                all_books.update({key: value for key, value in managed.items()
                                  if isinstance(key, str) and isinstance(value, dict)})
                existing_total = sum(int(meta['size']) for key, meta in all_books.items()
                                     if key != digest and _validate_book_metadata(key, meta))
                if existing_total + size > MAX_MANAGED_LIBRARY_BYTES:
                    raise SyncError('managed library limit is 1 GiB')
                metadata = {
                    'schema': 1,
                    'digest': digest,
                    'original_filename': path.name,
                    'title': title if isinstance(title, str) and title else path.stem,
                    'size': size,
                }
                if not _validate_book_metadata(digest, metadata):
                    raise SyncError('the managed EPUB metadata is invalid')
                destination = root / 'books' / 'sha256' / (digest + '.epub')
                manifest = root / 'books' / 'sha256' / (digest + '.json')
                if destination.exists() and (destination.stat().st_size != size or
                                             _file_sha256(destination) != digest):
                    self._recover_candidate(destination, 'existing managed book does not match digest')
                    raise SyncError('the selected folder contains a rejected book with this digest')
                if not destination.exists():
                    created_paths.append(destination)
                    _copy_atomic(path, destination, digest, size)
                if not manifest.exists():
                    created_paths.append(manifest)
                atomic_write_json(manifest, metadata, self.recovery_dir / 'books')
                local_copy = self.managed_library_dir / (digest + '.epub')
                replaced_local_copy = False
                if not local_copy.exists() or local_copy.stat().st_size != size or \
                        _file_sha256(local_copy) != digest:
                    if not local_copy.exists():
                        created_paths.append(local_copy)
                    else:
                        replaced_local_copy = True
                    _copy_atomic(path, local_copy, digest, size)
                if not _is_epub_file(local_copy):
                    raise SyncError('the managed EPUB could not be loaded after copying')
                try:
                    loaded_book = _load_epub(local_copy)
                except Exception as error:
                    raise SyncError(
                        'the managed EPUB could not be loaded after copying') from error
                previous_editions = [item for key, item in managed.items()
                                     if key != digest and isinstance(item, dict) and
                                     item.get('original_filename') == path.name]
                managed[digest] = metadata
                self._touch()
                self._loaded_managed_books[digest] = loaded_book
                if previous_editions:
                    self._diagnose('Imported {} as a separate edition; progress is not '
                                   'mapped between changed EPUB bytes.'.format(path.name))
                return metadata
            except (SyncError, OSError) as error:
                for created in reversed(created_paths):
                    try:
                        created.unlink(missing_ok=True)
                    except OSError:
                        pass
                if 'local_copy' in locals() and 'replaced_local_copy' in locals() and \
                        replaced_local_copy:
                    try:
                        local_copy.unlink(missing_ok=True)
                    except OSError:
                        pass
                if managed is not None and digest is not None:
                    if previous_metadata is missing:
                        managed.pop(digest, None)
                    else:
                        managed[digest] = previous_metadata
                if isinstance(error, SyncError):
                    raise
                raise SyncError('managed EPUB import failed: {}'.format(error)) from error

    def take_loaded_managed_book(self, digest: str):
        return self._loaded_managed_books.pop(digest, None)

    def diagnostics_text(self) -> str:
        with self._lock:
            lines = [self.status.message]
            lines.extend(self.status.diagnostics)
            return '\n'.join(lines)


def _is_epub_file(path: Path) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('mimetype')
            return info.compress_type == zipfile.ZIP_STORED and \
                archive.read(info) == b'application/epub+zip'
    except (KeyError, OSError, zipfile.BadZipFile):
        return False


def _load_epub(path: Path):
    return epub.read_epub(str(path), options={'ignore_ncx': True})


def _is_loadable_epub_file(path: Path) -> bool:
    if not _is_epub_file(path):
        return False
    try:
        _load_epub(path)
    except Exception:
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
