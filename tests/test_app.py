import sys

from retype.app import _parseArgs


def test_chord_json_command_line_interface_is_removed(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['retype', '--help'])
    try:
        _parseArgs('retype')
    except SystemExit:
        pass
    assert 'chord' not in capsys.readouterr().out.lower()
    # argparse writes help to its configured stdout; parsing the old switch
    # must now fail rather than accepting a file path.
    monkeypatch.setattr(sys, 'argv', ['retype', '--chords', 'backup.json'])
    try:
        _parseArgs('retype')
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError('obsolete --chords argument was accepted')
