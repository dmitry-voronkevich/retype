"""Shared fixtures for deterministic PyQt5 integration tests."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from qt import QDialog, QTimer

from retype.controllers import MainController
from retype.services.device_snapshot import DeviceReadError


class _NoDeviceReader:
    """Keep ordinary GUI tests independent of physical serial hardware."""

    def read(self, _progress):
        raise DeviceReadError("no test device configured")

    def cancel(self):
        pass


@pytest.fixture
def make_controller(qapp, qtbot, tmp_path):
    """Build an isolated controller and close every Qt resource it owns."""
    library_dir = str(Path(__file__).parents[2] / "library")
    controllers = []

    def factory(device_reader_factory=None, config=None):
        user_dir = tmp_path / f"user-{len(controllers)}"
        if device_reader_factory is None:
            device_reader_factory = lambda: _NoDeviceReader()
        user_dir.mkdir()
        if config is not None:
            data = deepcopy(config)
            data['user_dir'] = str(user_dir)
            (user_dir / 'config.json').write_text(json.dumps(data))
        controller = MainController(
            config_dir=str(user_dir),
            library_paths=[library_dir],
            device_reader_factory=device_reader_factory,
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
        # MainController.quit() intentionally exits the application on
        # macOS.  Fixture cleanup must only close this controller so later
        # tests can continue using the shared QApplication.
        controller._window.close()
        qtbot.wait(20)

    for timer in qapp.findChildren(QTimer):
        timer.stop()
    qapp.processEvents()


@pytest.fixture
def controller(make_controller):
    return make_controller()
