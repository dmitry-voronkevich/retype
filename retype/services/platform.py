import sys
from qt import QApplication, QAction, QKeySequence, Qt


class PlatformPolicy:
    def __init__(self, platform_name=None):
        # type: (str | None) -> None
        self._platform_name = (platform_name or sys.platform).lower()

    @property
    def is_macos(self):
        # type: (PlatformPolicy) -> bool
        return 'darwin' in self._platform_name

    @property
    def is_windows(self):
        # type: (PlatformPolicy) -> bool
        return self._platform_name.startswith('win')

    @property
    def native_menu_bar(self):
        # type: (PlatformPolicy) -> bool
        return self.is_macos

    @property
    def shortcut_modifier_label(self):
        # type: (PlatformPolicy) -> str
        return 'Command' if self.is_macos else 'Ctrl'

    @property
    def shortcut_modifier_key(self):
        # type: (PlatformPolicy) -> str
        # Qt maps Ctrl shortcuts to Command on macOS. Meta would produce
        # Control shortcuts instead.
        return 'Ctrl'

    @property
    def shortcut_modifier(self):
        # type: (PlatformPolicy) -> Qt.KeyboardModifier
        return Qt.KeyboardModifier.ControlModifier

    def modified_shortcut(self, key):
        # type: (PlatformPolicy, str) -> str
        return f'{self.shortcut_modifier_key}+{key}'

    def has_shortcut_modifier(self, modifiers):
        # type: (PlatformPolicy, Qt.KeyboardModifiers) -> bool
        return modifiers == Qt.KeyboardModifiers(self.shortcut_modifier)

    def quit_shortcuts(self):
        # type: (PlatformPolicy) -> list[object]
        shortcuts = [QKeySequence.StandardKey.Quit]
        if self.is_windows:
            shortcuts.append('Alt+F4')
        return shortcuts

    def preferences_shortcuts(self):
        # type: (PlatformPolicy) -> list[object]
        if self.is_macos:
            return [QKeySequence.StandardKey.Preferences]
        return ['Ctrl+O']

    def visible_shortcut(self, shortcut):
        # type: (PlatformPolicy, object) -> str
        return _shortcut_to_text(shortcut)

    def visible_shortcuts(self, shortcuts):
        # type: (PlatformPolicy, list[object]) -> str
        return ' / '.join(
            text for text in (self.visible_shortcut(s) for s in shortcuts)
            if text)

    def apply_application_defaults(self, app):
        # type: (PlatformPolicy, QApplication) -> None
        app.setApplicationName('retype')
        app.setApplicationDisplayName('retype')
        app.setOrganizationName('retype')
        app.setOrganizationDomain('retype.readthedocs.io')
        if self.is_macos:
            app.setQuitOnLastWindowClosed(False)
            app.setStyle('Fusion')

    def menu_role(self, role):
        # type: (PlatformPolicy, QAction.MenuRole) -> QAction.MenuRole
        if self.is_macos:
            return role
        return QAction.MenuRole.NoRole


platform_policy = PlatformPolicy()


def _shortcut_to_text(shortcut):
    # type: (object) -> str
    if shortcut is None:
        return ''
    if isinstance(shortcut, QKeySequence):
        sequence = shortcut
    else:
        sequence = QKeySequence(shortcut)
    return sequence.toString(QKeySequence.NativeText)


def shortcut_to_text(shortcut):
    # type: (object) -> str
    return _shortcut_to_text(shortcut)


def shortcut_texts(shortcuts):
    # type: (list[object]) -> list[str]
    return [_shortcut_to_text(shortcut) for shortcut in shortcuts]


