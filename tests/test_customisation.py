import json
from copy import deepcopy

from qt import QObject, Qt, QPushButton, pyqtSignal

from retype.controllers.safe_config import SafeConfig
from retype.constants import default_config
from retype.services.chord_detection import ValidatedChord
from retype.services.chord_lessons import ChordMasteryProgress, ChordMasteryStorage
from retype.services.chord_mastery import MIN_SUCCESSFUL_USES_FOR_MASTERY
from retype.ui import CustomisationDialog


class FakeWindow(QObject):
    closing = pyqtSignal()


def _setup(chords=None, progress=None):
    dialog = CustomisationDialog(
        default_config, FakeWindow(), *[None]*3,
        getLoadedChords=lambda: chords or {}, chordProgress=progress)
    return dialog


def _row_keys(section):
    return [section.table.item(row, 1).text() for row in range(section.table.rowCount())]


def _check_state(section, row):
    return section.table.item(row, 0).checkState() == Qt.CheckState.Checked


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

    def test_mastery_section_renders_progress_counts_and_sort_state(self, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        for _ in range(MIN_SUCCESSFUL_USES_FOR_MASTERY):
            progress.record(ValidatedChord(
                'mastered', 'mastered', 'mastered', 0, 0, 20.0, 10.0, False))
        for _ in range(2):
            progress.record(ValidatedChord(
                'started', 'started', 'started', 0, 0, 20.0, 10.0, False))
        dialog = _setup({'mastered': 'm', 'started': 's', 'other': 'o'}, progress=progress)
        section = dialog.chord_mastery

        assert section.table.accessibleName() == 'Chord mastery table'
        assert section.summary.label.text() == (
            '1 teaching target in progress, 1 mastered hint-eligible chord, '
            'and 1 other chord.')
        assert section.table.horizontalHeaderItem(0).text() == 'Select'
        assert section.table.horizontalHeaderItem(3).text() == 'Status'
        assert section.table.horizontalHeader().sortIndicatorSection() == 3
        assert section.table.horizontalHeader().sortIndicatorOrder() == \
            Qt.SortOrder.AscendingOrder
        assert _row_keys(section) == ['mastered', 'started', 'other']
        assert section.bulk_actions.isHidden()

    def test_status_sorting_is_stable_for_ties_and_keyboard_accessible(self, qtbot, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        progress.record(ValidatedChord(
            'gamma', 'gamma', 'gamma', 0, 0, 20.0, 10.0, False))
        for _ in range(10):
            progress.record(ValidatedChord(
                'zeta', 'zeta', 'zeta', 0, 0, 20.0, 10.0, False))
        dialog = _setup({'alpha': 'a', 'beta': 'b', 'gamma': 'g', 'zeta': 'z'},
                        progress=progress)
        section = dialog.chord_mastery

        header = section.table.horizontalHeader()
        assert _row_keys(section) == ['zeta', 'gamma', 'alpha', 'beta']

        header.setCurrentIndex(header.model().index(0, 3))
        header.setFocus()
        qtbot.keyClick(header, Qt.Key.Key_Space)

        assert header.sortIndicatorSection() == 3
        assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert _row_keys(section) == ['alpha', 'beta', 'gamma', 'zeta']

        header.setCurrentIndex(header.model().index(0, 2))
        qtbot.keyClick(header, Qt.Key.Key_Space)

        assert header.sortIndicatorSection() == 2
        assert _row_keys(section) == ['alpha', 'beta', 'gamma', 'zeta']

        header.setCurrentIndex(header.model().index(0, 0))
        header.setFocus()
        qtbot.keyClick(header, Qt.Key.Key_Space)

        assert header.sortIndicatorSection() == 2
        assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        assert _row_keys(section) == ['alpha', 'beta', 'gamma', 'zeta']

    def test_selection_controls_bulk_actions_and_keyboard_navigation(self, qtbot, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup({'mastered': 'm', 'started': 's', 'other': 'o'}, progress=progress)
        dialog.show()
        qtbot.waitUntil(lambda: dialog.isVisible())
        section = dialog.chord_mastery

        assert section.bulk_actions.isHidden()
        qtbot.mouseClick(section.select_all_btn, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: not section.bulk_actions.isHidden())
        assert _check_state(section, 0)
        assert _check_state(section, 1)
        assert _check_state(section, 2)

        qtbot.mouseClick(section.select_none_btn, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: section.bulk_actions.isHidden())
        assert not _check_state(section, 0)
        assert not _check_state(section, 1)
        assert not _check_state(section, 2)

        section.table.setCurrentCell(0, 0)
        section.table.setFocus()
        qtbot.keyClick(section.table, Qt.Key.Key_Space)
        assert _check_state(section, 0)
        section.table.setCurrentCell(1, 0)
        qtbot.keyClick(section.table, Qt.Key.Key_Space)
        assert _check_state(section, 1)
        assert not section.bulk_actions.isHidden()

    def test_bulk_actions_apply_to_selected_rows_support_undo_and_persistence(self, qtbot, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        dialog = _setup({'mastered': 'm', 'started': 's', 'other': 'o'}, progress=progress)
        section = dialog.chord_mastery

        qtbot.mouseClick(section.select_all_btn, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(section.bulk_mastered_btn, Qt.MouseButton.LeftButton)
        assert section.isDirty()
        assert section.overrides() == {
            'mastered': True, 'started': True, 'other': True}
        assert 'Marked 3 selected chords as mastered.' in \
            section.confirmation_label.label.text()

        qtbot.mouseClick(section.undo_btn, Qt.MouseButton.LeftButton)
        assert section.overrides() == {}
        assert not section.confirmation.isVisible()

        qtbot.mouseClick(section.select_all_btn, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(section.bulk_unmastered_btn, Qt.MouseButton.LeftButton)
        assert section.overrides() == {
            'mastered': False, 'started': False, 'other': False}
        assert 'unmastered' in section.confirmation_label.label.text()
        assert progress.set_manual_overrides(section.overrides()) is True

        restored = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        assert restored.manual_overrides() == {
            'mastered': False, 'started': False, 'other': False}
        assert restored.progress_for('mastered').is_mastered is False

    def test_partial_and_failed_save_feedback_is_clear(self, qtbot, tmp_path):
        progress = ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))
        for _ in range(MIN_SUCCESSFUL_USES_FOR_MASTERY):
            progress.record(ValidatedChord(
                'mastered', 'mastered', 'mastered', 0, 0, 20.0, 10.0, False))
        dialog = _setup({'mastered': 'm', 'started': 's'}, progress=progress)
        section = dialog.chord_mastery

        qtbot.mouseClick(section.select_all_btn, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(section.bulk_mastered_btn, Qt.MouseButton.LeftButton)
        assert 'already matched' in section.confirmation_label.label.text()

        section.setSaveFailed()
        assert section.saveFailed() is True
        assert 'Unable to save mastery changes' in section.confirmation_label.label.text()

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
        assert section.summary.label.text() == (
            '0 teaching targets in progress, 0 mastered hint-eligible chords, '
            'and 1 other chord.')

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
