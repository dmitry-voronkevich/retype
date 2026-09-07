from qt import QAction, QKeySequence, Qt

from retype.extras.actions import makeAction
from retype.services.keymap import K, Keymap
from retype.services.platform import PlatformPolicy, shortcut_to_text


def test_platform_policy_switches_mac_defaults():
    policy = PlatformPolicy('darwin')

    assert policy.is_macos
    assert not policy.is_windows
    assert policy.native_menu_bar
    assert policy.shortcut_modifier_label == 'Command'
    assert policy.quit_shortcuts() == [QKeySequence.StandardKey.Quit]
    assert policy.preferences_shortcuts() == [QKeySequence.StandardKey.Preferences]
    assert policy.menu_role(QAction.MenuRole.AboutRole) == QAction.MenuRole.AboutRole


def test_platform_policy_uses_native_shortcut_modifier():
    mac_policy = PlatformPolicy('darwin')
    linux_policy = PlatformPolicy('linux')
    mac_modifiers = Qt.KeyboardModifiers(Qt.KeyboardModifier.MetaModifier)

    assert mac_policy.modified_shortcut('PgUp') == 'Meta+PgUp'
    assert mac_policy.has_shortcut_modifier(mac_modifiers)
    assert linux_policy.modified_shortcut('PgUp') == 'Ctrl+PgUp'


def test_platform_policy_keeps_non_mac_defaults():
    policy = PlatformPolicy('win32')

    assert policy.is_windows
    assert not policy.is_macos
    assert not policy.native_menu_bar
    assert policy.shortcut_modifier_label == 'Ctrl'
    assert policy.quit_shortcuts() == [QKeySequence.StandardKey.Quit, 'Alt+F4']
    assert policy.preferences_shortcuts() == ['Ctrl+O']
    assert policy.menu_role(QAction.MenuRole.AboutRole) == QAction.MenuRole.NoRole


def test_keymap_values_are_serialised_to_native_text(monkeypatch):
    selector = K([QKeySequence.StandardKey.Quit, 'Ctrl+O'])
    monkeypatch.setattr(Keymap, 'selectors', {'Menu.quit': selector})

    values = Keymap.getValuesDict()

    assert values == {'Menu.quit': {'': [shortcut_to_text(QKeySequence.StandardKey.Quit),
                                        shortcut_to_text('Ctrl+O')]}}
    assert all(isinstance(item, str) for item in values['Menu.quit'][''])


def test_make_action_applies_menu_role_and_shortcuts():
    action = makeAction(
        name='Preferences…',
        menu_role=QAction.MenuRole.PreferencesRole,
        shortcuts=[QKeySequence.StandardKey.Preferences],
    )

    assert action.menuRole() == QAction.MenuRole.PreferencesRole
    assert action.shortcut().toString(QKeySequence.NativeText) != ''
