import json
from copy import deepcopy

from qt import pyqtSignal, QObject, Qt, QPushButton

from retype.controllers.safe_config import SafeConfig
from retype.constants import default_config
from retype.services.chord_lessons import ChordMasteryProgress, ChordMasteryStorage
from retype.ui import CustomisationDialog


class FakeWindow(QObject):
    closing = pyqtSignal()


def _setup(chords=None, progress=None):
    dialog = CustomisationDialog(
        default_config, FakeWindow(), *[None]*3,
        getLoadedChords=lambda: chords or {}, chordProgress=progress)
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
        assert dialog.selectors['adaptive_chord_lesson_limit'].value() == 5
        assert dialog.selectors['load_chords_on_startup'].isChecked()

        dialog.selectors['load_chords_on_startup'].set_(False)
        assert dialog.config_edited['load_chords_on_startup'] is False

    def test_lesson_limit_and_toggle_are_editable(self):
        dialog = _setup()

        dialog.selectors['adaptive_chord_lesson_limit'].set_(7)
        assert dialog.config_edited['adaptive_chord_lesson_limit'] == 7

        dialog.selectors['adaptive_chord_lessons'].set_(False)
        assert dialog.config_edited['adaptive_chord_lessons'] is False
        assert dialog.selectors['adaptive_chord_lesson_limit'].value() == 7

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

    def test_mastery_section_renders_and_is_accessible(self, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup({'the': 't+h+e'}, progress=progress)
        section = dialog.chord_mastery

        assert section.table.accessibleName() == 'Chord mastery table'
        assert section.summary.label.text().startswith('First use:')
        assert section.table.rowCount() == 1
        assert section.table.horizontalHeaderItem(0).text() == 'Chord'
        assert section.table.horizontalHeaderItem(3).text() == 'Actions'
        action_widget = section.table.cellWidget(0, 3)
        buttons = action_widget.findChildren(QPushButton)
        assert {button.text() for button in buttons} == {
            'Mastered', 'Unmastered'}
        assert any(button.accessibleName() == 'Mark the mastered'
                   for button in buttons)

    def test_manual_override_strip_supports_undo_and_reset(self, qtbot, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup({'the': 't+h+e'}, progress=progress)
        dialog.show()
        qtbot.waitUntil(lambda: dialog.isVisible())
        section = dialog.chord_mastery
        action_widget = section.table.cellWidget(0, 3)
        mastered_button = next(
            button for button in action_widget.findChildren(QPushButton)
            if button.text() == 'Mastered')
        unmastered_button = next(
            button for button in action_widget.findChildren(QPushButton)
            if button.text() == 'Unmastered')

        qtbot.mouseClick(mastered_button, Qt.MouseButton.LeftButton)
        assert section.isDirty()
        assert dialog.revert_btn.isEnabled()
        assert section.confirmation_label.label.text() == \
            "Marked 'the' as mastered."
        assert section.overrides()['the'] is True

        qtbot.mouseClick(section.undo_btn, Qt.MouseButton.LeftButton)
        assert section.overrides() == {}
        assert not section.confirmation.isVisible()

        qtbot.mouseClick(unmastered_button, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(section.restore_btn, Qt.MouseButton.LeftButton)
        assert section.overrides() == {}
        assert 'Restored measured state' in section.confirmation_label.label.text()

    def test_empty_and_malformed_states_are_descriptive(self, tmp_path):
        path = tmp_path / 'chord-mastery.json'
        path.write_text(json.dumps({
            'version': 2,
            'progress': {'broken': 'not-an-int'},
            'manual_overrides': {'broken': True},
        }))
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup({'the': 't+h+e'}, progress=progress)
        section = dialog.chord_mastery

        assert 'Malformed saved progress' in section.warning.label.text()
        assert section.summary.label.text().startswith('1 teaching target')

    def test_malformed_progress_is_shown_without_loaded_chords(self, tmp_path):
        path = tmp_path / 'chord-mastery.json'
        path.write_text(json.dumps({
            'version': 2,
            'progress': {'broken': 'not-an-int'},
        }))
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup(progress=progress)
        section = dialog.chord_mastery

        assert section.summary.label.text() == 'No loaded chords yet.'
        assert 'Malformed saved progress' in section.warning.label.text()
        assert '1 entry' in section.warning.label.text()
