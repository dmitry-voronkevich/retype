Dependencies
============

**Required:**

- Python 3.10--3.14 (declared as ``>=3.10,<3.15``)
- ``PyQt5`` (the Qt5 binding target)
- ``ebooklib``
- ``tinycss2``

**Optional:**

- ``pywin32`` -- Windows-only. This is only used for optionally hiding the System Console window.
- ``pytest`` -- to run tests
- ``pyinstaller`` and ``setuptools`` -- to build retype
- ``Sphinx`` and ``sphinx-rtd-theme`` -- to build the docs locally

Runtime dependencies are declared in ``pyproject.toml`` and pinned for
reproduction by ``uv.lock``. Use ``uv sync --locked`` (or
``uv sync --locked --all-groups`` for tests/builds/docs) inside the project
venv; do not install them into global Python.

Phase 1 validation covered Python 3.11, 3.13, and 3.14 on the validation
machine. Python 3.10 and 3.12 were unavailable there and remain untested; the
lock's declared range is not a claim that every version/platform combination
has been validated. See ``CONTRIBUTING.md`` for the macOS offscreen test and
native screenshot/evidence workflow.
