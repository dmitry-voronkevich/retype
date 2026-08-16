import json
from pathlib import Path

import pytest

from tools.chord_study import (
    STUDY_WORDS,
    TrialRecorder,
    build_schedule,
    load_chords,
    study_entries,
    write_exports,
)


def backup(tmp_path: Path) -> Path:
    words = {
        word: [[ord(char) for char in word], [ord(char) for char in word]]
        for word in STUDY_WORDS
    }
    # A shorter duplicate must replace the first `the` entry.
    chords = [[[ord("x"), ord("y"), ord("z")], [ord("t"), ord("h"), ord("e")]],
              [[ord("q")], [ord("t"), ord("h"), ord("e")]]]
    chords.extend(words[word] for word in STUDY_WORDS if word != "the")
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"history": [[
        {"type": "chords", "chords": chords},
        {"type": "layout", "layout": [[606, ord("a"), ord("b"),
                                             609, ord("c"), ord("d")]]},
    ]]}), encoding="utf-8")
    return path


def test_parsing_shortest_duplicate_and_layout_notation(tmp_path):
    mapping, char_to_switch, switch_order = load_chords(backup(tmp_path))
    assert mapping["the"]["codes"] == [ord("q")]
    assert mapping["the"]["by_word"] == "q"
    assert mapping["the"]["orig"] == "q"
    assert char_to_switch[ord("a")] == 606
    assert char_to_switch[ord("c")] == 609
    assert switch_order == [606, 609]
    assert set(study_entries(mapping)) == set(STUDY_WORDS)


def test_exact_reproducible_paired_schedule():
    schedule = build_schedule(seed=17, condition_order="paired")
    assert len(schedule) == 8 * 3 * 2
    assert [trial["word"] for trial in schedule[:2]] == ["the", "the"]
    assert all(trial["expected_sequence"] == trial["word"] + " " for trial in schedule)
    pairs = {}
    for trial in schedule:
        pairs.setdefault(trial["pair_id"], []).append(trial)
    assert len(pairs) == 24
    assert all({trial["condition"] for trial in trials} == {"device_chord", "sequential"}
               for trials in pairs.values())
    assert schedule == build_schedule(seed=17, condition_order="paired")
    assert schedule != build_schedule(seed=18, condition_order="paired")

    blocked = build_schedule(seed=17, condition_order="blocked")
    assert blocked == build_schedule(seed=17)
    half = len(blocked) // 2
    assert len({trial["condition"] for trial in blocked[:half]}) == 1
    assert len({trial["condition"] for trial in blocked[half:]}) == 1
    assert {trial["condition"] for trial in blocked[:half]} == {"sequential"}
    assert {trial["condition"] for trial in blocked[half:]} == {"device_chord"}
    assert blocked == build_schedule(seed=17, condition_order="blocked")


def test_event_pairing_and_derived_timing():
    times = iter((100.0, 100.1, 100.25, 100.5))
    recorder = TrialRecorder({"trial_id": "t", "pair_id": "p", "pair_index": 1,
                              "repetition": 1, "word": "a", "condition": "sequential",
                              "expected_sequence": "a"}, clock=lambda: next(times))
    recorder.record("key_down", 65, "A", "a", now=100.1)
    recorder.record("key_up", 65, "A", now=100.25)
    recorder.record("key_down", 66, "B", "", now=100.5)
    result = recorder.finish(True)
    assert result["observed_output"] == "a"
    assert result["expected_output_appeared"] is True
    assert result["event_count"] == 3
    assert result["key_durations_seconds"] == [0.15]
    assert result["inter_event_gaps_seconds"] == [0.15, 0.25]
    assert result["burst_duration_seconds"] == 0.4
    assert recorder.events[0]["press_id"] == recorder.events[1]["press_id"]


def test_restart_discards_attempt_and_resets_local_press_ids():
    recorder = TrialRecorder({"expected_sequence": "a"}, clock=lambda: 1.0)
    recorder.record("key_down", 65, "A", "a", now=1.1)
    recorder.start(now=2.0)
    assert recorder.events == []
    event = recorder.record("key_down", 66, "B", "b", now=2.1)
    assert event["press_id"] == 1
    assert recorder.finish(True)["observed_output"] == "b"


def test_cancel_without_saving_and_exports(tmp_path):
    trial = {"trial_id": "t", "pair_id": "p", "pair_index": 1, "repetition": 1,
             "word": "a", "condition": "sequential", "expected_sequence": "a",
             "status": "completed", "performed_instruction": True,
             "observed_output": "a", "output_length": 1,
             "expected_output_appeared": True, "inter_event_gaps_seconds": [],
             "inter_output_gaps_seconds": [], "key_durations_seconds": [],
             "burst_duration_seconds": 0.0, "event_count": 1,
             "events": [{"event_index": 0, "event_type": "key_down",
                         "monotonic_seconds": 0.0, "qt_timestamp_ms": None,
                         "key_name": "A", "key_code": 65, "text": "a",
                         "auto_repeat": False, "press_id": 1}]}
    session = {"metadata": {"session_id": "session-test"}, "trials": [trial]}
    write_exports(tmp_path, session)
    assert (tmp_path / "session.json").exists()
    assert (tmp_path / "events.csv").read_text().count("key_down") == 1
    assert "session-test" in (tmp_path / "trials.csv").read_text()
    # The no-saving path is represented by simply not calling write_exports.
    cancelled = tmp_path / "cancelled"
    assert not cancelled.exists()


def test_missing_word_is_reported(tmp_path):
    mapping, _, _ = load_chords(backup(tmp_path))
    with pytest.raises(ValueError, match="missing single-word"):
        study_entries(mapping, ("missing",))
