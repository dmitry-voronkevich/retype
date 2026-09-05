import os
import logging
from enum import Enum
from qt import QObject, pyqtSignal, QUrl, QDesktopServices, QMessageBox

from typing import TYPE_CHECKING

from retype.ui import (MainWin, ShelfView, BookView, CustomisationDialog,
                       AboutDialog)
from retype.games.typespeed import TypespeedView
from retype.games.steno import StenoView
from retype.controllers import SafeConfig, MenuController, LibraryController
from retype.console import Console
from retype.constants import iswindows
from retype.services.icon_set import Icons
from retype.services import (ChordMasteryProgress, ChordMasteryStorage,
                             DeviceSnapshotReader, DeviceStartupLoader,
                             snapshot_to_chords)
from retype.resource_handler import getIconsPath

logger = logging.getLogger(__name__)


class View(Enum):
    shelf_view = 1
    book_view = 2
    typespeed_view = 3
    steno_view = 4


class MainController(QObject):
    views = {}  # type: ViewsDict  # type: ignore[assignment]
    switchViewRequested = pyqtSignal(int)
    prevViewRequested = pyqtSignal()
    loadBookRequested = pyqtSignal(int)
    saveConfigRequested = pyqtSignal(dict)
    customisationDialogRequested = pyqtSignal()
    aboutDialogRequested = pyqtSignal(str)

    def __init__(self, config_dir=None, library_paths=None,
                 device_reader=None, device_reader_factory=None):
        # type: (MainController, str | None, list[str] | None, DeviceSnapshotReader | None, callable | None) -> None
        super().__init__()
        self.config = SafeConfig(config_dir, library_paths)
        if device_reader_factory is not None:
            self._device_reader_factory = device_reader_factory
        elif callable(device_reader):
            self._device_reader_factory = device_reader
        elif device_reader is not None:
            self._device_reader_factory = lambda: device_reader
        else:
            self._device_reader_factory = DeviceSnapshotReader
        self._device_loader = None  # type: DeviceStartupLoader | None
        self.chord_progress = ChordMasteryProgress(
            ChordMasteryStorage(self.config['user_dir']))
        # Keep view state local to a controller. This also makes multiple
        # isolated GUI runs in one QApplication deterministic.
        self.views = {}

        Icons.populateSets(
            getIconsPath(), getIconsPath(self.config['user_dir']))
        Icons.setIconSet(self.config['icon_set'])

        self.console = Console(
            self.config['prompt'], self.config['console_font'])
        self._window = MainWin(self.console, self.getGeometry(self.config))
        self._window.setObjectName('main-window')
        if iswindows:
            self.sysconsole_visible = True
            self._window.opened.connect(self.maybeHideConsoleWindow)

        self._view = None  # type: QWidget | None
        self._prev_view = None  # type: QWidget | None
        self.about_dialog = None  # type: AboutDialog | None

        self.switchViewRequested.connect(self.switchView)
        self.prevViewRequested.connect(self.prevView)
        self.loadBookRequested.connect(self.loadBook)
        self.saveConfigRequested.connect(self.saveConfig)
        self.customisationDialogRequested.connect(self.showCustomisationDialog)
        self.aboutDialogRequested.connect(self.showAboutDialog)

        self._window.closing.connect(self._stopDeviceChordLoad)

        self._initLibrary()
        self._initMenuBar()
        self._instantiateViews()
        self.setViewByEnum(View.shelf_view)
        self._connectConsole()
        self._populateLibrary()
        self._verifyUserDir()
        self._startDeviceChordLoad()

    def _setDeviceStatus(self, message, loading=False):
        # type: (MainController, str, bool) -> None
        logger.info("CharaChorder: %s", message)
        self._window.setBaseStatus(message)
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None:
            dialog.setChordLoadState(message, loading)

    def _deviceLoaderActive(self):
        # type: (MainController) -> bool
        return self._device_loader is not None

    def _createDeviceLoader(self):
        # type: (MainController) -> DeviceStartupLoader
        return DeviceStartupLoader(self._device_reader_factory())

    def _beginDeviceChordLoad(self, start_message):
        # type: (MainController, str) -> bool
        if self._deviceLoaderActive():
            self._setDeviceStatus("CharaChorder chord load already in progress.", True)
            return False
        self._setDeviceStatus(start_message, True)
        loader = self._createDeviceLoader()
        self._device_loader = loader
        loader.status.connect(lambda msg: self._setDeviceStatus(msg, True))
        loader.snapshotReady.connect(self._installDeviceSnapshot)
        loader.unavailable.connect(self._deviceChordLoadUnavailable)
        loader.cancelled.connect(self._deviceChordLoadCancelled)
        loader.failed.connect(self._deviceChordLoadFailed)
        loader.thread.finished.connect(
            lambda loader=loader: self._deviceChordLoadFinished(loader))
        loader.thread.finished.connect(lambda: logger.debug(
            "CharaChorder reader stopped"))
        loader.start()
        return True

    def _startDeviceChordLoad(self):
        # type: (MainController) -> None
        """Read once in a worker; BookView remains empty until it is complete."""
        if self.config['load_chords_on_startup']:
            self._beginDeviceChordLoad("Looking for a CharaChorder Two S3…")
        else:
            self._setDeviceStatus("CharaChorder startup loading is disabled.")

    def loadChordsNow(self):
        # type: (MainController) -> None
        self._beginDeviceChordLoad("Looking for a CharaChorder Two S3…")

    def _installDeviceSnapshot(self, snapshot):
        # type: (MainController, object) -> None
        """Cross the sole chord-map boundary only after a full read succeeds."""
        try:
            chords = snapshot_to_chords(snapshot)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Could not adapt complete CharaChorder snapshot")
            self._deviceChordLoadFailed(
                "CharaChorder data could not be used; chord features are unavailable")
            return
        self.views[View.book_view].setChords(chords)
        self._setDeviceStatus(
            "Loaded {} chord hints from CharaChorder Two S3 ({})".format(
                len(chords), snapshot.version))

    def _deviceChordLoadUnavailable(self, message):
        # type: (MainController, str) -> None
        self._setDeviceStatus(
            "CharaChorder chord hints unavailable: {}".format(message))

    def _deviceChordLoadCancelled(self):
        # type: (MainController) -> None
        self._setDeviceStatus("CharaChorder chord load cancelled.")

    def _deviceChordLoadFailed(self, message):
        # type: (MainController, str) -> None
        self._setDeviceStatus(
            "CharaChorder chord load failed: {}".format(message))

    def _deviceChordLoadFinished(self, loader):
        # type: (MainController, DeviceStartupLoader) -> None
        if self._device_loader is not loader:
            return
        self._device_loader = None
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None:
            dialog.setChordLoadState(self._window.baseStatus())

    def _stopDeviceChordLoad(self):
        # type: (MainController) -> None
        loader = self._device_loader
        if loader is not None and loader.thread.isRunning():
            loader.cancel()
            # A serial request has a bounded one-second timeout; waiting here
            # releases the port before Qt tears down the process.
            loader.thread.wait(1500)

    def _instantiateViews(self):
        # type: (MainController) -> None
        self.views[View.shelf_view] = ShelfView(self._window, self)

        sdict = self.config['sdict']
        rdict = self.config['rdict']
        bookview_settings = self.config['bookview']
        self.views[View.book_view] = BookView(
            self._window, self, sdict, rdict, bookview_settings, {},
            chord_progress=self.chord_progress,
            adaptive_chord_lessons=self.config['adaptive_chord_lessons'])

        self.customisation_dialog = CustomisationDialog(
            self.config.raw, self._window,
            self.saveConfigRequested, self.prevViewRequested,
            lambda: self.views[View.book_view].font_size,
            self._window)
        self.customisation_dialog.loadChordsNowRequested.connect(
            self.loadChordsNow)
        self.customisation_dialog.setChordLoadState(
            "Ready to load chords from CharaChorder.")

    def _viewFromEnumOrInt(self, view):
        # type: (MainController, View | int) -> QWidget
        if isinstance(view, View):
            return self.views[view]
        elif isinstance(view, int):
            return self.views[View(view)]
        else:
            logger.error(  # type: ignore[unreachable]
                f"Improper view identifier {view}")

    def _setView(self, view):
        # type: (MainController, QWidget) -> None
        """Brings the view instance to the fore"""
        self._window.stacker.addWidget(view)
        self._window.stacker.setCurrentWidget(view)

    def setView(self, view):
        # type: (MainController, QWidget) -> None
        book_view = self.views.get(View.book_view)
        if book_view is not None:
            book_view.resetSessionStatistics()
        self.console.clear()

        if view is self._view:
            return
        self._prev_view = self._view
        self._setView(view)
        self._view = view

    def setViewByEnum(self, view_e=View.shelf_view):
        # type: (MainController, View | int) -> None
        self.setView(self._viewFromEnumOrInt(view_e))

    def view(self):
        # type: (MainController) -> QWidget | None
        return self._view

    def isVisible(self, view):
        # type: (MainController, View) -> bool
        return self._viewFromEnumOrInt(view).isVisible()

    def switchView(self, view=None):
        # type: (MainController, View | int | None) -> None
        # If no argument (or 0), switch to shelf view if not on it, otherwise
        #  switch to book view
        if not view:
            view = View.shelf_view if not self.isVisible(View.shelf_view)\
                else View.book_view
        elif view == 3:
            self.showTypespeed()
        elif view == 4:
            self.showSteno()
        self.setViewByEnum(view)

    def showCustomisationDialog(self):
        # type: (MainController) -> None
        self.customisation_dialog.show()

    def prevView(self):
        # type: (MainController) -> None
        if self._prev_view:
            self.setView(self._prev_view)

    def show(self):
        # type: (MainController) -> None
        self._window.show()

    def _initMenuBar(self):
        # type: (MainController) -> None
        menu = self._window.menuBar()
        self._menu_controller = MenuController(self, menu)

    def quit(self):
        # type: (MainController) -> None
        self._window.close()

    def _initLibrary(self):
        # type: (MainController) -> None
        self.library = LibraryController(self.config['user_dir'],
                                         self.config['library_paths'])

    def _populateLibrary(self):
        # type: (MainController) -> None
        """Instantiate all the book wrappers and shelf items"""
        shelf_view = self.views[View.shelf_view]  # type: ShelfView
        self.library.instantiateBooks()
        shelf_view._populate()

    def _repopulateLibrary(self, user_dir, library_paths):
        # type: (MainController, str, list[str]) -> None
        self.library.__init__(user_dir, library_paths)  # type: ignore[misc]
        shelf_view = self.views[View.shelf_view]
        self.library.instantiateBooks()
        shelf_view.repopulate()

    def loadBook(self, book_id=0):
        # type: (MainController, int) -> None
        book_view = self.views[View.book_view]
        self.library.setBook(book_id, book_view, self.switchViewRequested)

    def _connectConsole(self):
        # type: (MainController) -> None
        """Pass some signals console services need access to"""
        self.console.initServices(self.views[View.book_view],
                                  self.switchViewRequested,
                                  self.loadBookRequested,
                                  self.customisationDialogRequested,
                                  self.aboutDialogRequested,
                                  self.config['auto_newline'])

    def _verifyUserDir(self):
        # type: (MainController) -> None
        user_dir = self.config['user_dir']
        if not os.path.exists(user_dir):
            msg = QMessageBox(
                QMessageBox.Icon.Warning, 'retype', f'User dir \'{user_dir}\'\
 cannot be found.\nretype will not be able to save and load progress and\
 configuration.')
            msg.addButton(QMessageBox.StandardButton.Ignore)
            change_btn = msg.addButton(
                'Change', QMessageBox.ButtonRole.ActionRole)
            msg.exec()
            if msg.clickedButton() == change_btn:
                self.showCustomisationDialog()

    def saveConfig(self, config_dict):
        # type: (MainController, NestedDict) -> None
        self.config.populate(config_dict)
        self.config.save()
        config = self.config

        # Repopulate library if paths changed
        if config['library_paths'] != self.library.library_paths:
            logger.debug("Repopulating library")
            self._repopulateLibrary(config['user_dir'],
                                    config['library_paths'])

        # Update prompt if changed
        if config['prompt'] != self.console.prompt:
            self.console.prompt = config['prompt']

        # Update sdict
        self.views[View.book_view].setSdict(config['sdict'])

        # Update rdict
        self.views[View.book_view].setRdict(config['rdict'])

        # Device data is read once at startup and is intentionally not
        # replaced by configuration saves.
        book_view = self.views[View.book_view]
        book_view.setAdaptiveChordLessons(config['adaptive_chord_lessons'])
        if self.chord_progress.storage.path != os.path.join(
                config['user_dir'], 'chord-mastery.json'):
            self.chord_progress = ChordMasteryProgress(
                ChordMasteryStorage(config['user_dir']))
            book_view.setChordProgress(self.chord_progress)

        # Update steno kdict
        if View.steno_view in self.views:
            self.views[View.steno_view].setKdict(config['steno']['kdict'])

        # Update auto_newline
        hs = self.console.highlighting_service
        if hs:
            hs.setAutoNewline(config['auto_newline'])

        # Update library’s user_dir
        self.library.user_dir = config['user_dir']

        # Update book display font
        if not config['bookview']['save_font_size_on_quit']:
            self.views[View.book_view].font_size = \
                config['bookview']['font_size']
        self.views[View.book_view].font_family = config['bookview']['font']

        # Update console font
        self.console.font_family = config['console_font']

    def getGeometry(self, config):
        # type: (MainController, SConfig) -> Geometry
        return config['window']

    def openUrl(self, url):
        # type: (MainController, QUrl | str) -> None
        if isinstance(url, QUrl):
            QDesktopServices.openUrl(url)
        else:
            QDesktopServices.openUrl(QUrl(url))

    def showAboutDialog(self, page_title=None):
        # type: (MainController, str | None) -> None
        cs = self.console.command_service
        if self.about_dialog is None and cs is not None:
            self.about_dialog = AboutDialog(
                cs.commands_info,
                self.config['prompt'], self.library.books, self._window)
        if isinstance(self.about_dialog, AboutDialog):
            self.about_dialog.show()
            if page_title:
                self.about_dialog.setActivePage(page_title)

    def showTypespeed(self):
        # type: (MainController) -> None
        i = View.typespeed_view
        if i not in self.views:
            self.views[i] = TypespeedView(
                self._window, self, self.config['bookview'],
                self.config['user_dir'])
        self.setView(self._viewFromEnumOrInt(i))

    def showSteno(self):
        # type: (MainController) -> None
        i = View.steno_view
        if i not in self.views:
            self.views[i] = StenoView(
                self._window, self, self.config['bookview'],
                self.config['steno']['kdict'])
        self.setView(self._viewFromEnumOrInt(i))

    if iswindows:
        def hideConsoleWindow(self, show=False):
            # type: (MainController, bool) -> None
            try:
                from win32gui import ShowWindow
                from win32con import SW_HIDE, SW_RESTORE
                from win32console import (GetConsoleWindow,
                                          GetConsoleProcessList)
                con = GetConsoleWindow()  # type: int
                if con == 0:
                    logger.info("No attached console window")
                    return
                if len(GetConsoleProcessList()) == 1:
                    ShowWindow(con, SW_RESTORE if show else SW_HIDE)
                    self.sysconsole_visible = show
                else:
                    logger.info("retype does not own the attached console")
            except ImportError:
                logger.info("No pywin32")

        def toggleConsoleWindow(self):
            # type: (MainController) -> None
            self.hideConsoleWindow(not self.sysconsole_visible)

        def maybeHideConsoleWindow(self):
            # type: (MainController) -> None
            hide = self.config['hide_sysconsole']
            if hide:
                self.hideConsoleWindow()


if TYPE_CHECKING:
    from qt import QWidget  # noqa: F401
    from retype.extras.metatypes import (  # noqa: F401
        NestedDict, Config, Geometry, SConfig, ViewsDict)
