# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.
- Chord congratulations are a bounded final-token heuristic in `retype/stats/stats_dock.py`; preserve its separate Likely timing metric and the limitation documented in `docs/source/book-view.rst`.
- Run GUI checks through `uv run --group test pytest`. On macOS, successful full runs can still print `QBasicTimer::start` teardown warnings; use the process exit code and pytest result, not warnings alone. Timer cleanup is centralized in `tests/gui/conftest.py`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
