"""Shared fixtures for deterministic PyQt5 integration tests."""

from pathlib import Path

import pytest
from qt import QDialog, QTimer

from retype.controllers import MainController


@pytest.fixture
def make_controller(qapp, qtbot, tmp_path):
    """Build an isolated controller and close every Qt resource it owns."""
    library_dir = str(Path(__file__).parents[2] / "library")
    controllers = []

    def factory(chords_path=None):
        user_dir = tmp_path / f"user-{len(controllers)}"
        user_dir.mkdir()
        controller = MainController(
            chords_path=str(chords_path) if chords_path else None,
            config_dir=str(user_dir),
            library_paths=[library_dir],
        )
        controller.show()
        qtbot.wait(20)
        controllers.append(controller)
        return controller

    yield factory

    # Reject first: a modal dialog must not be left running while the window
    # or its event-loop-owned timers are being torn down.
    for controller in controllers:
        for widget in list(qapp.topLevelWidgets()):
            if isinstance(widget, QDialog) and widget.isVisible():
                widget.reject()
                widget.close()
        for view in controller.views.values():
            autosave = getattr(view, 'autosave', None)
            signal = getattr(autosave, 'signal', None)
            timer = getattr(signal, 'timer', None)
            if timer is not None:
                timer.stop()
        controller.quit()
        qtbot.wait(20)

    for timer in qapp.findChildren(QTimer):
        timer.stop()
    qapp.processEvents()


@pytest.fixture
def controller(make_controller):
    return make_controller()
