import os
import logging
from copy import deepcopy
from enum import Enum
from qt import (QApplication, QObject, pyqtSignal, QUrl, QDesktopServices,
                QMessageBox, QThread, QTimer)
from typing import TYPE_CHECKING

from retype.ui import (MainWin, ShelfView, BookView, CustomisationDialog,
                       AboutDialog)
from retype.games.typespeed import TypespeedView
from retype.games.steno import StenoView
from retype.controllers import SafeConfig, MenuController, LibraryController
from retype.controllers.library import BookWrapper, is_valid_managed_book
from retype.console import Console
from retype.services.icon_set import Icons
from retype.services.platform import platform_policy
from retype.services import (ChordMasteryProgress, ChordMasteryStorage,
                             DeviceSnapshotReader, DeviceStartupLoader,
                             LearningSync, SyncError, SyncResult,
                             apply_learning_settings, snapshot_to_chords)
from retype.resource_handler import getApplicationDataPath, getIconsPath

logger = logging.getLogger(__name__)


class View(Enum):
    shelf_view = 1
    book_view = 2
    typespeed_view = 3
    steno_view = 4


class _SyncWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(self, sync):
        # type: (_SyncWorker, LearningSync) -> None
        QThread.__init__(self)
        self.sync = sync

    def run(self):
        # type: (_SyncWorker) -> None
        self.completed.emit(self.sync.sync_now())


class _ManagedLibraryLoadWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(self, load_data):
        # type: (_ManagedLibraryLoadWorker, list[tuple[object, object]]) -> None
        QThread.__init__(self)
        self.load_data = load_data

    def run(self):
        books = {}
        for item, save_data in self.load_data:
            if not is_valid_managed_book(item.path, item.checksum):
                continue
            book = BookWrapper(item, save_data, report_errors=False)
            books[item.idn] = book
        self.completed.emit(books)


class _ManagedBookImportWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, sync, path):
        # type: (_ManagedBookImportWorker, LearningSync, str) -> None
        QThread.__init__(self)
        self.sync = sync
        self.path = path

    def run(self):
        # type: (_ManagedBookImportWorker) -> None
        try:
            metadata = self.sync.import_book(self.path)
            loaded_book = self.sync.take_loaded_managed_book(
                str(metadata['digest']))
            if loaded_book is None:
                raise SyncError('managed EPUB could not be loaded')
            self.completed.emit((metadata, loaded_book))
        except SyncError as error:
            self.failed.emit(str(error))
        except Exception as error:
            logger.exception("Managed EPUB import failed")
            self.failed.emit('managed EPUB import failed: {}'.format(error))


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
        # Tests and embedders that pass a config root get an equally isolated
        # bootstrap root; installed apps always use per-user application data.
        bootstrap_root = config_dir or getApplicationDataPath()
        self.learning_sync = LearningSync(bootstrap_root,
                                          self.config['user_dir'])
        self._sync_worker = None  # type: _SyncWorker | None
        self._managed_book_worker = None  # type: _ManagedBookImportWorker | None
        self._managed_library_worker = None  # type: _ManagedLibraryLoadWorker | None
        self._sync_pending = False
        self._sync_retry_timer = QTimer(self)
        self._sync_retry_timer.setSingleShot(True)
        self._sync_retry_timer.timeout.connect(self._retrySync)
        self._sync_retry_delay = 1000
        self._sync_closing = False
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
            ChordMasteryStorage(self.config['user_dir'], self._recordSyncChords))
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
        if platform_policy.is_windows:
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
        self._window.closing.connect(self._syncOnClosing)

        self._initLibrary()
        self._initMenuBar()
        self._instantiateViews()
        self.setViewByEnum(View.shelf_view)
        self._connectConsole()
        self._populateLibrary()
        self._verifyUserDir()
        self._startDeviceChordLoad()
        self.requestSync()

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
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None and hasattr(dialog, 'chord_mastery'):
            dialog.chord_mastery.refresh()
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
            adaptive_chord_lessons=self.config['adaptive_chord_lessons'],
            adaptive_chord_lesson_limit=self.config[
                'adaptive_chord_lesson_limit'])

        self.customisation_dialog = CustomisationDialog(
            self.config.raw, self._window,
            self.saveConfigRequested, self.prevViewRequested,
            lambda: self.views[View.book_view].font_size,
            self._window,
            getLoadedChords=lambda: self.views[View.book_view].loaded_chords,
            chordProgress=self.chord_progress,
            syncActions=self)
        self.customisation_dialog.loadChordsNowRequested.connect(
            self.loadChordsNow)
        self.customisation_dialog.saveChordMasteryRequested.connect(
            self.saveChordMastery)
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
        menu.setNativeMenuBar(platform_policy.native_menu_bar)
        self._menu_controller = MenuController(self, menu)

    def quit(self):
        # type: (MainController) -> None
        self._window.close()
        if platform_policy.is_macos:
            QApplication.quit()

    def _initLibrary(self):
        # type: (MainController) -> None
        self.library = LibraryController(
            self.config['user_dir'], self.config['library_paths'],
            str(self.learning_sync.managed_library_dir), self._recordSyncBook)

    def _populateLibrary(self):
        # type: (MainController) -> None
        shelf_view = self.views[View.shelf_view]  # type: ShelfView
        self.library.instantiateBooks(include_managed=False)
        shelf_view._populate()
        self._startManagedLibraryLoad()

    def _startManagedLibraryLoad(self):
        # type: (MainController) -> None
        if self._managed_library_worker is not None and \
                self._managed_library_worker.isRunning():
            return
        worker = _ManagedLibraryLoadWorker(self.library.managedBookLoadData())
        self._managed_library_worker = worker
        worker.completed.connect(self._managedLibraryLoadCompleted)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _managedLibraryLoadCompleted(self, books):
        # type: (MainController, dict[int, BookWrapper]) -> None
        self._managed_library_worker = None
        installed = self.library.installManagedBooks(books)
        self.views[View.shelf_view].addBooks(installed)

    def _repopulateLibrary(self, user_dir, library_paths):
        # type: (MainController, str, list[str]) -> None
        managed_worker = self._managed_library_worker
        if managed_worker is not None and managed_worker.isRunning():
            managed_worker.wait()
        self.library.__init__(  # type: ignore[misc]
            user_dir, library_paths, str(self.learning_sync.managed_library_dir),
            self._recordSyncBook)
        shelf_view = self.views[View.shelf_view]
        self.library.instantiateBooks(include_managed=False)
        shelf_view.repopulate()
        self._startManagedLibraryLoad()

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
        previous_config = deepcopy(self.config.raw)
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
        book_view.setAdaptiveChordLessonLimit(
            config['adaptive_chord_lesson_limit'])
        if self.chord_progress.storage.path != os.path.join(
                config['user_dir'], 'chord-mastery.json'):
            self.chord_progress = ChordMasteryProgress(
                ChordMasteryStorage(config['user_dir'], self._recordSyncChords))
            book_view.setChordProgress(self.chord_progress)
            dialog = getattr(self, 'customisation_dialog', None)
            if dialog is not None:
                dialog.chordProgress = self.chord_progress
                if hasattr(dialog, 'chord_mastery'):
                    dialog.chord_mastery.setProgress(self.chord_progress)

        # Update steno kdict
        if View.steno_view in self.views:
            self.views[View.steno_view].setKdict(config['steno']['kdict'])

        # Update auto_newline
        hs = self.console.highlighting_service
        if hs:
            hs.setAutoNewline(config['auto_newline'])

        # Update library’s user_dir
        self.library.user_dir = config['user_dir']
        self.learning_sync.set_legacy_dir(config['user_dir'])
        try:
            self.learning_sync.record_settings(config.raw, previous_config)
        except OSError as error:
            self._syncMutationFailed(error)
        self._scheduleSync()

        # Update book display font
        if not config['bookview']['save_font_size_on_quit']:
            self.views[View.book_view].font_size = \
                config['bookview']['font_size']
        self.views[View.book_view].font_family = config['bookview']['font']

        # Update console font
        self.console.font_family = config['console_font']

    def _syncMutationFailed(self, error):
        # type: (MainController, OSError) -> None
        self.learning_sync._diagnose(
            'Local pending sync state could not be written: {}'.format(error))
        self.learning_sync.status.message = (
            'Local sync state could not be saved; retrying.')
        self._updateSyncPresentation()

    def _recordSyncBook(self, identity, data):
        # type: (MainController, str, dict) -> None
        try:
            self.learning_sync.record_book(identity, data)
        except OSError as error:
            self._syncMutationFailed(error)
        self._scheduleSync()

    def _recordSyncChords(self, progress, overrides):
        # type: (MainController, dict[str, int], dict[str, bool]) -> None
        try:
            self.learning_sync.record_chords(progress, overrides)
        except OSError as error:
            self._syncMutationFailed(error)
        self._scheduleSync()

    def sync_status(self):
        # type: (MainController) -> object
        return self.learning_sync.status

    def enableSync(self, folder, managed_library_consent=False):
        # type: (MainController, str, bool) -> object
        status = self.learning_sync.configure(folder, managed_library_consent)
        if status.state == 'ready':
            # A first-run config can still be in memory rather than on disk.
            self.learning_sync.record_settings(
                self.config.raw, self.learning_sync.settings_baseline())
            self.requestSync()
        self._updateSyncPresentation()
        return status

    def setManagedLibraryConsent(self, consent):
        # type: (MainController, bool) -> object
        status = self.learning_sync.set_managed_library_consent(consent)
        if consent:
            self.requestSync()
        self._updateSyncPresentation()
        return status

    def disableSync(self):
        # type: (MainController) -> object
        status = self.learning_sync.disable()
        self._updateSyncPresentation()
        return status

    def importManagedBook(self, path):
        # type: (MainController, str) -> None
        worker = self._managed_book_worker
        if self._sync_closing or (worker is not None and worker.isRunning()):
            return
        self.learning_sync.status.message = 'Importing the selected EPUB…'
        worker = _ManagedBookImportWorker(self.learning_sync, path)
        self._managed_book_worker = worker
        worker.completed.connect(self._managedBookImportCompleted)
        worker.failed.connect(self._managedBookImportFailed)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._updateSyncPresentation()

    def _managedBookImportCompleted(self, result):
        # type: (MainController, tuple[dict[str, object], object]) -> None
        self._managed_book_worker = None
        metadata, loaded_book = result
        added = self.library.addManagedBooks(
            {metadata['digest']: metadata}, {metadata['digest']},
            {metadata['digest']: loaded_book})
        self.views[View.shelf_view].addBooks(added)
        self.learning_sync.status.message = (
            'The EPUB was added to the managed library.')
        self.requestSync()
        self._updateSyncPresentation()

    def _managedBookImportFailed(self, message):
        # type: (MainController, str) -> None
        self._managed_book_worker = None
        self.learning_sync.status.message = message
        self._updateSyncPresentation()

    def requestSync(self):
        # type: (MainController) -> None
        """Run provider-folder I/O outside the Qt/typing event path."""
        if self._sync_retry_timer.isActive():
            self._sync_retry_timer.stop()
        if not self.learning_sync.enabled or self._sync_closing:
            return
        if self._sync_worker is not None and self._sync_worker.isRunning():
            self._sync_pending = True
            return
        self.learning_sync.status.message = 'Checking the selected sync folder…'
        worker = _SyncWorker(self.learning_sync)
        self._sync_worker = worker
        worker.completed.connect(self._syncCompleted)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._updateSyncPresentation()

    def _retrySync(self):
        if self.learning_sync.enabled and not self._sync_closing:
            self.requestSync()

    def _scheduleSyncRetry(self):
        if self._sync_closing or not self.learning_sync.enabled:
            return
        if not self._sync_retry_timer.isActive():
            self._sync_retry_timer.start(self._sync_retry_delay)
            self._sync_retry_delay = min(self._sync_retry_delay * 2, 30000)

    def _scheduleSync(self):
        # type: (MainController) -> None
        if self.learning_sync.enabled:
            QTimer.singleShot(1000, self.requestSync)

    def _applySyncedSettingsToLiveViews(self):
        # type: (MainController) -> None
        book_view = self.views.get(View.book_view)
        if book_view is not None:
            book_view.setSdict(self.config['sdict'])
            book_view.setRdict(self.config['rdict'])
            book_view.setAdaptiveChordLessons(
                self.config['adaptive_chord_lessons'])
            book_view.setAdaptiveChordLessonLimit(
                self.config['adaptive_chord_lesson_limit'])
        if View.steno_view in self.views:
            self.views[View.steno_view].setKdict(self.config['steno']['kdict'])
        highlighting = self.console.highlighting_service
        if highlighting:
            highlighting.setAutoNewline(self.config['auto_newline'])
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None and hasattr(dialog, 'applyExternalConfig'):
            dialog.applyExternalConfig(self.config.raw)

    def _syncCompleted(self, result):
        # type: (MainController, object) -> None
        waiting_for_provider = isinstance(result, SyncResult) and \
            result.status.state == 'waiting'
        if isinstance(result, SyncResult):
            book_view = self.views.get(View.book_view) \
                if hasattr(self, 'views') else None
            active_checksum = getattr(getattr(book_view, 'book', None),
                                      'checksum', None)
            if book_view is not None:
                book_view.maybeSave()
            if result.settings:
                updated = apply_learning_settings(self.config.raw, result.settings)
                if updated != self.config.raw:
                    self.config.populate(updated)
                    self.config.save()
                    self._applySyncedSettingsToLiveViews()
            merged_save_changed = set()
            if result.save and hasattr(self, 'library'):
                merged_save_changed = self.library.applyMergedSave(result.save)
            if result.managed_books and hasattr(self, 'library') and \
                    self.learning_sync.managed_library_consent:
                added = self.library.addManagedBooks(
                    result.managed_books, result.managed_books_ready,
                    result.managed_books_loaded)
                if added:
                    self.views[View.shelf_view].addBooks(added)
            if result.status.state == 'synced':
                self.chord_progress.apply_merged(
                    result.chord_counts, result.chord_overrides)
                if hasattr(self, 'views') and View.book_view in self.views:
                    self.views[View.book_view].setChordProgress(self.chord_progress)
                    dialog = getattr(self, 'customisation_dialog', None)
                    if dialog is not None:
                        dialog.chordProgress = self.chord_progress
                        if hasattr(dialog, 'chord_mastery'):
                            dialog.chord_mastery.setProgress(self.chord_progress)
                if active_checksum in merged_save_changed:
                    book = next((item for item in self.library.books.values()
                                 if item.checksum == active_checksum), None) \
                        if self.library.books else None
                    if book is not None:
                        self.views[View.book_view].setBook(book, book.save_data)
        self._updateSyncPresentation()
        self._sync_worker = None
        if waiting_for_provider:
            self._scheduleSyncRetry()
            self._sync_pending = False
        elif self._sync_pending or self.learning_sync.has_deferred_changes:
            self._sync_pending = False
            self._sync_retry_delay = 1000
            self.requestSync()
        else:
            self._sync_retry_delay = 1000

    def _updateSyncPresentation(self):
        # type: (MainController) -> None
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None and hasattr(dialog, 'setSyncState'):
            dialog.setSyncState(self.learning_sync.status)

    def _syncOnClosing(self):
        # type: (MainController) -> None
        self._sync_closing = True
        self._sync_retry_timer.stop()
        book_view = self.views.get(View.book_view) \
            if hasattr(self, 'views') else None
        if book_view is not None:
            book_view.maybeSave()
        worker = self._sync_worker
        if worker is not None and worker.isRunning():
            worker.wait()
        import_worker = self._managed_book_worker
        if import_worker is not None and import_worker.isRunning():
            import_worker.wait()
        library_worker = self._managed_library_worker
        if library_worker is not None and library_worker.isRunning():
            library_worker.wait()
        if self.learning_sync.enabled:
            # This is a local-folder write attempt, not a claim that a cloud
            # provider has uploaded it to any other device.
            self.learning_sync.sync_now()

    def saveChordMastery(self, overrides):
        # type: (MainController, dict[str, bool]) -> None
        saved = self.chord_progress.set_manual_overrides(overrides)
        dialog = getattr(self, 'customisation_dialog', None)
        if dialog is not None and hasattr(dialog, 'chord_mastery'):
            if saved:
                dialog.chord_mastery.setSaveSucceeded()
            else:
                dialog.chord_mastery.setSaveFailed()
        if saved:
            book_view = self.views[View.book_view]
            book_view.setChordProgress(self.chord_progress)

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

    if platform_policy.is_windows:
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
