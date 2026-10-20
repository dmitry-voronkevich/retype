from retype.controllers.library import _save_position_key


def test_save_position_key_rejects_nonfinite_progress():
    assert _save_position_key({
        'progress': float('nan'), 'chapter_pos': 1, 'persistent_pos': 1}) is None


def test_save_position_key_rejects_out_of_range_progress():
    assert _save_position_key({
        'progress': 101, 'chapter_pos': 1, 'persistent_pos': 1}) is None
