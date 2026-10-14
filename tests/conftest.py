"""Process-wide Qt cleanup for tests that create widgets without fixtures."""

from qt import QApplication, QEvent, QTimer


def pytest_sessionfinish(session, exitstatus):
    """Stop top-level Qt resources before the interpreter tears Qt down."""
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        for timer in widget.findChildren(QTimer):
            timer.stop()
    top_levels = list(app.topLevelWidgets())
    app.closeAllWindows()
    for widget in top_levels:
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.quit()
    app.processEvents()
