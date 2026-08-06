"""Parse a CharaChorder device backup JSON into a word -> chord map.

The chord notation used here is "word order": the input keys of a chord are
 ordered by where the corresponding letter first appears in the produced word.
 For example a chord stored as ``l, h, o`` that produces "hello" is shown as
 ``h+l+o``, which is easier to memorise than the device's raw key order.

Only real, single-word chords are kept: the produced phrase must be a single
 word (no whitespace) and the chord must use at least two keys.
"""
import re
import json
import logging
from pathlib import Path

from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# What counts as a "word" for chord lookup, in both the hint bar and the
#  inline highlighting. Kept here so the two stay in lockstep.
WORD_RE = re.compile(r"[A-Za-z0-9']+")

# CharaChorder action codes that can appear in a chord's output but are not
#  printable characters (arrow keys). They must not be treated as text.
_ARROW_CODES = (335, 336, 337, 338)

# Friendly names for the non-printable action/modifier keys that can be part of
#  a chord's input, so a chord like "will" reads as "w+i+l+DUP" rather than
#  "w+i+l+536". DUP duplicates the previous character (e.g. the second l in
#  "will"). Names mirror charachorder_word_freq.py.
_CODE_NAME = {
    32: "Space", 127: "Del", 298: "Bksp", 296: "Enter", 299: "Tab",
    297: "Esc", 335: "Right", 336: "Left", 337: "Down", 338: "Up",
    512: "Ctrl-L", 513: "Shift-L", 514: "Alt-L", 515: "Meta-L",
    516: "Ctrl-R", 517: "Shift-R", 518: "Alt-R", 519: "Meta-R",
    544: "Space-R", 536: "DUP", 550: "Num-L", 551: "Num-R",
    552: "Fn-L", 553: "Fn-R",
}


def _is_printable_ascii(code):
    # type: (object) -> bool
    return isinstance(code, int) and 32 <= code <= 126


def _token(code):
    # type: (int) -> str
    """Render a single input key: its character if it is a visible printable,
     else the action key's name (Space, DUP, ...), falling back to a bracketed
     code for unknown keys."""
    if 33 <= code <= 126:  # visible printable ASCII (space handled by name)
        return chr(code)
    return _CODE_NAME.get(code, "[{}]".format(code))


def _decode_output(codes):
    # type: (list) -> str
    """Turn a chord's output codes into the string it produces."""
    return "".join(
        chr(c) for c in codes
        if _is_printable_ascii(c) and c not in _ARROW_CODES)


def by_word_notation(input_codes, phrase):
    # type: (list, str) -> str
    """Order ``input_codes`` by first appearance of their letter in ``phrase``.

    Keys whose character is not in the phrase are kept in their original order
     after the ones that are, so nothing is silently dropped.
    """
    phrase = phrase.lower()

    def sort_key(pair):
        # type: (tuple) -> tuple
        original_index, code = pair
        if _is_printable_ascii(code):
            idx = phrase.find(chr(code).lower())
            if idx >= 0:
                return (0, idx)
        return (1, original_index)

    ordered = sorted(enumerate(input_codes), key=sort_key)
    return "+".join(_token(code) for _, code in ordered)


def _iter_raw_chords(data):
    # type: (object) -> list
    """Locate the list of ``[input, output]`` chord pairs in a backup dict."""
    if isinstance(data, dict) and "history" in data:
        history = data["history"]
        if history and isinstance(history[0], list):
            for item in history[0]:
                if isinstance(item, dict) and item.get("type") == "chords":
                    return item.get("chords") or []
    if isinstance(data, dict):
        return data.get("chords") or []
    return []


def load_chords(json_path):
    # type: (str) -> dict[str, str]
    """Load a CharaChorder backup and return ``{word_lower: chord_notation}``.

    Entries are limited to single-word phrases produced by two or more keys.
     When the same word has several chords, the one with the fewest keys wins.
    """
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("Could not read chords file '%s': %s", json_path, e)
        return {}

    best = {}  # type: dict[str, list]
    for entry in _iter_raw_chords(data):
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        input_codes, output_codes = entry
        if not (isinstance(input_codes, list)
                and isinstance(output_codes, list)):
            continue

        phrase = _decode_output(output_codes).strip()
        if not phrase or " " in phrase:
            continue  # empty or multi-word phrase

        keys = [c for c in input_codes if isinstance(c, int) and c > 0]
        if len(keys) < 2:
            continue  # not a real chord

        word = phrase.lower()
        if word not in best or len(keys) < len(best[word]):
            best[word] = keys

    return {word: by_word_notation(keys, word)
            for word, keys in best.items()}


def chordable_spans(text, chords):
    # type: (str, dict[str, str]) -> list[tuple[int, int]]
    """Return ``(start, end)`` character spans of every word in ``text`` that
     has a chord in ``chords``. Positions index ``text`` directly, so they can
     be used against a QTextDocument whose plain text equals ``text``."""
    if not chords:
        return []
    return [(m.start(), m.end()) for m in WORD_RE.finditer(text)
            if m.group().lower() in chords]


if TYPE_CHECKING:
    from typing import Dict  # noqa: F401
