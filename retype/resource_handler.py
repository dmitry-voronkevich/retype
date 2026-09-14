import os
import sys
from pathlib import Path
from qt import QIcon

from retype.services.icon_set import Icons


_frozen = getattr(sys, 'frozen', False)  # type: ignore[misc]
_meipass = getattr(sys, '_MEIPASS', False)  # type: ignore[misc]


def _root_from_runtime(frozen, meipass, executable, module_file, argv0):
    # type: (bool, object, str, str | None, str) -> str
    """Resolve the old data root without depending on import-time globals.

    Before per-user data directories, both source runs and frozen applications
    stored ``config.json`` beside their runtime root.  Keeping this resolution
    explicit lets the migration use the same old location for either launch.
    """
    if frozen or meipass:
        return os.path.abspath(os.path.dirname(executable))
    source_file = module_file or argv0
    return os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(source_file)), '..'))


def __getRoot():
    # type: () -> str
    return _root_from_runtime(
        bool(_frozen), _meipass, sys.executable,
        globals().get('__file__'), sys.argv[0])


root_path = __getRoot()
temp_path_or_none = str(_meipass) if _meipass else None  # type: ignore[misc]


def getApplicationDataPath():
    # type: () -> str
    """Return a writable, per-user root independent of the app bundle.

    In a frozen macOS app ``sys.executable`` is inside ``.app/Contents`` and
    is not a reliable place for saves.  Keep this small platform helper free
    of QApplication lifetime/order requirements so it is also safe during
    early configuration imports.
    """
    home = Path.home()
    if sys.platform.lower().startswith('darwin'):
        return str(home / 'Library' / 'Application Support' / 'retype')
    if sys.platform.lower().startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        return str(Path(base) / 'retype') if base else str(home / 'AppData' / 'Local' / 'retype')
    base = os.environ.get('XDG_DATA_HOME')
    return str(Path(base) / 'retype') if base else str(home / '.local' / 'share' / 'retype')


def getLibraryPath():
    # type: () -> str
    return os.path.join(root_path, 'library')


def getStylePath(user_dir=None):
    # type: (str | None) -> str
    return os.path.join(user_dir if user_dir else root_path, 'style')


def getIconsPath(user_dir=None):
    # type: (str | None) -> str
    return os.path.join(getStylePath(user_dir), 'icons')


def getIcon(icon_name, extension='png'):
    # type: (str, str) -> QIcon
    return QIcon(Icons.getIconPath(f'{icon_name}.{extension}'))


def getTypespeedWordsPath(user_dir=None):
    # type: (str | None) -> str
    return os.path.join(getStylePath(user_dir), 'typespeed_words')


def getIncludePath():
    # type: () -> str
    root = temp_path_or_none or root_path
    if os.path.split(root)[1] == 'include':
        root = os.path.abspath(os.path.join(root, '..'))
    return os.path.join(root, 'include')
