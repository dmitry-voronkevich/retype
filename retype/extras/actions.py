from qt import QAction

from typing import TYPE_CHECKING

from retype.resource_handler import getIcon


def makeAction(name=None,  # type: str | None
               func=None,  # type: Callable[[], None] | None
               tooltip=None,  # type: str | None
               shortcuts=None,  # type: list[object] | None
               icon=None,  # type: str | None
               widget=None,  # type: QWidget | None
               menu_role=None,  # type: QAction.MenuRole | None
               **_
               ):
    # type: (...) -> QAction
    action = QAction(name)
    if func:
        action.triggered.connect(func)
    if tooltip:
        action.setToolTip(tooltip)
    if shortcuts:
        action.setShortcuts(shortcuts)
    if icon:
        action.setIcon(getIcon(icon))
    if menu_role is not None:
        action.setMenuRole(menu_role)
    if widget:
        widget.addAction(action)
    return action


if TYPE_CHECKING:
    from qt import QWidget  # noqa: F401
    from typing import Callable  # noqa: F401
