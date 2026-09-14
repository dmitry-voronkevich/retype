import os
import json
import logging
import tempfile
import traceback
from hashlib import sha256
from copy import deepcopy
from pathlib import Path
from qt import QMessageBox

from typing import TYPE_CHECKING

from retype.extras.dict import SafeDict
from retype.constants import default_config
from retype.resource_handler import root_path
from retype.services.sync import atomic_write_json, _validate_save

logger = logging.getLogger(__name__)


class _SafeConfig:
    def __init__(self, default_user_dir=None, library_paths=None):
        # type: (_SafeConfig, str | None, list[str] | None) -> None
        self.config_rel_path = 'config.json'
        self._explicit_default_user_dir = default_user_dir is not None
        self.default_user_dir = default_user_dir or default_config['user_dir']
        self.defaults = deepcopy(default_config)
        self.defaults['user_dir'] = self.default_user_dir
        if library_paths is not None:
            self.defaults['library_paths'] = list(library_paths)
        self.base_config_abs_path = os.path.join(
            self.default_user_dir, self.config_rel_path)
        self._default_dir_error = None
        self.data_migration = {
            'source': None, 'copied': [], 'preserved': [], 'skipped': [],
            'backup_dir': None,
        }
        if not self._explicit_default_user_dir:
            try:
                os.makedirs(self.default_user_dir, exist_ok=True)
            except OSError as error:
                self._default_dir_error = error
                logger.warning('Could not create application data directory: %s',
                               error)
        self._migrateLegacyBundleData()
        self.config = self.raw = self.load(self.base_config_abs_path)
        self.safe_dict = SafeDict(
            self.config, self.defaults,
            ['rdict', 'sdict', 'kdict'])

    def isPathDefaultUserDir(self, path):
        # type: (_SafeConfig, str) -> bool
        return os.path.abspath(path) == \
            os.path.abspath(self.default_user_dir)

    def _read_legacy_json(self, path):
        # type: (_SafeConfig, str) -> tuple[bytes, object] | None
        try:
            with open(path, 'rb') as file:
                raw = file.read()
            return raw, json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeDecodeError, ValueError, TypeError,
                RecursionError):
            return None

    def _is_valid_legacy_file(self, filename, data):
        # type: (_SafeConfig, str, object) -> bool
        if filename == 'save.json':
            return isinstance(data, dict) and all(
                _validate_save(item) is not None for item in data.values())
        return (isinstance(data, dict) and data.get('version') in (1, 2) and
                isinstance(data.get('progress'), dict))

    def _is_valid_legacy_config(self, data):
        # type: (_SafeConfig, object) -> bool
        return (isinstance(data, dict) and
                isinstance(data.get('user_dir'), str) and
                bool(data['user_dir']))

    def _write_bytes_atomically(self, path, data):
        # type: (_SafeConfig, str, bytes) -> None
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        descriptor = None
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix='.' + os.path.basename(path) + '.', dir=directory)
            with os.fdopen(descriptor, 'wb') as file:
                descriptor = None
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            # Linking a completed temporary file is an atomic create: unlike
            # replace(), it refuses to overwrite a file that appeared after
            # migration checked the destination.
            os.link(temporary, path)
            os.unlink(temporary)
            temporary = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _backup_legacy_bytes(self, filename, data):
        # type: (_SafeConfig, str, bytes) -> str
        backup_dir = os.path.join(self.default_user_dir, 'recovery',
                                  'legacy-migration')
        name = '{}.{}.legacy.bak'.format(filename, sha256(data).hexdigest()[:12])
        backup = os.path.join(backup_dir, name)
        if not os.path.exists(backup):
            try:
                self._write_bytes_atomically(backup, data)
            except FileExistsError:
                # Another launch wrote the same content-addressed recovery
                # copy first; it is already a sufficient backup.
                pass
        self.data_migration['backup_dir'] = backup_dir
        return backup

    def _copy_legacy_file(self, filename, data, destination):
        # type: (_SafeConfig, str, bytes, str) -> None
        """Back up valid old bytes and install them only at a vacant path."""
        self._backup_legacy_bytes(filename, data)
        if os.path.exists(destination):
            self.data_migration['preserved'].append(filename)
            return
        try:
            self._write_bytes_atomically(destination, data)
        except FileExistsError:
            self.data_migration['preserved'].append(filename)
            return
        self.data_migration['copied'].append(filename)

    def _migrateLegacyBundleData(self):
        # type: (_SafeConfig) -> None
        """Copy valid source-run or bundle-root data into the writable layout.

        ``root_path`` is the old effective data root for both source launches
        and frozen applications.  The source is never modified.  A selected
        custom user directory remains selected, and existing destination files
        always win rather than being replaced by an older bundle copy.
        """
        if self._explicit_default_user_dir:
            return
        legacy_root = os.path.abspath(root_path)
        if legacy_root == os.path.abspath(self.default_user_dir):
            return
        legacy_config_path = os.path.join(legacy_root, self.config_rel_path)
        legacy_config = self._read_legacy_json(legacy_config_path) \
            if os.path.exists(legacy_config_path) else None
        existing_config = self._read_legacy_json(self.base_config_abs_path) \
            if os.path.exists(self.base_config_abs_path) else None
        legacy_files = [
            filename for filename in ('save.json', 'chord-mastery.json')
            if os.path.exists(os.path.join(legacy_root, filename))]
        if legacy_config is None and os.path.exists(legacy_config_path):
            self.data_migration['skipped'].append('config.json (unreadable)')
        if not legacy_config and not legacy_files:
            return

        legacy_data = legacy_config[1] if legacy_config else None
        legacy_user_dir = (legacy_data.get('user_dir')
                           if self._is_valid_legacy_config(legacy_data) else
                           legacy_root)
        legacy_uses_root = os.path.abspath(legacy_user_dir) == legacy_root
        existing_data = existing_config[1] if existing_config else None
        existing_user_dir = (existing_data.get('user_dir')
                             if self._is_valid_legacy_config(existing_data)
                             else None)
        target_user_dir = existing_user_dir or (
            self.default_user_dir if legacy_uses_root else legacy_user_dir)
        self.data_migration['source'] = legacy_root

        # The bootstrap config is the pointer to the active data directory.
        # Copy a valid legacy pointer only when the new bootstrap is vacant.
        if legacy_config and self._is_valid_legacy_config(legacy_data):
            migrated_config = deepcopy(legacy_data)
            if legacy_uses_root:
                migrated_config['user_dir'] = self.default_user_dir
            try:
                self._backup_legacy_bytes('config.json', legacy_config[0])
                if not os.path.exists(self.base_config_abs_path):
                    encoded_config = json.dumps(
                        migrated_config, sort_keys=True, separators=(',', ':'),
                        ensure_ascii=False).encode('utf-8')
                    try:
                        self._write_bytes_atomically(
                            self.base_config_abs_path, encoded_config)
                    except FileExistsError:
                        self.data_migration['preserved'].append('config.json')
                    else:
                        self.data_migration['copied'].append('config.json')
                else:
                    self.data_migration['preserved'].append('config.json')
            except (OSError, TypeError, ValueError) as error:
                self.data_migration['skipped'].append('config.json ({})'.format(error))
                logger.warning('Could not copy legacy config data: %s', error)
        elif legacy_config:
            self.data_migration['skipped'].append('config.json (unsupported)')

        # A legacy custom directory is still the active directory.  Do not
        # silently copy from it or create a missing external/volume path.
        if not legacy_uses_root:
            return
        if not os.path.isdir(target_user_dir):
            self.data_migration['skipped'].append(
                'learning data (active folder is unavailable)')
            return
        for filename in legacy_files:
            source = os.path.join(legacy_root, filename)
            parsed = self._read_legacy_json(source)
            if parsed is None or not self._is_valid_legacy_file(
                    filename, parsed[1]):
                self.data_migration['skipped'].append(filename + ' (unreadable)')
                continue
            try:
                self._copy_legacy_file(
                    filename, parsed[0], os.path.join(target_user_dir, filename))
            except OSError as error:
                self.data_migration['skipped'].append('{} ({})'.format(
                    filename, error))
                logger.warning('Could not copy legacy learning data: %s', error)

    def data_directory_status(self, local_sync_dir=None):
        # type: (_SafeConfig, str | None) -> str
        """A concise, user-facing account of the active folder and migration."""
        active = self.raw.get('user_dir', self.default_user_dir)
        if not isinstance(active, str) or not active:
            active = self.default_user_dir
        if not os.path.isdir(active):
            if self.isPathDefaultUserDir(active):
                reason = ('retype could not create its per-user data folder; '
                          'check permissions and available storage')
                if self._default_dir_error is not None:
                    reason += ' ({})'.format(self._default_dir_error)
            else:
                reason = ('the saved User dir setting points to a folder that is '
                          'not available; retype does not create custom or '
                          'disconnected-volume folders automatically')
            return 'Data folder: {}. It is unavailable because {}; progress and settings cannot be saved there.'.format(active, reason)

        message = ('Data folder in use: {}. It stores this device\'s progress, '
                   'chord mastery, and configuration.').format(active)
        copied = self.data_migration['copied']
        preserved = self.data_migration['preserved']
        skipped = self.data_migration['skipped']
        source = self.data_migration['source']
        if copied:
            message += ' Legacy data from {} was copied without removing the original: {}.'.format(
                source, ', '.join(copied))
        elif source and preserved:
            message += ' Legacy data remains at {}; existing local files were left unchanged.'.format(source)
        if preserved:
            message += ' Existing files not overwritten: {}.'.format(', '.join(preserved))
        if skipped:
            message += ' Not imported: {}.'.format(', '.join(skipped))
        backup_dir = self.data_migration['backup_dir']
        if backup_dir:
            message += ' Recovery copies are in {}.'.format(backup_dir)
        if local_sync_dir:
            message += ' Local sync recovery files are in {}.'.format(local_sync_dir)
        return message

    def load(self, path):
        # type: (_SafeConfig, str) -> Config
        config = self._load(path)
        if not isinstance(config, dict) or not isinstance(
                config.get('user_dir'), str) or not config['user_dir']:
            # Nothing modifies it, but may as well explicitly avoid mutation.
            # A malformed old config must not block valid legacy learning-file
            # recovery or make a fresh per-user data folder unusable.
            return deepcopy(self.defaults)

        user_dir = config['user_dir']
        if user_dir and not self.isPathDefaultUserDir(user_dir):
            custom_path = os.path.join(user_dir, self.config_rel_path)
            logger.debug("Non-default user_dir: {}\n\
Attempting to load config from: {}".format(user_dir, custom_path))
            config = self._load(custom_path)
            if not config:
                config = deepcopy(self.defaults)
                config['user_dir'] = user_dir
        # Chord maps now come only from the one-shot device read. Drop the
        # retired file setting while migrating an existing configuration, so a
        # later save cannot preserve a misleading fallback path.
        if config.pop('chords_path', None) is not None:
            logger.info("Removed obsolete chords_path configuration setting")
        return config

    def _load(self, path):
        # type: (_SafeConfig, str) -> Config | None
        if os.path.exists(path):
            logger.info(f'Read config: {path}')
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    config = json.load(f)  # type: Config
                    return config
            except (OSError, ValueError, TypeError, RecursionError) as e:
                s = 'Unable to read config file.'
                logger.error(f"{s}\n{e}", exc_info=True)
                msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
                msg.setDetailedText(f'Path: {path}\n\n'
                                    f'{traceback.format_exc()}')
                msg.exec()
        else:
            logger.debug(
                f'Config path {path} not found.\n'
                'This is normal if the config file has not been created yet.')
            return None

    def populate(self, config_dict):
        # type: (_SafeConfig, NestedDict) -> None
        self.config = self.raw = config_dict  # type: ignore[assignment]
        self.safe_dict.raw = self.raw  # type: ignore[assignment]

    def save(self):
        # type: (_SafeConfig) -> bool
        user_dir = self.raw['user_dir']
        path = os.path.join(user_dir, self.config_rel_path)
        previous_config = None
        previous_config_exists = (not self.isPathDefaultUserDir(user_dir) and
                                  os.path.exists(path))
        if previous_config_exists:
            try:
                with open(path, 'rb') as file:
                    previous_config = file.read()
            except OSError as error:
                logger.error('Unable to read config before saving: %s', error)
                return False
        if not self._save(path, self.raw):  # Saving failed
            return False

        if not self.isPathDefaultUserDir(user_dir):
            dconfig = self.loadDconfig() if os.path.exists(
                self.base_config_abs_path) else None
            dconfig = dconfig or deepcopy(self.defaults)
            dconfig['user_dir'] = user_dir
            # Keep the bootstrap at the application-data root.  The previous
            # code accidentally wrote this second copy to ``path`` again.
            if self._save(self.base_config_abs_path, dconfig):
                return True
            try:
                if not previous_config_exists:
                    os.unlink(path)
                else:
                    descriptor, temporary = tempfile.mkstemp(
                        prefix='.config-rollback-', dir=os.path.dirname(path))
                    try:
                        with os.fdopen(descriptor, 'wb') as file:
                            descriptor = None
                            file.write(previous_config)
                            file.flush()
                            os.fsync(file.fileno())
                        os.replace(temporary, path)
                        temporary = None
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                        if temporary is not None:
                            try:
                                os.unlink(temporary)
                            except OSError:
                                pass
            except OSError as error:
                logger.error('Unable to roll back config file %s: %s',
                             path, error)
            return False
        return True

    def _save(self, path, data):
        # type: (_SafeConfig, str, Config) -> bool
        try:
            logger.debug(f'Saving config: {path}')
            atomic_write_json(Path(path), data)
        except (OSError, TypeError, ValueError) as e:
            s = 'Unable to save config file.'
            logger.error(f"{s}\n{e}", exc_info=True)
            msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
            msg.setDetailedText(f'Path: {path}\n\n'
                                f'{traceback.format_exc()}')
            msg.exec()
            return False
        return True

    def loadDconfig(self):
        # type: (_SafeConfig) -> Config | None
        dconfig = None
        path = os.path.join(self.default_user_dir, self.config_rel_path)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                dconfig = json.load(f)  # type: Config
        except (OSError, ValueError, TypeError, RecursionError) as e:
            s = 'Unable to load dconfig file.'
            logger.error(f"{s}\n{e}", exc_info=True)
            msg = QMessageBox(QMessageBox.Icon.Warning, 'retype', s)
            msg.setDetailedText(f'Path: {path}\n\n'
                                f'{traceback.format_exc()}')
            msg.exec()
        return dconfig

    def __getitem__(self, key, default=None):
        # type: (_SafeConfig, str, object | None) -> object
        return self.safe_dict.__getitem__(key, default)

    def get(self, key, default=None):
        # type: (_SafeConfig, str, object | None) -> object
        return self.__getitem__(key, default)


if TYPE_CHECKING:
    from retype.extras.metatypes import (  # noqa: F401
        Config, NestedDict, SConfig)
    SafeConfig = SConfig
else:
    SafeConfig = _SafeConfig
