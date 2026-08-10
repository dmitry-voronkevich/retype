import json

from retype.services.chords import (
    by_device_notation, by_word_notation, chordable_spans, load_chords,
    parse_layout)


def _ascii(s):
    """Codes for a string as the device stores output (printable ASCII)."""
    return [ord(c) for c in s]


def _layout():
    """A layout whose geographic order for hello is o, e, h, l."""
    return [[
        606, ord('o'), 608, ord('e'), 607, ord('h'), 605, ord('l'),
    ]]


def _write(tmp_path, chords, layout=None):
    """Build a minimal CharaChorder backup with chord/layout records."""
    records = [{"type": "chords", "chords": chords}]
    if layout is not None:
        records.append({"type": "layout", "layout": layout})
    data = {
        "charaVersion": 1,
        "type": "backup",
        "history": [records],
    }
    p = tmp_path / "backup.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_by_word_notation_orders_keys_by_first_appearance():
    # User's canonical example: chord stored as l,h,o for the word "hello"
    #  should read as "h+l+o" (order of first appearance in the word).
    assert by_word_notation([ord('l'), ord('h'), ord('o')], "hello") == "h+l+o"


def test_load_chords_uses_word_order_and_lowercases_key(tmp_path):
    path = _write(tmp_path, [
        [_ascii("lho"), _ascii("hello")],
    ])
    assert load_chords(path) == {"hello": "h+l+o"}


def test_hello_device_order_uses_selected_layout(tmp_path):
    path = _write(tmp_path, [
        [_ascii("lhoe"), _ascii("hello")],
    ], _layout())
    chord = load_chords(path)["hello"]
    assert chord.word_order == "h+e+l+o"
    assert chord.device_order == "o+e+h+l"
    assert chord.input_codes == tuple(_ascii("lhoe"))
    assert chord.layout is not None


def test_missing_layout_hides_physical_notation(tmp_path):
    path = _write(tmp_path, [
        [_ascii("lhoe"), _ascii("hello")],
    ])
    assert load_chords(path)["hello"].device_order is None


def test_layout_mapping_is_lowercase_but_input_tokens_keep_case():
    layout = parse_layout([[606, ord('O'), 608, ord('E'),
                            607, ord('H'), 605, ord('L')]])
    assert layout is not None
    assert dict(layout.char_to_switch) == {
        "o": 606, "e": 608, "h": 607, "l": 605,
    }
    assert by_device_notation(
        [ord('L'), ord('H'), ord('O'), ord('E')], layout) == "O+E+H+L"


def test_device_order_keeps_unknown_actions_and_duplicates_last():
    layout = parse_layout(_layout())
    assert layout is not None
    assert by_device_notation(
        [ord('l'), DUP, 999, ord('l'), ord('o')], layout) \
        == "o+l+l+DUP+[999]"


def test_load_chords_preserves_duplicate_input_codes(tmp_path):
    path = _write(tmp_path, [
        [[ord('l'), ord('l'), ord('h'), ord('e'), DUP, DUP],
         _ascii("hello")],
    ])
    assert load_chords(path)["hello"] == "h+e+l+l+DUP+DUP"
    assert load_chords(path)["hello"].input_codes == (
        ord('l'), ord('l'), ord('h'), ord('e'), DUP, DUP)


def test_load_chords_skips_single_key_chords(tmp_path):
    path = _write(tmp_path, [
        [_ascii("a"), _ascii("a")],          # single key -> not a real chord
        [_ascii("nd"), _ascii("and")],       # two keys -> kept
    ])
    assert load_chords(path) == {"and": "n+d"}


def test_load_chords_skips_multi_word_phrases(tmp_path):
    path = _write(tmp_path, [
        [_ascii("ty"), _ascii("thank you")],  # phrase with a space -> skipped
        [_ascii("te"), _ascii("the")],
    ])
    assert load_chords(path) == {"the": "t+e"}


def test_load_chords_prefers_shortest_input_for_duplicate_word(tmp_path):
    path = _write(tmp_path, [
        [_ascii("thre"), _ascii("there")],
        [_ascii("the"), _ascii("there")],
    ])
    # Shorter (3-key) chord should win over the 4-key one.
    assert load_chords(path) == {"there": "t+h+e"}


DUP = 536  # CharaChorder "duplicate previous character" key


def test_by_word_notation_names_the_dup_action_key():
    # "will" is chorded as w, i, l, DUP (DUP doubles the l). The action key
    #  must show as its button name, not the raw code 536.
    assert by_word_notation([ord('i'), ord('l'), ord('w'), DUP], "will") \
        == "w+i+l+DUP"


def test_load_chords_renders_action_keys_by_name(tmp_path):
    path = _write(tmp_path, [
        [[ord('i'), ord('l'), ord('w'), DUP], _ascii("will")],
    ])
    assert load_chords(path) == {"will": "w+i+l+DUP"}


SPACE = 32  # CharaChorder Space key


def test_by_word_notation_names_the_space_key():
    # A "three" shortcut chorded as Space + 3 should read "Space+3", not " +3".
    assert by_word_notation([SPACE, ord('3')], "three") == "Space+3"


def test_load_chords_empty_when_no_chords_section(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"history": [[]]}), encoding="utf-8")
    assert load_chords(str(p)) == {}


def test_chordable_spans_returns_positions_of_known_words():
    chords = {"world": "w+o+r+l+d", "again": "a+g+n"}
    # "the world and again": world at [4,9), again at [14,19)
    assert chordable_spans("the world and again", chords) == \
        [(4, 9), (14, 19)]


def test_chordable_spans_is_case_insensitive():
    assert chordable_spans("World WORLD", {"world": "x"}) == \
        [(0, 5), (6, 11)]


def test_chordable_spans_empty_when_no_map():
    assert chordable_spans("hello world", {}) == []
