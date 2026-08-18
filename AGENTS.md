# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.
- Chord congratulations use the typed `ValidatedChord` domain event in `retype/services/chord_detection.py`; preserve its separate Likely timing metric and the limitation documented in `docs/source/book-view.rst`.
- Run GUI checks through `uv run --group test pytest`. On macOS, all assertions can pass but Qt teardown can then print `QBasicTimer::start` warnings and exit 139; report this as a teardown limitation, not a green process. Timer cleanup is centralized in `tests/gui/conftest.py`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
