import logging
from base64 import b64decode
from qt import (QMainWindow, QStackedWidget, QSplitter, Qt, pyqtSignal, QDir,
                QFile, QTimer)

from typing import TYPE_CHECKING

from retype.resource_handler import getStylePath, getIcon


class _StatusBarFeedback:
    DETECTION_PRIORITY = 10
    PROGRESSION_PRIORITY = 20

    def __init__(self, status_bar):
        # type: (_StatusBarFeedback, object) -> None
        self.status_bar = status_bar
        self.base_message = ''
        self.base_visible = False
        self._overlay_priority = -1
        self._overlay_timeout_ms = 0
        self._overlay_message = ''
        self._pending_overlay = None  # type: tuple[str, int, int] | None
        self._overlay_token = 0
        self._overlay_clear_timer = QTimer(status_bar)
        self._overlay_clear_timer.setSingleShot(True)
        self._overlay_clear_timer.timeout.connect(self._restore_base)
        self._debounce_timer = QTimer(status_bar)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._flush_pending_overlay)

    def set_base_message(self, message):
        # type: (_StatusBarFeedback, str) -> None
        self.base_message = message
        self.base_visible = bool(message)
        if self._overlay_priority < 0:
            self._show_base()

    def get_base_message(self):
        # type: (_StatusBarFeedback) -> str
        return self.base_message

    def _show_base(self):
        # type: (_StatusBarFeedback) -> None
        if self.base_visible:
            self.status_bar.showMessage(self.base_message)
        else:
            self.status_bar.clearMessage()

    def show_detection(self, message):
        # type: (_StatusBarFeedback, str) -> None
        self._request_overlay(message, self.DETECTION_PRIORITY, 1200, 150)

    def show_progression(self, message):
        # type: (_StatusBarFeedback, str) -> None
        self._request_overlay(message, self.PROGRESSION_PRIORITY, 4000)

    def _request_overlay(self, message, priority, timeout_ms, debounce_ms=0):
        # type: (_StatusBarFeedback, str, int, int, int) -> None
        if priority < self._overlay_priority:
            return
        self._pending_overlay = (message, priority, timeout_ms)
        self._debounce_timer.stop()
        if debounce_ms:
            self._debounce_timer.start(debounce_ms)
            return
        self._flush_pending_overlay()

    def _flush_pending_overlay(self):
        # type: (_StatusBarFeedback) -> None
        pending = self._pending_overlay
        self._pending_overlay = None
        self._debounce_timer.stop()
        if pending is None:
            return
        message, priority, timeout_ms = pending
        if priority < self._overlay_priority:
            return
        self._overlay_token += 1
        self._overlay_priority = priority
        self._overlay_message = message
        self.status_bar.showMessage(message)
        self._overlay_clear_timer.stop()
        self._overlay_timeout_ms = timeout_ms
        if timeout_ms > 0:
            self._overlay_clear_timer.start(timeout_ms)

    def _restore_base(self):
        # type: (_StatusBarFeedback) -> None
        self._overlay_clear_timer.stop()
        self._overlay_priority = -1
        self._overlay_timeout_ms = 0
        self._overlay_message = ''
        if self.base_visible:
            self.status_bar.showMessage(self.base_message)
        else:
            self.status_bar.clearMessage()


class MainWin(QMainWindow):
    opened = pyqtSignal()
    closing = pyqtSignal()

    def __init__(self, console, geometry, parent=None):  # qss_file
        # type: (MainWin, Console, Geometry, QWidget | None) -> None
        super().__init__(parent)
        self.console = console
        self.geom = geometry
        self._initUI()
        self._initQss()

    def _initUI(self):
        # type: (MainWin) -> None
        self.stacker = QStackedWidget()
        self.stacker.setObjectName('view-stacker')
        self.consistent_layout = QSplitter()
        self.consistent_layout.setObjectName('main-splitter')
        self.consistent_layout.setHandleWidth(2)
        self.consistent_layout.setOrientation(Qt.Orientation.Vertical)
        self.consistent_layout.setContentsMargins(0, 0, 0, 0)
        self.consistent_layout.addWidget(self.stacker)
        self.consistent_layout.addWidget(self.console)

        self.setCentralWidget(self.consistent_layout)

        status_bar = self.statusBar()
        status_bar.setObjectName('app-status-bar')
        status_bar.setAccessibleName('Application status')
        status_bar.setAccessibleDescription(
            'Shows transient application, device, and chord feedback messages.')
        self._status_feedback = _StatusBarFeedback(status_bar)

        self.resize(self.geom['w'], self.geom['h'])
        if self.geom['x'] is not None and self.geom['y'] is not None:
            self.move(self.geom['x'], self.geom['y'])

        self.setWindowTitle('retype')
        self.setWindowIcon(getIcon('retype', 'ico'))

        self.splitters = {'main': self.consistent_layout}
        self.maybeRestoreSplitterState('main')

    def _initQss(self):
        # type: (MainWin) -> None
        QDir.addSearchPath('style', getStylePath())
        qss_file = QFile('style:0_default.qss')
        qss_file.open(QFile.ReadOnly | QFile.Text)
        qss = str(qss_file.readAll(), 'utf-8'
                  )  # type: str  # type: ignore[call-overload]
        self.setStyleSheet(qss)

    def setBaseStatus(self, message):
        # type: (MainWin, str) -> None
        self._status_feedback.set_base_message(message)

    def baseStatus(self):
        # type: (MainWin) -> str
        return self._status_feedback.get_base_message()

    def showChordDetectionStatus(self, message):
        # type: (MainWin, str) -> None
        self._status_feedback.show_detection(message)

    def showChordProgressStatus(self, message):
        # type: (MainWin, str) -> None
        self._status_feedback.show_progression(message)

    def currentView(self):
        # type: (MainWin) -> QWidget
        return self.stacker.currentWidget()

    def showEvent(self, event):
        # type: (MainWin, QShowEvent) -> None
        QMainWindow.showEvent(self, event)
        self.opened.emit()

    def closeEvent(self, event):
        # type: (MainWin, QCloseEvent) -> None
        self.closing.emit()
        event.accept()
        logging.info('retype quit')

    def denoteSplitter(self, name, splitter):
        # type: (MainWin, str, QSplitter) -> None
        self.splitters[name] = splitter

    def maybeRestoreSplitterState(self, name):
        # type: (MainWin, str) -> None
        splitter = self.splitters.get(name)
        if splitter and self.geom['save_splitters_on_quit']:
            key = f'{name}_splitter_state\
'  # type: Literal['main_splitter_state']  # type: ignore[assignment]
            encoded_state = self.geom.get(key)
            if encoded_state is not None:
                splitter.restoreState(b64decode(encoded_state))


if TYPE_CHECKING:
    from retype.extras.metatypes import Geometry  # noqa: F401
    from qt import QWidget, QShowEvent, QCloseEvent  # noqa: F401
    from retype.console import Console  # noqa: F401
    from typing import Literal  # noqa: F401
