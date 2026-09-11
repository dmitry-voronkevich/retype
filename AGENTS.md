# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.
- Chord successes use the typed `ValidatedChord` domain event in `retype/services/chord_detection.py`; keep Likely timing observations separate. `retype/services/chord_mastery.py` is session-only and cannot infer direct-entry evidence from its timing fields; see `docs/source/book-view.rst`.
- Run GUI checks through `uv run --group test pytest`. On macOS, all assertions can pass but Qt teardown can then print `QBasicTimer::start` warnings and exit 139; report this as a teardown limitation, not a green process. Timer cleanup is centralized in `tests/gui/conftest.py`.
- The CharaChorder loading contract and supported profile are documented in `docs/source/device-startup.rst`; `retype/services/device_snapshot.py` owns the read and atomic handoff to `BookView.setChords`, while the `load_chords_on_startup` setting and "Load chords now" action live in the `Chords & CharaChorder` section.
- Chord mastery persistence now keeps cumulative counts and manual mastered/unmastered overrides in `chord-mastery.json`; the lesson-limit setting lives in config and `BookView.setAdaptiveChordLessonLimit` is the canonical refresh path.
- The main-window status bar uses one feedback model with sticky base/mastery messages and transient detection overlays; `retype/ui/main_win.py` owns the lifetime/priority rules and `docs/source/book-view.rst` documents the user-facing behavior.
- Build the macOS release artifact with `scripts/build-macos-dmg.sh`; it uses the locked `uv` build group, outputs `dist/retype.app` and `dist/retype.dmg`, and CI publishes the native runner architecture. Signing and notarization remain release-owner policy.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
