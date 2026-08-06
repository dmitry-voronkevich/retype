import json

from retype.services.chords import (
    load_chords, by_word_notation, chordable_spans)


def _ascii(s):
    """Codes for a string as the device stores output (printable ASCII)."""
    return [ord(c) for c in s]


def _write(tmp_path, chords):
    """Build a minimal CharaChorder backup with the given [input, output] chords."""
    data = {
        "charaVersion": 1,
        "type": "backup",
        "history": [[
            {"type": "chords", "chords": chords},
        ]],
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
