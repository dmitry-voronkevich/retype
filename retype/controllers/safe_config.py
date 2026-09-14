import os
import json
import logging
import shutil
import tempfile
import traceback
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
        if not self._explicit_default_user_dir:
            try:
                os.makedirs(self.default_user_dir, exist_ok=True)
            except OSError as error:
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

    def _is_complete_legacy_learning_file(self, path, filename,
                                          source_data=None):
        # type: (_SafeConfig, str, str, object | None) -> bool
        try:
            with open(path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        if filename == 'save.json':
            if not isinstance(data, dict) or any(
                    _validate_save(item) is None for item in data.values()):
                return False
            if isinstance(source_data, dict) and source_data and \
                    not set(source_data).issubset(data):
                return False
            return True
        if not isinstance(data, dict) or data.get('version') not in (1, 2) or \
                not isinstance(data.get('progress'), dict):
            return False
        if isinstance(source_data, dict) and \
                isinstance(source_data.get('progress'), dict):
            source_progress = source_data['progress']
            destination_progress = data['progress']
            if not set(source_progress).issubset(destination_progress):
                return False
            for key, value in source_progress.items():
                destination_value = destination_progress[key]
                if isinstance(value, int) and not isinstance(value, bool) and \
                        value >= 0:
                    if not isinstance(destination_value, int) or \
                            isinstance(destination_value, bool) or \
                            destination_value < value:
                        return False
                elif destination_value != value:
                    return False
            for key, value in source_data.items():
                if key not in ('version', 'progress') and \
                        (key not in data or data[key] != value):
                    return False
        return True

    def _is_unsupported_legacy_learning_file(self, path, filename):
        # type: (_SafeConfig, str, str) -> bool
        try:
            with open(path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        if filename == 'save.json':
            return not isinstance(data, dict)
        return not (isinstance(data, dict) and
                    data.get('version') in (1, 2) and
                    isinstance(data.get('progress'), dict))

    def _migrateLegacyBundleData(self):
        # type: (_SafeConfig) -> None
        """Copy, never move, old bundle-root state into the writable root.

        Frozen releases previously used the executable directory as ``user_dir``.
        A first launch after this change keeps the legacy files intact and only
        imports the two learning-state files when the legacy config used that
        old default.  A deliberately selected external user directory remains
        selected and is referenced by the new local bootstrap config.
        """
        if self._explicit_default_user_dir or os.path.exists(self.base_config_abs_path):
            return
        legacy_path = os.path.join(root_path, self.config_rel_path)
        legacy = self._load(legacy_path) if os.path.exists(legacy_path) else None
        legacy_files = tuple(filename for filename in
                             ('save.json', 'chord-mastery.json')
                             if os.path.exists(os.path.join(root_path, filename)))
        if (os.path.abspath(legacy_path) == os.path.abspath(
                self.base_config_abs_path) or
                (not isinstance(legacy, dict) and not legacy_files)):
            return
        migrated = deepcopy(legacy) if isinstance(legacy, dict) \
            else deepcopy(self.defaults)
        legacy_user_dir = migrated.get('user_dir') if isinstance(legacy, dict) \
            else root_path
        if not isinstance(legacy_user_dir, str) or not legacy_user_dir:
            legacy_user_dir = root_path
        migration_ready = True
        if os.path.abspath(legacy_user_dir) == os.path.abspath(root_path):
            migrated['user_dir'] = self.default_user_dir
            try:
                os.makedirs(self.default_user_dir, exist_ok=True)
                for filename in legacy_files:
                    source = os.path.join(root_path, filename)
                    destination = os.path.join(self.default_user_dir, filename)
                    if not os.path.exists(source):
                        raise OSError('legacy learning file disappeared: {}'.format(
                            filename))
                    source_data = None
                    try:
                        with open(source, 'r', encoding='utf-8') as file:
                            source_data = json.load(file)
                    except (OSError, ValueError, TypeError, RecursionError):
                        pass
                    if self._is_unsupported_legacy_learning_file(
                            destination, filename):
                        logger.warning('Preserving unsupported local learning file: %s',
                                       destination)
                        continue
                    if not self._is_complete_legacy_learning_file(
                            destination, filename, source_data):
                        descriptor = None
                        temporary = None
                        try:
                            descriptor, temporary = tempfile.mkstemp(
                                prefix='.' + filename + '.',
                                dir=self.default_user_dir)
                            os.close(descriptor)
                            descriptor = None
                            shutil.copy2(source, temporary)
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
            except OSError as error:
                migration_ready = False
                logger.warning('Could not copy legacy local learning data: %s',
                               error)
        if migration_ready:
            self._save(self.base_config_abs_path, migrated)

    def load(self, path):
        # type: (_SafeConfig, str) -> Config
        config = self._load(path)
        if config is None:      # Loading failed
            # Nothing modifies it, but may as well explicitly avoid mutation
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
