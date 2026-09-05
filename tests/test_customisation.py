import json
from copy import deepcopy

from qt import pyqtSignal, QObject

from retype.controllers.safe_config import SafeConfig
from retype.ui import CustomisationDialog
from retype.constants import default_config


class FakeWindow(QObject):
    closing = pyqtSignal()


def _setup():
    dialog = CustomisationDialog(default_config, FakeWindow(), *[None]*3)
    return dialog


class TestCustomisation:
    def test_chord_json_setting_is_removed(self, tmp_path):
        dialog = _setup()
        assert 'chords_path' not in default_config
        assert 'chords_path' not in dialog.selectors

        legacy = deepcopy(default_config)
        legacy['user_dir'] = str(tmp_path)
        legacy['chords_path'] = '/old/backup.json'
        (tmp_path / 'config.json').write_text(json.dumps(legacy))
        assert 'chords_path' not in SafeConfig(str(tmp_path)).raw

    def test_auto_newline_default_value(self):
        dialog = _setup()
        key = 'auto_newline'
        assert (dialog.selectors[key].isChecked() == default_config[key])

    def test_chord_settings_defaults_and_persisted_opt_out(self):
        dialog = _setup()
        assert dialog.selectors['adaptive_chord_lessons'].isChecked()
        assert dialog.selectors['load_chords_on_startup'].isChecked()

        dialog.selectors['load_chords_on_startup'].set_(False)
        assert dialog.config_edited['load_chords_on_startup'] is False

    def test_missing_startup_chord_setting_defaults_to_enabled(self, tmp_path):
        legacy = deepcopy(default_config)
        legacy['user_dir'] = str(tmp_path)
        legacy.pop('load_chords_on_startup')
        (tmp_path / 'config.json').write_text(json.dumps(legacy))

        assert SafeConfig(str(tmp_path))['load_chords_on_startup'] is True

    def test_auto_newline_check_uncheck(self):
        dialog = CustomisationDialog(default_config, FakeWindow(),
                                     None, None, None)
        key = 'auto_newline'

        dialog.selectors[key].set_(True)
        assert dialog.selectors[key].isChecked() is True
        assert dialog.config_edited[key] is True

        dialog.selectors[key].set_(False)
        assert dialog.selectors[key].isChecked() is False
        assert dialog.config_edited[key] is False
