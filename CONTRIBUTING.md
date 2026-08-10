# Contributing

## macOS contributor and agent setup

Use an isolated environment for retype. This workflow is the same for human
contributors and autonomous agents. Do not install project dependencies into
Homebrew's or another global Python.

1. Install a supported Python and [uv](https://docs.astral.sh/uv/) (Homebrew is
   convenient: `brew install python@3.14 uv`).
2. From the repository root, create/select the environment and install the
   locked runtime, test, and build dependencies:

   ```sh
   uv venv --python 3.14
   uv sync --locked --all-groups
   ```

   Activating `.venv/bin/activate` is optional; `uv run` uses the project
   environment without activation.
3. Validate the lock and package metadata, then run the Qt tests headlessly:

   ```sh
   uv lock --check
   uv run --locked --all-groups python -c \
     "from importlib.metadata import metadata; print(metadata('retype').get_all('Requires-Dist'))"
   QT_QPA_PLATFORM=offscreen uv run --locked --group test pytest
   ```

   `QT_QPA_PLATFORM=offscreen` is for automated tests only. The project remains
   a PyQt5/Qt5 application; this setup does not start a Qt6 or PySide migration.

The declared Python range is 3.10 through 3.14 (`>=3.10,<3.15`). The Phase 1
lock was resolved for that range. This checkout has validated Python 3.11,
3.13, and 3.14; Python 3.10 and 3.12 were not available in the validation
machine, so compatibility with those versions is not claimed as tested here.
PyQt5 wheel/platform availability can also vary by Python and macOS runner.

The source launcher remains `uv run --locked bin/retype`. The existing
PyInstaller flow remains available with the isolated build group, for example:

```sh
uv run --locked --group build python setup.py b -k bundle
```

## Native macOS visual evidence

When visual evidence is needed, run the application natively on macOS with the
offscreen variable unset:

```sh
uv run --locked bin/retype
screencapture -x evidence/retype-macos.png
```

Use the macOS display/window and the built-in Screenshot tool (or
`screencapture`) for evidence. Phase 1 documents this native macOS-first path
only; it does not add a screenshot harness, golden baselines, or packaging
smoke tests. Keep evidence files out of the source tree unless a later task
explicitly adds an evidence convention.
