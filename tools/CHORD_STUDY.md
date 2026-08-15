# Real-device chord study tool

`chord_study.py` is an opt-in research instrument, separate from retype's
production chord detector and feedback UI. It adapts the parsing semantics of
the user's ChordMentor reference at
`/Users/zloy/dev/ChordMentor/charachorder_word_freq.py`; that repository is not
a dependency and is not modified here.

## Invocation

From the repository root (after `uv sync --locked`):

```console
uv run python tools/chord_study.py \
  --chords ~/Downloads/charachorder-backup.json \
  --out-dir ~/Desktop/retype-chord-study \
  --pairs 3 --seed 20250308 --condition-order blocked
```

The initial words, in order, are `the`, `and`, `for`, `was`, `without`,
`because`, `with`, and `at`. Every pair contains one `device_chord` trial and
one `sequential` trial, with three repetitions by default. The default
`--condition-order paired` shuffles the two conditions within each pair. Use
`--condition-order blocked` to group all trials of one condition before all
trials of the other (the seed reproducibly chooses which block comes first).
Pair IDs remain attached to both trials for paired analysis. An explicit
override such as `--words the,and` is accepted only as an explicitly recorded
session metadata field (`word_override`). The backup is validated for all
selected single-word entries before the window opens.

For CI/headless validation, use the same backup without opening Qt:

```console
uv run python tools/chord_study.py --chords backup.json --dry-run
uv run python tools/chord_study.py --help
```

## Trial procedure

1. Read the displayed **exact instruction**, target, expected character
   sequence, pair ID, word-order notation, physical left-to-right/device-order
   notation, and raw input codes.
2. Check **I performed the instruction above**. Until **Begin this trial** is
   pressed, no events are captured.
3. Press **Begin this trial**, which focuses the visible capture box. For a
   `device_chord` trial, press the physical chord; for a `sequential` trial,
   type the expected word conventionally, one key at a time.
4. Press **Complete Trial**. **Stop & Save** saves completed trials (and a
   currently active trial as `stopped`); **Cancel Without Saving** writes
   nothing for the current session.

Each save updates `session.json`, `events.csv`, and `trials.csv` in the output
directory. The JSON contains every focused key-down and key-up event, including
monotonic trial-relative time, Qt timestamp when available, key name/code,
text, auto-repeat, and a local press ID. Derived trial fields include observed
output, output length, inter-event/output gaps, paired key durations, burst
duration, event count, instruction completion, expected-output appearance, and
status.

## Privacy and interpretation

Capture is a foreground Qt widget with explicit focus only. There is no global
keyboard hook, background monitoring, serial-number collection, device/USB
access, network upload, or device-origin claim. Keep the output directory
local and treat it as sensitive keystroke data.

Monotonic timing is local process timing. Qt event timestamps depend on the
platform/input stack and can be missing or have different resolution. A short
burst or matching output is evidence in this study, not proof that a device
produced it as a chord. Analyze `condition` separately; never pool device and
sequential trials as if they measured the same behavior.

## Analysis

Load `session.json` for the canonical event-level record, or use `trials.csv`
for one-row-per-trial summaries and `events.csv` for event-level analysis.
Group by `pair_id`, `word`, `repetition`, and `condition`; compare paired
conditions only after checking `status == completed`,
`performed_instruction`, and `expected_output_appeared`. Useful exploratory
metrics are `burst_duration_seconds`, the gap arrays, key-duration arrays,
`event_count`, and output length. Preserve the seed and pair IDs in reports;
do not infer device attribution from timing alone.
