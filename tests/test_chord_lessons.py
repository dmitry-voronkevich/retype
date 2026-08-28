import json

from retype.services.chord_detection import LikelyChordBurst, ValidatedChord
from retype.services.chord_lessons import (
    AdaptiveChordExposure, ChordMasteryProgress, ChordMasteryStorage,
    MASTERY_PROGRESS_FILENAME,
)
from retype.services.chord_mastery import MIN_SUCCESSFUL_USES_FOR_MASTERY


def _result(word='the'):
    return ValidatedChord(word, word, word, 0, 0, 20.0, 10.0, False)


def _progress(tmp_path, contents=None):
    if contents is not None:
        (tmp_path / MASTERY_PROGRESS_FILENAME).write_text(
            json.dumps(contents), encoding='utf-8')
    return ChordMasteryProgress(ChordMasteryStorage(str(tmp_path)))


def test_progress_persists_only_validated_successes(tmp_path):
    progress = _progress(tmp_path)

    assert progress.record(LikelyChordBurst('the', 20.0, True)) is None
    assert progress.record(_result()).successful_uses == 1

    restored = _progress(tmp_path)
    assert restored.progress_for('the').successful_uses == 1


def test_missing_data_starts_empty_and_partial_data_is_preserved(tmp_path):
    progress = _progress(tmp_path)
    assert progress.progress_for('the').successful_uses == 0

    partial = {
        'version': 1,
        'progress': {'the': 3, 'broken': 'unknown future data'},
    }
    progress = _progress(tmp_path, partial)
    assert progress.progress_for('the').successful_uses == 3
    progress.record(_result('and'))

    saved = json.loads((tmp_path / MASTERY_PROGRESS_FILENAME).read_text())
    assert saved['progress'] == {
        'the': 3, 'and': 1, 'broken': 'unknown future data'}


def test_corrupt_or_unknown_storage_is_not_overwritten(tmp_path):
    path = tmp_path / MASTERY_PROGRESS_FILENAME
    path.write_text('{not json', encoding='utf-8')
    progress = _progress(tmp_path)
    progress.record(_result())
    assert path.read_text(encoding='utf-8') == '{not json'

    unknown = {'version': 99, 'progress': {'the': 4}}
    path.write_text(json.dumps(unknown), encoding='utf-8')
    progress = _progress(tmp_path)
    progress.record(_result())
    assert json.loads(path.read_text(encoding='utf-8')) == unknown


def test_default_lesson_limits_and_prioritises_started_targets(tmp_path):
    progress = _progress(tmp_path, {'version': 1, 'progress': {
        'started': 2, 'anotherstarted': 1,
    }})
    lesson = AdaptiveChordExposure(progress).select(
        {'newa': 'a', 'newb': 'b', 'newc': 'c', 'newd': 'd',
         'newe': 'e', 'newf': 'f', 'started': 's',
         'anotherstarted': 'z'},
        'newf newf newe newe newe newd started')

    assert lesson.teaching_keys[:2] == ('started', 'anotherstarted')
    assert lesson.teaching_keys == ('started', 'anotherstarted', 'newe',
                                    'newf', 'newd')
    assert len(lesson.teaching_keys) == 5


def test_new_targets_are_ordered_by_chapter_word_frequency(tmp_path):
    lesson = AdaptiveChordExposure(_progress(tmp_path)).select(
        {'rare': 'r', 'common': 'c', 'middle': 'm'},
        'rare common common middle middle middle')

    assert lesson.teaching_keys == ('middle', 'common', 'rare')


def test_completed_chords_remain_hint_eligible_but_not_targets(tmp_path):
    progress = _progress(tmp_path, {'version': 1, 'progress': {
        'known': MIN_SUCCESSFUL_USES_FOR_MASTERY,
    }})
    lesson = AdaptiveChordExposure(progress).select(
        {'known': 'k', 'new': 'n'}, 'known new')

    assert lesson.teaching_keys == ('new',)
    assert lesson.mastered_hint_keys == ('known',)
    assert lesson.hint_keys == ('new', 'known')


def test_full_list_opt_out_and_empty_dictionary(tmp_path):
    selector = AdaptiveChordExposure(_progress(tmp_path))
    chords = {'one': '1', 'two': '2', 'three': '3'}

    assert selector.select(chords, full_chord_list=True).hint_keys == \
        ('one', 'three', 'two')
    assert selector.select({}, 'ordinary typing').hint_keys == ()
    assert selector.select({}, 'ordinary typing').teaching_keys == ()
