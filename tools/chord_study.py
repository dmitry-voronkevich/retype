#!/usr/bin/env python3
"""Opt-in, foreground CharaChorder chord-versus-sequential study.

This is research instrumentation.  It does not access a device, install a
keyboard hook, or make a device-origin claim.  Capture starts only after the
participant presses ``Begin this trial`` and the capture widget has focus.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

STUDY_WORDS = ("the", "and", "for", "was", "without", "because", "with", "at")
DEFAULT_PAIRS = 3
DEFAULT_SEED = 20250308

# These names and the layout-order rules intentionally follow the user's
# ChordMentor reference implementation (charachorder_word_freq.py).
CODE_NAME = {
    0: "", 32: "Space", 127: "Del", 298: "Bksp", 296: "Enter",
    299: "Tab", 297: "Esc", 335: "→", 336: "←", 337: "↓", 338: "↑",
    512: "Ctrl-L", 513: "Shift-L", 514: "Alt-L", 515: "Meta-L",
    516: "Ctrl-R", 517: "Shift-R", 518: "Alt-R", 519: "Meta-R",
    544: "Space-R", 536: "Dup", 550: "Num-L", 551: "Num-R",
    552: "Fn-L", 553: "Fn-R", 562: "LClick", 563: "RClick",
    600: "LH-T3", 601: "LH-T2", 602: "LH-T1", 603: "LH-Id",
    604: "LH-M1", 605: "LH-R1", 606: "LH-Pk", 607: "LH-M2",
    608: "LH-R2", 609: "RH-T3", 610: "RH-T2", 611: "RH-T1",
    612: "RH-Id", 613: "RH-M1", 614: "RH-R1", 615: "RH-Pk",
    616: "RH-M2", 617: "RH-R2",
}
LEFT_TO_RIGHT = (606, 608, 607, 605, 604, 603, 602, 601, 600,
                 609, 610, 611, 612, 613, 614, 616, 617, 615)


def token(code: int) -> str:
    """Render a backup code as the reference tool does."""
    if 32 <= code <= 126:
        return chr(code)
    return CODE_NAME.get(code, f"[{code}]")


def decode_output(codes: Iterable[Any]) -> str:
    """Decode printable output codes, excluding navigation codes."""
    return "".join(
        chr(code) for code in codes
        if isinstance(code, int) and 32 <= code <= 126
        and code not in (335, 336, 337, 338)
    )


def _history_items(data: Any) -> Iterable[dict[str, Any]]:
    history = data.get("history") if isinstance(data, dict) else None
    if isinstance(history, list) and history and isinstance(history[0], list):
        yield from (item for item in history[0] if isinstance(item, dict))


def load_chords(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[int, int], list[int]]:
    """Load entries and layout notation from a CharaChorder backup.

    Duplicate entries are keyed by decoded output and the shortest positive
    input-code list wins, matching the reference semantics.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    chords_raw = None
    layout = None
    for item in _history_items(data):
        if item.get("type") == "chords":
            chords_raw = item.get("chords")
        elif item.get("type") == "layout":
            layout = item.get("layout")
    if chords_raw is None and isinstance(data, dict):
        chords_raw = data.get("chords")
    if not isinstance(chords_raw, list):
        raise ValueError("backup contains no chords list")

    char_to_switch: dict[int, int] = {}
    switch_order: list[int] = []
    seen: set[int] = set()
    if isinstance(layout, list) and layout and isinstance(layout[0], list):
        current = None
        for code in layout[0]:
            if isinstance(code, int) and 600 <= code <= 617:
                current = code
                if code not in seen:
                    switch_order.append(code)
                    seen.add(code)
            elif current is not None and isinstance(code, int) and 32 <= code <= 126:
                char_to_switch.setdefault(code, current)

    finger_index = {switch: index for index, switch in enumerate(LEFT_TO_RIGHT)}
    mapping: dict[str, dict[str, Any]] = {}
    for item in chords_raw:
        if not isinstance(item, list) or len(item) != 2:
            continue
        input_codes, output_codes = item
        if not isinstance(input_codes, list) or not isinstance(output_codes, list):
            continue
        phrase = decode_output(output_codes).strip()
        if not phrase:
            continue
        cleaned = [code for code in input_codes if isinstance(code, int) and code > 0]
        key = phrase.lower()
        if key not in mapping or len(cleaned) < len(mapping[key]["codes"]):
            mapping[key] = {"phrase": phrase, "codes": cleaned}

    for phrase_key, value in mapping.items():
        codes = value["codes"]
        phrase = phrase_key
        value["orig"] = "+".join(token(code) for code in codes if token(code))

        def word_key(code: int) -> tuple[int, int]:
            if 32 <= code <= 126:
                index = phrase.find(chr(code).lower())
                return (0, index) if index >= 0 else (1, 999)
            return (1, 999)

        by_word = sorted(codes, key=word_key)
        value["by_word"] = "+".join(token(code) for code in by_word if token(code))
        if char_to_switch:
            by_lr = sorted(codes, key=lambda code: (
                finger_index.get(char_to_switch.get(code, 9999), 999), code))
        else:
            by_lr = codes
        value["by_lr"] = "+".join(token(code) for code in by_lr if token(code))
        value["fingers"] = ", ".join(
            f"{token(code)}:{CODE_NAME.get(char_to_switch.get(code, ''), str(char_to_switch.get(code)))}"
            for code in codes
        )
    return mapping, char_to_switch, switch_order


def study_entries(mapping: dict[str, dict[str, Any]], words: Iterable[str] = STUDY_WORDS) -> dict[str, dict[str, Any]]:
    """Return single-word study entries, rejecting missing words."""
    selected = tuple(word.lower() for word in words)
    missing = [word for word in selected if not re.fullmatch(r"[a-z]+", word) or word not in mapping]
    if missing:
        raise ValueError("missing single-word chord entries: " + ", ".join(missing))
    return {word: mapping[word] for word in selected}


def build_schedule(words: Iterable[str] = STUDY_WORDS, repetitions: int = DEFAULT_PAIRS,
                   seed: int = DEFAULT_SEED, condition_order: str = "paired") -> list[dict[str, Any]]:
    """Build reproducible paired trials, optionally in condition blocks.

    ``paired`` alternates the two conditions within each pair with seeded
    order. ``blocked`` places every trial of one condition before every trial
    of the other, while retaining pair IDs for later paired analysis.
    """
    selected = tuple(words)
    if repetitions < 1:
        raise ValueError("--pairs must be at least 1")
    if condition_order not in ("paired", "blocked"):
        raise ValueError("condition_order must be paired or blocked")
    rng = random.Random(seed)
    pair_specs = []
    pair_number = 0
    for repetition in range(1, repetitions + 1):
        for word in selected:
            pair_number += 1
            pair_specs.append((pair_number, repetition, word,
                               f"pair-{pair_number:03d}-r{repetition}-{word}"))

    def trial_for(spec: tuple[int, int, str, str], condition: str) -> dict[str, Any]:
        pair_index, repetition, word, pair_id = spec
        return {
            "trial_id": f"{pair_id}-{condition}", "pair_id": pair_id,
            "pair_index": pair_index, "repetition": repetition, "word": word,
            "condition": condition, "expected_sequence": word,
        }

    if condition_order == "blocked":
        conditions = ["device_chord", "sequential"]
        rng.shuffle(conditions)
        return [trial_for(spec, condition) for condition in conditions for spec in pair_specs]

    schedule: list[dict[str, Any]] = []
    for spec in pair_specs:
        conditions = ["device_chord", "sequential"]
        rng.shuffle(conditions)
        schedule.extend(trial_for(spec, condition) for condition in conditions)
    return schedule


@dataclass
class TrialRecorder:
    """Qt-independent event recorder used by the focused capture widget."""
    trial: dict[str, Any]
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    _next_press_id: int = field(default=1, init=False)
    _active: dict[int, list[int]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    def start(self, now: float | None = None) -> None:
        """Start the trial clock at the moment focused capture is enabled."""
        self.started_at = self.clock() if now is None else now

    def record(self, event_type: str, key_code: int, key_name: str, text: str = "",
               auto_repeat: bool = False, qt_timestamp_ms: int | None = None,
               now: float | None = None) -> dict[str, Any]:
        if event_type not in ("key_down", "key_up"):
            raise ValueError("event_type must be key_down or key_up")
        stamp = self.clock() if now is None else now
        relative = max(0.0, stamp - self.started_at)
        stack = self._active.setdefault(key_code, [])
        if event_type == "key_down":
            # Auto-repeat belongs to the original physical press; another
            # non-repeat down of the same key gets its own local identifier.
            if auto_repeat and stack:
                press_id = stack[-1]
            else:
                press_id = self._next_press_id
                self._next_press_id += 1
                stack.append(press_id)
        else:
            press_id = stack.pop() if stack else None
            if not stack:
                self._active.pop(key_code, None)
        event = {
            "event_index": len(self.events), "event_type": event_type,
            "monotonic_seconds": round(relative, 9),
            "qt_timestamp_ms": qt_timestamp_ms, "key_name": key_name,
            "key_code": key_code, "text": text, "auto_repeat": bool(auto_repeat),
            "press_id": press_id,
        }
        self.events.append(event)
        return event

    def finish(self, performed_instruction: bool, status: str = "completed") -> dict[str, Any]:
        """Derive analysis fields without attributing events to a device."""
        downs = [event for event in self.events if event["event_type"] == "key_down"]
        output_events = [event for event in downs if event["text"]]
        observed = "".join(event["text"] for event in output_events)
        gaps = [round(b["monotonic_seconds"] - a["monotonic_seconds"], 9)
                for a, b in zip(self.events, self.events[1:])]
        output_gaps = [round(b["monotonic_seconds"] - a["monotonic_seconds"], 9)
                       for a, b in zip(output_events, output_events[1:])]
        presses: dict[int, dict[str, Any]] = {}
        durations: list[float] = []
        for event in self.events:
            press_id = event["press_id"]
            if press_id is None:
                continue
            if event["event_type"] == "key_down":
                presses.setdefault(press_id, {"down": event["monotonic_seconds"]})
            elif press_id in presses and "up" not in presses[press_id]:
                presses[press_id]["up"] = event["monotonic_seconds"]
                durations.append(round(presses[press_id]["up"] - presses[press_id]["down"], 9))
        first = self.events[0]["monotonic_seconds"] if self.events else 0.0
        last = self.events[-1]["monotonic_seconds"] if self.events else 0.0
        expected = self.trial["expected_sequence"]
        return {
            **self.trial,
            "status": status,
            "performed_instruction": bool(performed_instruction),
            "observed_output": observed,
            "output_length": len(observed),
            "expected_output_appeared": expected.lower() in observed.lower(),
            "inter_event_gaps_seconds": gaps,
            "inter_output_gaps_seconds": output_gaps,
            "key_durations_seconds": durations,
            "burst_duration_seconds": round(last - first, 9) if self.events else 0.0,
            "event_count": len(self.events),
        }


EVENT_FIELDS = ("session_id", "trial_id", "pair_id", "word", "condition", "event_index",
                "event_type", "monotonic_seconds", "qt_timestamp_ms", "key_name",
                "key_code", "text", "auto_repeat", "press_id")
TRIAL_FIELDS = ("session_id", "trial_id", "pair_id", "pair_index", "repetition", "word",
                "condition", "expected_sequence", "status", "performed_instruction",
                "observed_output", "output_length", "expected_output_appeared",
                "inter_event_gaps_seconds", "inter_output_gaps_seconds",
                "key_durations_seconds", "burst_duration_seconds", "event_count")


def write_exports(out_dir: Path, session: dict[str, Any]) -> None:
    """Write canonical JSON and both CSV exports after each saved trial."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for trial in session["trials"]:
            for event in trial.get("events", []):
                row = {**trial, **event, "session_id": session["metadata"]["session_id"]}
                writer.writerow(row)
    with (out_dir / "trials.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRIAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for trial in session["trials"]:
            writer.writerow({key: json.dumps(value) if isinstance(value, list) else value
                             for key, value in {**trial, "session_id": session["metadata"]["session_id"]}.items()})


def _session_metadata(chords_path: Path, out_dir: Path, words: tuple[str, ...],
                      pairs: int, seed: int, condition_order: str,
                      override: bool) -> dict[str, Any]:
    return {
        "schema_version": 1, "session_id": uuid.uuid4().hex,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "chords_path": str(chords_path.resolve()), "out_dir": str(out_dir.resolve()),
        "study_words": list(words), "word_override": list(words) if override else None,
        "pairs_per_word": pairs, "seed": seed, "condition_order": condition_order,
        "reference_implementation": "/Users/zloy/dev/ChordMentor/charachorder_word_freq.py",
        "parsing_semantics": "Decoded printable output; shortest duplicate input; reference word order; physical left-to-right layout order.",
        "privacy_boundary": "Foreground focused Qt key events only; no global hook, background monitoring, serial/device access, network upload, or device-origin claim.",
        "timestamp_limits": "Monotonic timestamps are local process timing; Qt event timestamps are platform/input-stack dependent and may be absent. Timing does not prove a chord.",
    }


def dry_run(chords_path: Path, words: tuple[str, ...], pairs: int, seed: int,
             condition_order: str) -> int:
    mapping, _, _ = load_chords(chords_path)
    entries = study_entries(mapping, words)
    schedule = build_schedule(words, pairs, seed, condition_order)
    print(f"validated {len(entries)} study chord entries; {len(schedule)} trials ({condition_order})")
    for word, entry in entries.items():
        print(f"{word}: word-order={entry['by_word']} device-order={entry['by_lr']} raw={entry['orig']}")
    for trial in schedule:
        print(f"{trial['trial_id']}: {trial['condition']} expected={trial['expected_sequence']}")
    return 0


def _qt_key_name(event: Any) -> str:
    try:
        from PyQt5.QtGui import QKeySequence
        return QKeySequence(event.key()).toString() or f"Key({event.key()})"
    except Exception:
        return f"Key({event.key()})"


def run_gui(chords_path: Path, out_dir: Path, words: tuple[str, ...], pairs: int, seed: int,
            condition_order: str, entries: dict[str, dict[str, Any]]) -> int:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QFrame, QLabel, QMainWindow,
                                 QPushButton, QVBoxLayout, QWidget)

    schedule = build_schedule(words, pairs, seed, condition_order)
    session = {"metadata": _session_metadata(chords_path, out_dir, words, pairs, seed, condition_order, words != STUDY_WORDS), "trials": []}
    app = QApplication.instance() or QApplication(sys.argv)

    class CaptureArea(QFrame):
        def __init__(self, trial: dict[str, Any], display: QLabel):
            super().__init__()
            self.setFrameStyle(QFrame.Box | QFrame.Plain)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setMinimumHeight(90)
            self.recorder = TrialRecorder(trial)
            self.display = display
            self.started = False

        def begin(self) -> None:
            self.recorder.start()
            self.started = True
            self.setFocus(Qt.OtherFocusReason)

        def keyPressEvent(self, event: Any) -> None:
            if self.started:
                self.recorder.record("key_down", event.key(), _qt_key_name(event), event.text(),
                                     event.isAutoRepeat(), getattr(event, "timestamp", lambda: None)())
                self.display.setText(self.recorder.finish(False)["observed_output"])
            event.accept()

        def keyReleaseEvent(self, event: Any) -> None:
            if self.started:
                self.recorder.record("key_up", event.key(), _qt_key_name(event), event.text(),
                                     event.isAutoRepeat(), getattr(event, "timestamp", lambda: None)())
            event.accept()

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.position = 0
            self.current: CaptureArea | None = None
            self.instruction_check: QCheckBox | None = None
            self.setWindowTitle("CharaChorder research chord study (foreground only)")
            self.container = QWidget()
            self.layout = QVBoxLayout(self.container)
            self.setCentralWidget(self.container)
            self.render_trial()

        def clear(self) -> None:
            while self.layout.count():
                item = self.layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        def render_trial(self) -> None:
            self.clear()
            if self.position >= len(schedule):
                self.finish_session("completed")
                return
            trial = schedule[self.position]
            entry = entries[trial["word"]]
            instruction = (f"Exact instruction: Focus the box, then press the physical chord for '{trial['word']}'."
                           if trial["condition"] == "device_chord" else
                           f"Exact instruction: Focus the box, then type '{trial['word']}' conventionally, one key at a time.")
            self.layout.addWidget(QLabel(f"Trial {self.position + 1}/{len(schedule)}   Pair {trial['pair_id']}"))
            self.layout.addWidget(QLabel(f"TARGET WORD: {trial['word']}    TRIAL TYPE: {trial['condition']}"))
            self.layout.addWidget(QLabel(f"EXPECTED CHARACTER SEQUENCE: {trial['expected_sequence']}"))
            self.layout.addWidget(QLabel(f"CHORD WORD ORDER: {entry['by_word']}"))
            self.layout.addWidget(QLabel(f"CHORD DEVICE ORDER (physical left-to-right): {entry['by_lr']}"))
            self.layout.addWidget(QLabel(f"RAW INPUT CODES: {entry['orig']}"))
            self.layout.addWidget(QLabel(instruction))
            self.instruction_check = QCheckBox("I performed the instruction above")
            self.layout.addWidget(self.instruction_check)
            observed = QLabel("(capture has not started)")
            self.current = CaptureArea(trial, observed)
            self.layout.addWidget(observed)
            self.layout.addWidget(self.current)
            begin = QPushButton("Begin this trial")
            complete = QPushButton("Complete Trial")
            stop = QPushButton("Stop & Save")
            cancel = QPushButton("Cancel Without Saving")
            complete.setEnabled(False)
            begin.clicked.connect(lambda: self.begin_trial(begin, complete))
            complete.clicked.connect(self.complete_trial)
            stop.clicked.connect(lambda: self.stop_save())
            cancel.clicked.connect(self.cancel)
            self.current.destroyed.connect(lambda: None)
            self.layout.addWidget(begin)
            self.layout.addWidget(complete)
            self.layout.addWidget(stop)
            self.layout.addWidget(cancel)
            self._complete_button = complete
            self._begin_button = begin
            self.show()

        def begin_trial(self, begin: QPushButton, complete: QPushButton) -> None:
            if not self.instruction_check or not self.instruction_check.isChecked():
                self.statusBar().showMessage("Check that you performed the exact instruction before capture.")
                return
            assert self.current
            self.current.begin()
            begin.setEnabled(False)
            complete.setEnabled(True)
            self.statusBar().showMessage("Capturing focused key events only.")

        def complete_trial(self) -> None:
            if not self.current or not self.current.started:
                return
            assert self.instruction_check
            result = self.current.recorder.finish(self.instruction_check.isChecked())
            result["events"] = list(self.current.recorder.events)
            session["trials"].append(result)
            write_exports(out_dir, session)
            self.position += 1
            self.render_trial()

        def stop_save(self) -> None:
            if self.current and self.current.started:
                assert self.instruction_check
                result = self.current.recorder.finish(self.instruction_check.isChecked(), "stopped")
                result["events"] = list(self.current.recorder.events)
                session["trials"].append(result)
            session["metadata"]["stop_status"] = "stop_requested"
            write_exports(out_dir, session)
            self.close()

        def finish_session(self, status: str) -> None:
            session["metadata"]["stop_status"] = status
            write_exports(out_dir, session)
            self.close()

        def cancel(self) -> None:
            # Deliberately do not call write_exports: this is the no-saving path.
            self.close()

    window = Window()
    window.resize(760, 520)
    return app.exec_()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chords", required=True, type=Path, help="CharaChorder backup JSON path")
    parser.add_argument("--out-dir", type=Path, default=Path("chord-study-output"), help="local output directory")
    parser.add_argument("--pairs", type=int, default=DEFAULT_PAIRS, help="repetitions per word/condition (default: 3)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"schedule seed (default: {DEFAULT_SEED})")
    parser.add_argument("--condition-order", choices=("paired", "blocked"), default="paired",
                        help="interleave conditions per pair, or group each condition into a block")
    parser.add_argument("--words", help="comma-separated explicit word override; recorded in session metadata")
    parser.add_argument("--dry-run", action="store_true", help="validate and print schedule without opening Qt or capturing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    words = STUDY_WORDS if args.words is None else tuple(word.strip().lower() for word in args.words.split(",") if word.strip())
    if not words:
        print("error: --words must contain at least one word", file=sys.stderr)
        return 2
    try:
        mapping, _, _ = load_chords(args.chords)
        entries = study_entries(mapping, words)
        if args.dry_run:
            return dry_run(args.chords, words, args.pairs, args.seed, args.condition_order)
        return run_gui(args.chords, args.out_dir, words, args.pairs, args.seed,
                       args.condition_order, entries)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
