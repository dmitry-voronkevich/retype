#!/usr/bin/env python3
"""Capture deterministic Qt widget evidence for agent and CI runs.

This intentionally captures widgets with QWidget.grab(), not screen
coordinates. It creates fresh temporary config data for each scenario and
never compares or approves a golden image.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import tempfile
import traceback
from pathlib import Path

from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
from qt import QApplication

from retype.controllers import MainController

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library"
SCENARIOS = ("shelf", "book", "customisation", "chords")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--scenario", choices=SCENARIOS, default=None,
        help="Capture one scenario; the default captures all four.",
    )
    p.add_argument(
        "--output-dir", type=Path, default=ROOT / "evidence" / "gui",
        help="Directory for PNGs, manifest.json, and capture.log.",
    )
    return p


def write_chords(path: Path) -> None:
    """Create a deterministic multi-hint backup with authoritative layout data."""
    chords = [
        [[111, 102], [111, 102]],              # of
        [[116, 104, 101], [116, 104, 101]],    # the
        [[110, 97, 116, 117, 114, 101],
         [110, 97, 116, 117, 114, 101]],       # nature
        [[102, 108, 97, 116, 110, 100],
         [102, 108, 97, 116, 108, 97, 110, 100]],  # flatland
        [[119, 111, 114, 108, 100],
         [119, 111, 114, 108, 100]],            # world
        [[115, 116, 114, 97, 110, 103, 101],
         [115, 116, 114, 97, 110, 103, 101]],   # strange
        [[99, 97, 115, 101], [99, 97, 115, 101]],  # case
        [[100, 114], [100, 114]],                # dr
        [[106, 101, 107, 121, 108, 108],
         [106, 101, 107, 121, 108, 108]],        # jekyll
        [[97, 110, 100], [97, 110, 100]],        # and
        [[109, 114], [109, 114]],                # mr
        [[104, 121, 100, 101],
         [104, 121, 100, 101]],                  # hyde
    ]
    layout = [[
        606, *map(ord, "abcdefghi"),
        608, *map(ord, "jklmnopqr"),
        607, *map(ord, "stuvwxyz"),
    ]]
    path.write_text(
        json.dumps({"history": [[
            {"type": "chords", "chords": chords},
            {"type": "layout", "layout": layout},
        ]]}),
        encoding="utf-8",
    )


def capture_scenario(app: QApplication, scenario: str, output: Path) -> dict:
    """Run one scenario and return its manifest entry."""
    with tempfile.TemporaryDirectory(prefix="retype-capture-") as temp:
        user_dir = Path(temp) / "user"
        user_dir.mkdir()
        chords_path = None
        if scenario == "chords":
            chords_path = Path(temp) / "chords.json"
            write_chords(chords_path)

        controller = MainController(
            chords_path=str(chords_path) if chords_path else None,
            config_dir=str(user_dir),
            library_paths=[str(LIBRARY)],
        )
        window = controller._window
        window.resize(960, 720)
        controller.show()
        app.processEvents()

        try:
            if scenario in ("book", "chords"):
                controller.loadBook(0)
                app.processEvents()
                widget = window
            elif scenario == "customisation":
                dialog = controller.customisation_dialog
                dialog.resize(700, 600)
                dialog.show()
                app.processEvents()
                widget = dialog
            else:
                widget = window

            pixmap = widget.grab()
            path = output / f"{scenario}.png"
            if not pixmap.save(str(path), "PNG"):
                raise RuntimeError(f"Qt could not save widget capture: {path}")
            return {
                "scenario": scenario,
                "dimensions": {"width": pixmap.width(), "height": pixmap.height()},
                "output_path": str(path.resolve()),
            }
        finally:
            # Modal dialogs must be rejected before closing the main window.
            if controller.customisation_dialog.isVisible():
                controller.customisation_dialog.reject()
                controller.customisation_dialog.close()
            for view in controller.views.values():
                autosave = getattr(view, 'autosave', None)
                signal = getattr(autosave, 'signal', None)
                timer = getattr(signal, 'timer', None)
                if timer is not None:
                    timer.stop()
            controller.quit()
            for top_level in list(app.topLevelWidgets()):
                if top_level is not window and top_level.isVisible():
                    top_level.close()
            app.processEvents()


def main() -> int:
    args = parser().parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "capture.log"
    logging.basicConfig(
        filename=log_path, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    scenarios = (args.scenario,) if args.scenario else SCENARIOS
    manifest = {
        "schema_version": 1,
        "python": sys.version,
        "python_implementation": platform.python_implementation(),
        "qt": QT_VERSION_STR,
        "pyqt": PYQT_VERSION_STR,
        "platform": platform.platform(),
        "system": sys.platform,
        "qt_platform": None,
        "output_directory": str(output),
        "scenarios": [],
        "failures": [],
        "diagnostics": {"log_path": str(log_path)},
    }

    app = QApplication.instance() or QApplication(sys.argv)
    manifest["qt_platform"] = app.platformName()
    for scenario in scenarios:
        try:
            entry = capture_scenario(app, scenario, output)
            manifest["scenarios"].append(entry)
            logging.info("Captured %s: %s", scenario, entry["output_path"])
        except Exception as error:  # Keep the manifest useful after a failure.
            details = {
                "scenario": scenario,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
            manifest["failures"].append(details)
            logging.exception("Capture failed for %s", scenario)

    manifest_path = output / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    app.processEvents()
    return 1 if manifest["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
