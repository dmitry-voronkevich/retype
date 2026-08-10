#!/usr/bin/env bash
set -euo pipefail

uv lock --check
uv sync --locked --all-groups
uv run --locked --all-groups python -c '
from importlib.metadata import metadata

required = {"PyQt5", "ebooklib", "tinycss2"}
requires = {
    requirement.split(";", 1)[0].split("<", 1)[0].split(">", 1)[0]
    .split("=", 1)[0].split("!", 1)[0].split("~", 1)[0].strip()
    for requirement in metadata("retype").get_all("Requires-Dist", [])
}
missing = required - requires
if missing:
    raise SystemExit(f"missing runtime metadata: {sorted(missing)}")
print("runtime dependency metadata: OK")
'
QT_QPA_PLATFORM=offscreen uv run --locked --group test pytest
