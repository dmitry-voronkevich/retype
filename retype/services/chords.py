"""Parse CharaChorder backups into word and device-order chord hints.

The word-order notation is ordered by the first appearance of each key's
character in the produced word.  The device-order notation uses the selected
CharaChorder layout to order mapped keys from the physical left to right;
unknown and action keys are kept at the end in their original order.

Only real, single-word chords are kept: the produced phrase must be a single
word (no whitespace) and the chord must use at least two keys.
"""
import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# What counts as a "word" for chord lookup, in both the hint bar and the
# inline highlighting. Kept here so the two stay in lockstep.
WORD_RE = re.compile(r"[A-Za-z0-9']+")

# CharaChorder action codes that can appear in a chord's output but are not
# printable characters (arrow keys). They must not be treated as text.
_ARROW_CODES = (335, 336, 337, 338)

# Friendly names for the non-printable action/modifier keys that can be part of
# a chord's input, so a chord like "will" reads as "w+i+l+DUP" rather than
# "w+i+l+536". Names mirror ChordMentor's conversion contract while keeping
# retype's existing, user-facing DUP spelling.
_CODE_NAME = {
    32: "Space", 127: "Del", 298: "Bksp", 296: "Enter", 299: "Tab",
    297: "Esc", 335: "Right", 336: "Left", 337: "Down", 338: "Up",
    512: "Ctrl-L", 513: "Shift-L", 514: "Alt-L", 515: "Meta-L",
    516: "Ctrl-R", 517: "Shift-R", 518: "Alt-R", 519: "Meta-R",
    544: "Space-R", 536: "DUP", 550: "Num-L", 551: "Num-R",
    552: "Fn-L", 553: "Fn-R", 562: "LClick", 563: "RClick",
}

# CharaChorder switch centers in true geographic left-to-right order.  The
# layout record maps characters to these switch centers; this order is not the
# order in which the layout record happens to list its switches.
_LEFT_TO_RIGHT_SWITCHES = (
    606, 608, 607, 605, 604, 603, 602, 601, 600,
    609, 610, 611, 612, 613, 614, 616, 617, 615,
)


def _is_printable_ascii(code):
    # type: (object) -> bool
    return isinstance(code, int) and 32 <= code <= 126


def _token(code):
    # type: (int) -> str
    """Render one input key, preserving action tokens and visible symbols."""
    if 33 <= code <= 126:  # visible printable ASCII (space handled by name)
        return chr(code)
    return _CODE_NAME.get(code, "[{}]".format(code))


def _decode_output(codes):
    # type: (list) -> str
    """Turn a chord's output codes into the string it produces."""
    return "".join(
        chr(c) for c in codes
        if _is_printable_ascii(c) and c not in _ARROW_CODES)


@dataclass(frozen=True)
class ChordLayout:
    """The authoritative character-to-switch data from one backup layout.

    ``char_to_switch`` deliberately stores lowercase character keys.  This is
    the same normalization used by ChordMentor and lets physical conversion
    work for either case in a chord's raw input codes without changing the
    displayed input token.
    """

    char_to_switch: tuple[tuple[str, int], ...]
    switch_order: tuple[int, ...]

    def switch_for(self, code):
        # type: (int) -> int | None
        if not _is_printable_ascii(code):
            return None
        key = chr(code).lower()
        for mapped_key, switch in self.char_to_switch:
            if mapped_key == key:
                return switch
        return None


def parse_layout(layout):
    # type: (object) -> ChordLayout | None
    """Parse a CharaChorder layout record without any Qt or file I/O.

    CharaChorder backups store the base layout as the first list in the
    layout record.  A layout starts a character mapping at each switch-center
    code (600--617).  Malformed or absent records produce ``None`` so callers
    never mistake raw chord input order for physical order.
    """
    if not isinstance(layout, list) or not layout:
        return None
    base = layout[0] if isinstance(layout[0], list) else layout
    if not isinstance(base, list):
        return None

    current_switch = None  # type: int | None
    switch_order = []  # type: list[int]
    seen_switches = set()
    mapped = []  # type: list[tuple[str, int]]
    mapped_keys = set()

    for code in base:
        if not isinstance(code, int):
            continue
        if 600 <= code <= 617:
            current_switch = code
            if code not in seen_switches:
                switch_order.append(code)
                seen_switches.add(code)
        elif current_switch is not None and _is_printable_ascii(code):
            # The first mapping wins, matching ChordMentor's stable layout
            # parsing when a backup contains a repeated character.
            key = chr(code).lower()
            if key not in mapped_keys:
                mapped.append((key, current_switch))
                mapped_keys.add(key)

    if not switch_order:
        return None
    return ChordLayout(tuple(mapped), tuple(switch_order))


def _input_codes(input_codes):
    # type: (object) -> list[int]
    """Keep raw positive input codes, including duplicate/action codes."""
    if not isinstance(input_codes, list):
        return []
    return [code for code in input_codes
            if isinstance(code, int) and code > 0]


def by_word_notation(input_codes, phrase):
    # type: (list, str) -> str
    """Order input keys by first appearance in ``phrase``.

    Mapped printable keys come first.  Unknown and action keys are last and
    retain their input order.  Python's stable sort also retains duplicate
    input codes, which is important for chords containing repeated keys.
    """
    phrase = phrase.lower()

    def sort_key(pair):
        # type: (tuple) -> tuple
        original_index, code = pair
        if _is_printable_ascii(code):
            idx = phrase.find(chr(code).lower())
            if idx >= 0:
                return (0, idx, original_index)
        return (1, original_index)

    ordered = sorted(enumerate(input_codes), key=sort_key)
    return "+".join(_token(code) for _, code in ordered)


def by_device_notation(input_codes, layout):
    # type: (list, ChordLayout | object) -> str | None
    """Order input keys in physical left-to-right device layout order.

    This is the pure equivalent of ChordMentor's ``by_lr`` conversion.  The
    layout is required: returning ``None`` is intentional when no layout was
    selected, because raw input order is not physical order.  Mapped keys are
    sorted by geographic switch order; unknown/action keys come last in their
    original order.  No code is deduplicated.
    """
    if not isinstance(layout, ChordLayout):
        layout = parse_layout(layout)
    if layout is None:
        return None

    switch_indices = {
        switch: index for index, switch in enumerate(_LEFT_TO_RIGHT_SWITCHES)
    }

    def sort_key(pair):
        # type: (tuple) -> tuple
        original_index, code = pair
        switch = layout.switch_for(code)
        if switch in switch_indices:
            return (0, switch_indices[switch], original_index)
        return (1, original_index)

    ordered = sorted(enumerate(_input_codes(input_codes)), key=sort_key)
    return "+".join(_token(code) for _, code in ordered)


class Chord(str):
    """A word-order string carrying the raw and physical chord metadata.

    Subclassing ``str`` preserves the existing chord-map consumer contract for
    callers that only need the word-order notation, while the hint UI can use
    ``input_codes``, ``device_order`` and ``layout`` authoritatively.
    """

    def __new__(cls, word_order, input_codes, device_order=None, layout=None):
        # type: (type[Chord], str, list[int] | tuple[int, ...], str | None,
        #         ChordLayout | None) -> Chord
        obj = str.__new__(cls, word_order)
        obj.word_order = word_order
        obj.input_codes = tuple(input_codes)
        obj.device_order = device_order
        obj.layout = layout
        return obj

    @property
    def raw_input_codes(self):
        # type: (Chord) -> tuple[int, ...]
        """Compatibility name for consumers that need the unflattened input."""
        return self.input_codes


def _backup_chords_and_layout(data):
    # type: (object) -> tuple[list, ChordLayout | None]
    """Select the chord and layout records from a backup."""
    chords = None
    layout = None
    if isinstance(data, dict) and "history" in data:
        history = data["history"]
        if history and isinstance(history[0], list):
            for item in history[0]:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "chords":
                    chords = item.get("chords")
                elif item.get("type") == "layout":
                    layout = item.get("layout")
    if chords is None and isinstance(data, dict):
        chords = data.get("chords")
    if layout is None and isinstance(data, dict):
        layout = data.get("layout")
    return (chords or [], parse_layout(layout))


def _iter_raw_chords(data):
    # type: (object) -> list
    """Locate the list of ``[input, output]`` chord pairs in a backup dict."""
    return _backup_chords_and_layout(data)[0]


def load_chords(json_path):
    # type: (str) -> dict[str, Chord]
    """Load a CharaChorder backup into ``{word_lower: Chord}``.

    Entries are limited to single-word phrases produced by two or more keys.
    When the same word has several chords, the one with the fewest input keys
    wins, retaining the existing first-entry tie behavior.
    """
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("Could not read chords file '%s': %s", json_path, e)
        return {}

    raw_chords, layout = _backup_chords_and_layout(data)
    best = {}  # type: dict[str, tuple[list[int], str]]
    for entry in raw_chords:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        input_codes, output_codes = entry
        if not (isinstance(input_codes, list)
                and isinstance(output_codes, list)):
            continue

        phrase = _decode_output(output_codes).strip()
        if not phrase or " " in phrase:
            continue  # empty or multi-word phrase

        keys = _input_codes(input_codes)
        if len(keys) < 2:
            continue  # not a real chord

        word = phrase.lower()
        if word not in best or len(keys) < len(best[word][0]):
            best[word] = (keys, phrase)

    chords = {}
    for word, (keys, phrase) in best.items():
        word_order = by_word_notation(keys, phrase)
        device_order = by_device_notation(keys, layout)
        chords[word] = Chord(word_order, keys, device_order, layout)
    return chords


def escape_chord_text(value):
    # type: (object) -> str
    """Escape a rendered chord fragment for safe insertion into rich text."""
    return html.escape(str(value), quote=True)


def chordable_spans(text, chords):
    # type: (str, dict[str, object]) -> list[tuple[int, int]]
    """Return ``(start, end)`` spans of every known chord word in ``text``.

    Positions index ``text`` directly, so they can be used against a
    QTextDocument whose plain text equals ``text``.
    """
    if not chords:
        return []
    return [(m.start(), m.end()) for m in WORD_RE.finditer(text)
            if m.group().lower() in chords]


if TYPE_CHECKING:
    from typing import Dict  # noqa: F401
