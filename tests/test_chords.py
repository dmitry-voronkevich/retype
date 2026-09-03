from retype.services.chords import (
    build_chords, by_device_notation, by_word_notation, chordable_spans,
    parse_layout)


def _ascii(value):
    return [ord(c) for c in value]


def _layout():
    return [[606, ord('o'), 608, ord('e'), 607, ord('h'), 605, ord('l')]]


def test_build_chords_uses_device_data_and_lowercases_keys():
    chords = build_chords([[_ascii('lho'), _ascii('hello')]], _layout())
    chord = chords['hello']
    assert chord == 'h+l+o'
    assert chord.device_order == 'o+h+l'


def test_build_chords_preserves_existing_filters_and_shortest_duplicate():
    chords = build_chords([
        [[ord('a')], _ascii('a')],
        [_ascii('ty'), _ascii('thank you')],
        [_ascii('thre'), _ascii('there')],
        [_ascii('the'), _ascii('there')],
        [[ord('i'), ord('l'), ord('w'), 536], _ascii('will')],
    ], _layout())
    assert set(chords) == {'there', 'will'}
    assert chords['there'] == 't+h+e'
    assert chords['will'] == 'w+i+l+DUP'


def test_layout_mapping_is_lowercase_but_tokens_keep_case():
    layout = parse_layout([[606, ord('O'), 608, ord('E'),
                            607, ord('H'), 605, ord('L')]])
    assert layout is not None
    assert by_device_notation(
        [ord('L'), ord('H'), ord('O'), ord('E')], layout) == 'O+E+H+L'


def test_device_order_keeps_unknown_actions_and_duplicates_last():
    layout = parse_layout(_layout())
    assert layout is not None
    assert by_device_notation([ord('l'), 536, 999, ord('l'), ord('o')], layout) \
        == 'o+l+l+DUP+[999]'


def test_word_notation_names_action_keys():
    assert by_word_notation([ord('i'), ord('l'), ord('w'), 536], 'will') \
        == 'w+i+l+DUP'


def test_chordable_spans_is_case_insensitive():
    assert chordable_spans('World WORLD', {'world': 'x'}) == [(0, 5), (6, 11)]
