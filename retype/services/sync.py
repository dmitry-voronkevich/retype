"""Provider-neutral learning-state synchronization for ordinary folders.

The sync protocol deliberately knows nothing about iCloud, accounts, or a
network.  A provider merely makes a user-selected directory appear locally.
Every installation owns one replica file, so an eventually-consistent provider
never has to merge a shared ``save.json`` file.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
import os
from math import isfinite
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from typing import Mapping
from uuid import UUID, uuid4


logger = logging.getLogger(__name__)

SYNC_SCHEMA = 'retype-learning-sync-replica'
COLLECTION_SCHEMA = 'retype-learning-sync-collection'
BOOTSTRAP_SCHEMA = 'retype-local-sync-bootstrap'
SYNC_VERSION = 1
MAX_REPLICA_BYTES = 5 * 1024 * 1024
MAX_MANAGED_BOOK_BYTES = 100 * 1024 * 1024
MAX_MANAGED_LIBRARY_BYTES = 1024 * 1024 * 1024
MAX_BACKUPS = 5
VALID_SETTINGS = (
    'sdict', 'rdict', 'auto_newline', 'adaptive_chord_lessons',
    'adaptive_chord_lesson_limit', 'steno.kdict',
)
_HEX = re.compile(r'^[0-9a-f]{32}$')
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
    managed_books: dict[str, dict[str, object]] = field(default_factory=dict)
    # Kept separate from ``save`` because legacy BookView accepts every save
    # mapping key as an attribute.  V1 opens the furthest position, while this
    # retained LWW value makes a later "recent position" recovery UX possible.
    book_resumes: dict[str, dict[str, object]] = field(default_factory=dict)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _json_bytes(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


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


def _copy_atomic(source: Path, destination: Path) -> None:
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
            isinstance(progress, bool) or progress < 0 or progress > 100 or
            not isfinite(progress)):
        return None
    out: dict[str, object] = {
        'persistent_pos': persistent,
        'chapter_pos': chapter,
        'progress': float(progress),
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
        if not isinstance(identity, str) or not _HEX.fullmatch(identity) or \
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
        self.recovery_dir = self.local_root / 'recovery' / 'sync'
        self.managed_library_dir = self.local_root / 'managed-books'
        self._lock = threading.RLock()
        # UI callbacks must never wait on a provider-folder scan. A callback
        # that arrives while the worker owns ``_lock`` is replayed before the
        # next scan/publish instead.
        self._deferred_lock = threading.Lock()
        self._deferred: list[tuple[str, tuple[object, ...]]] = []
        self._bootstrap = self._load_bootstrap()
        self._payload: dict[str, object] = _empty_payload()
        self._sequence = 0
        self._predecessor: str | None = None
        self._clock = HLC(0, 0)
        self._dirty = False
        self._materialized_counts: dict[str, int] = {}
        self._materialized_overrides: dict[str, bool] = {}
        self.status = SyncStatus()
        self._load_pending()

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
            if (isinstance(data, dict) and data.get('schema') == BOOTSTRAP_SCHEMA
                    and data.get('version') == SYNC_VERSION and
                    _valid_uuid(data.get('replica_id'))):
                return data
        except ValidationError as error:
            logger.warning('Ignoring malformed local sync bootstrap: %s', error)
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

    def _load_pending(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            data = _read_json(self.pending_path)
            collection = self.collection_id
            if collection is not None:
                data = validate_envelope(data, collection)
                if data['replica_id'] == self.replica_id:
                    self._payload = deepcopy(data['payload'])  # type: ignore[arg-type]
                    self._sequence = int(data['sequence'])
                    self._predecessor = data.get('predecessor_digest')  # type: ignore[assignment]
                    self._clock = HLC.from_data(data['hlc'])
                    self._dirty = True
        except ValidationError as error:
            self._recover_candidate(
                self.pending_path,
                'pending local sync data could not be read: {}'.format(error))

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
            atomic_write_json(self.pending_path, self._envelope(),
                              self.recovery_dir / 'pending')

    def _diagnose(self, message: str) -> None:
        logger.warning('Learning sync: %s', message)
        self.status.diagnostics.append(message)
        self.status.diagnostics = self.status.diagnostics[-20:]

    def _defer(self, kind: str, *args: object) -> None:
        with self._deferred_lock:
            self._deferred.append((kind, args))

    def _apply_deferred(self) -> None:
        with self._deferred_lock:
            events = self._deferred
            self._deferred = []
        for kind, args in events:
            if kind == 'book':
                self.record_book(str(args[0]), args[1])  # type: ignore[arg-type]
            elif kind == 'chords':
                self.record_chords(args[0], args[1])  # type: ignore[arg-type]
            elif kind == 'settings':
                self.record_settings(args[0], args[1])  # type: ignore[arg-type]

    @property
    def has_deferred_changes(self) -> bool:
        with self._deferred_lock:
            return bool(self._deferred)

    def _recover_candidate(self, path: Path, reason: str) -> None:
        try:
            self.recovery_dir.mkdir(parents=True, exist_ok=True)
            bytes_ = path.read_bytes()
            name = '{}.{}.rejected'.format(path.name, sha256(bytes_).hexdigest()[:12])
            (self.recovery_dir / name).write_bytes(bytes_)
        except OSError:
            pass
        self._diagnose('{}: {}'.format(path.name, reason))

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
                    # A different folder is a different collection, not an
                    # opportunity to reuse old G-counter components.
                    self._payload = _empty_payload()
                    self._sequence = 0
                    self._predecessor = None
                    self._bootstrap['legacy_migrated'] = False
                self._bootstrap['sync'] = {
                    'enabled': True,
                    'root': str(root),
                    'collection_id': collection_id,
                    'managed_library_consent': bool(managed_library_consent),
                }
                self._save_bootstrap()
                if previous_collection == collection_id and \
                        self._bootstrap.get('legacy_migrated') is True:
                    self._capture_local_only_changes()
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
            self._save_bootstrap()
            return self.status

    def disable(self) -> SyncStatus:
        """Return to local-only operation and intentionally retain folder data."""
        with self._lock:
            sync = self._bootstrap.setdefault('sync', {})
            assert isinstance(sync, dict)
            sync['enabled'] = False
            self._save_bootstrap()
            self.status = SyncStatus()
            return self.status

    def set_legacy_dir(self, directory: str | Path) -> None:
        with self._lock:
            self.legacy_dir = Path(directory)

    def _migrate_legacy_once(self) -> None:
        if self._bootstrap.get('legacy_migrated') is True:
            return
        self._import_legacy_state()
        self._bootstrap['legacy_migrated'] = True
        self._save_bootstrap()

    def _last_materialized(self) -> dict[str, object]:
        data = self._bootstrap.get('last_materialized')
        return data if isinstance(data, dict) else {}

    def _capture_local_only_changes(self) -> None:
        """Queue changes made while sync was disabled without re-counting it."""
        baseline = self._last_materialized()
        baseline_counts = baseline.get('chord_counts', {})
        baseline_overrides = baseline.get('chord_overrides', {})
        baseline_settings = baseline.get('settings', {})
        self._materialized_counts = dict(baseline_counts) if isinstance(
            baseline_counts, dict) else {}
        self._materialized_overrides = dict(baseline_overrides) if isinstance(
            baseline_overrides, dict) else {}

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
                for identity, value in raw_save.items():
                    if isinstance(identity, str) and _HEX.fullmatch(identity) and \
                            _validate_save(value) is not None:
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
                for identity, item in raw_save.items():
                    valid = _validate_save(item)
                    if isinstance(identity, str) and _HEX.fullmatch(identity) and valid:
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
        self._materialized_counts = {
            key: int(value) for key, value in counts.items()
            if isinstance(key, str) and _is_int(value) and value >= 0
        }
        self._materialized_overrides = {
            key: register['value'] for key, register in overrides.items()
            if isinstance(key, str) and isinstance(register, dict) and
            isinstance(register.get('value'), bool)
        }
        self._touch()

    def record_book(self, identity: str, data: Mapping[str, object]) -> None:
        if not _HEX.fullmatch(identity):
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
            self._touch()
        finally:
            self._lock.release()

    def record_chords(self, progress: Mapping[str, int],
                      overrides: Mapping[str, bool]) -> None:
        if not self._lock.acquire(blocking=False):
            self._defer('chords', dict(progress), dict(overrides))
            return
        try:
            if not self.enabled:
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
                old_total = self._materialized_counts.get(key, 0)
                delta = total - old_total
                if delta > 0:
                    contributions[key] = int(contributions.get(key, 0)) + delta
                    changed = True
                # Subsequent local callbacks carry a global total. Remember
                # this observation so each newly recorded use contributes one,
                # rather than repeatedly adding all local uses since a scan.
                self._materialized_counts[key] = total
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
            if changed:
                self._touch()
        finally:
            self._lock.release()

    def record_settings(self, config: Mapping[str, object],
                        previous: Mapping[str, object]) -> None:
        if not self._lock.acquire(blocking=False):
            self._defer('settings', deepcopy(dict(config)),
                        deepcopy(dict(previous)))
            return
        try:
            if not self.enabled:
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
                changed = True
            if changed:
                self._touch()
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
            self._apply_deferred()
            if not self.enabled:
                self.status = SyncStatus()
                return SyncResult(self.status)
            root = self.sync_root
            collection = self.collection_id
            if root is None or collection is None:
                self.status = SyncStatus('waiting', 'Waiting for sync configuration.')
                return SyncResult(self.status)
            try:
                if not root.is_dir():
                    raise SyncError('the selected folder is unavailable')
                if self._collection_manifest(root) != collection:
                    raise SyncError('the selected folder belongs to a different collection')
                replicas_dir = root / 'replicas'
                replicas_dir.mkdir(parents=True, exist_ok=True)
                self._migrate_legacy_once()
                self._publish(replicas_dir)
                replicas = self._scan_replicas(replicas_dir, collection)
                result = merge_replicas(replicas)
                self._materialized_counts = dict(result.chord_counts)
                self._materialized_overrides = dict(result.chord_overrides)
                self._remember_materialized(result)
                self._materialize_legacy_state(result)
                # Do not publish the final status until managed-book objects
                # have been materialized. Consumers use ``synced`` as the
                # completion signal, and otherwise a slower Windows copy can
                # race that signal and make a just-synced EPUB appear absent.
                self._materialize_managed_books(root, result)
                result.status = SyncStatus(
                    'synced',
                    'Local changes are saved in the selected sync folder.',
                    time.time(), list(self.status.diagnostics))
                self.status = result.status
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
            if existing_digest == local_digest and existing['sequence'] == envelope['sequence']:
                self._dirty = False
                return
            envelope['predecessor_digest'] = existing_digest
            self._predecessor = existing_digest
        atomic_write_json(target, envelope, self.recovery_dir / 'replicas')
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

    def _remember_materialized(self, result: SyncResult) -> None:
        self._bootstrap['last_materialized'] = {
            'chord_counts': dict(result.chord_counts),
            'chord_overrides': dict(result.chord_overrides),
            'settings': deepcopy(result.settings),
        }
        self._save_bootstrap()

    def _materialize_legacy_state(self, result: SyncResult) -> None:
        """Keep existing local consumers working from a merged cache.

        These files are a materialisation, not the protocol and never get
        uploaded directly.  Unknown legacy save entries remain local so an
        older/path-based entry is not silently discarded.
        """
        try:
            self.legacy_dir.mkdir(parents=True, exist_ok=True)
            save_path = self.legacy_dir / 'save.json'
            save_valid = True
            try:
                current = _read_json(save_path)
            except ValidationError as error:
                current = {}
                save_valid = not save_path.exists()
                self._diagnose('Existing local progress was left untouched: {}'.format(error))
            materialized = dict(current) if isinstance(current, dict) else {}
            materialized.update(deepcopy(result.save))
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
                self._diagnose('Existing local chord progress was left untouched: {}'.format(error))
            chord_data = dict(chord_current) if isinstance(chord_current, dict) else {}
            if chord_path.exists() and chord_data.get('version') not in (1, 2):
                chord_valid = False
                self._diagnose('Existing local chord progress has an unsupported format and was left untouched.')
            raw_progress = chord_data.get('progress', {})
            progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
            progress.update(result.chord_counts)
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
            self._diagnose('Merged learning data could not be materialized locally: {}'.format(error))

    def _materialize_managed_books(self, root: Path, result: SyncResult) -> None:
        total = sum(int(meta['size']) for meta in result.managed_books.values())
        if total > MAX_MANAGED_LIBRARY_BYTES:
            self._diagnose('Managed library exceeds the configured 1 GiB limit.')
            return
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
                if not destination.exists() or destination.stat().st_size != metadata['size'] or \
                        _file_sha256(destination) != digest:
                    _copy_atomic(source, destination)
            except OSError as error:
                self._diagnose('Managed book {} is unavailable: {}'.format(
                    metadata['original_filename'], error))

    def import_book(self, source: str | Path, title: str | None = None) -> dict[str, object]:
        """Explicitly copy one user-selected EPUB into the managed library."""
        try:
            return self._import_book(source, title)
        except SyncError:
            raise
        except OSError as error:
            raise SyncError('managed EPUB import failed: {}'.format(error)) from error

    def _import_book(self, source: str | Path,
                    title: str | None = None) -> dict[str, object]:
        path = Path(source)
        with self._lock:
            if not self.enabled:
                raise SyncError('turn on sync before importing a managed book')
            if not self.managed_library_consent:
                raise SyncError('managed-library consent is required before copying books')
            if path.suffix.lower() != '.epub' or not path.is_file():
                raise SyncError('select a readable EPUB file')
            # A source checkout's ``library`` is a useful ordinary local
            # library while developing or running retype from source. Its
            # files are not necessarily the EPUBs shipped in a release, so a
            # path-only check here used to reject an explicitly selected local
            # EPUB before any object or metadata could be written. Packaged
            # resource EPUBs remain excluded; those are not user library data.
            if getattr(sys, 'frozen', False) or getattr(sys, '_MEIPASS', None):
                try:
                    # Import lazily to keep this protocol module independent
                    # of resource-handler initialisation order.
                    from retype.resource_handler import getLibraryPath
                    bundled = Path(getLibraryPath()).resolve()
                    if os.path.commonpath((str(path.resolve()), str(bundled))) == str(bundled):
                        raise SyncError(
                            'bundled EPUBs are already available and are never uploaded')
                except ValueError:
                    # Different Windows volumes cannot share a common path.
                    pass
            try:
                is_epub_archive = zipfile.is_zipfile(path)
            except OSError as error:
                raise SyncError('the selected EPUB cannot be read: {}'.format(error)) from error
            if not is_epub_archive:
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
            destination = root / 'books' / 'sha256' / (digest + '.epub')
            manifest = root / 'books' / 'sha256' / (digest + '.json')
            if destination.exists() and (destination.stat().st_size != size or
                                         _file_sha256(destination) != digest):
                self._recover_candidate(destination, 'existing managed book does not match digest')
                raise SyncError('the selected folder contains a rejected book with this digest')
            destination_created = False
            local_copy = self.managed_library_dir / (digest + '.epub')
            local_copy_created = False
            manifest_before = manifest.read_bytes() if manifest.exists() else None
            previous_managed = managed.get(digest)
            previous_sequence = self._sequence
            previous_clock = self._clock
            previous_dirty = self._dirty
            manifest_written = False
            try:
                if not destination.exists():
                    destination_created = True
                    _copy_atomic(path, destination)
                manifest_written = True
                atomic_write_json(manifest, metadata, self.recovery_dir / 'books')
                if not local_copy.exists():
                    local_copy_created = True
                    _copy_atomic(path, local_copy)
                previous_editions = [item for key, item in managed.items()
                                     if key != digest and isinstance(item, dict) and
                                     item.get('original_filename') == path.name]
                managed[digest] = metadata
                self._touch()
            except OSError:
                if previous_managed is None:
                    managed.pop(digest, None)
                else:
                    managed[digest] = previous_managed
                self._sequence = previous_sequence
                self._clock = previous_clock
                self._dirty = previous_dirty
                if manifest_written:
                    try:
                        if manifest_before is None:
                            manifest.unlink(missing_ok=True)
                        else:
                            manifest.write_bytes(manifest_before)
                    except OSError:
                        pass
                if local_copy_created:
                    try:
                        local_copy.unlink(missing_ok=True)
                    except OSError:
                        pass
                if destination_created:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            if previous_editions:
                self._diagnose('Imported {} as a separate edition; progress is not '
                               'mapped between changed EPUB bytes.'.format(path.name))
            return metadata

    def diagnostics_text(self) -> str:
        with self._lock:
            lines = [self.status.message]
            lines.extend(self.status.diagnostics)
            return '\n'.join(lines)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
