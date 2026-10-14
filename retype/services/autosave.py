from qt import QObject, QTimer, pyqtSignal

from typing import TYPE_CHECKING


class IdleSignal(QObject):
    idle = pyqtSignal()

    def __init__(self, console, interval, parent=None):
        # type: (IdleSignal, Console, int, QObject | None) -> None
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setInterval(interval)
        self.timer.timeout.connect(self.emitIdle)
        console.textEdited.connect(self.onUpdate)

    def emitIdle(self):
        # type: (IdleSignal) -> None
        self.idle.emit()
        self.timer.stop()

    def onUpdate(self):
        # type: (IdleSignal) -> None
        self.timer.start()


class Autosave(QObject):
    save = pyqtSignal()

    def __init__(self, console, on=True, interval=5000, parent=None):
        # type: (Autosave, Console, bool, int, QObject | None) -> None
        super().__init__(parent)
        self.on = on
        self.signal = IdleSignal(console, interval, self)
        self.signal.idle.connect(self.maybeEmitSave)

    def maybeEmitSave(self):
        # type: (Autosave) -> None
        if self.on:
            self.save.emit()


if TYPE_CHECKING:
    from retype.console import Console  # noqa: F401
