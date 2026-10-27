"""Indexed, session-local chord outcomes over an otherwise unchanged chapter.

Index source words once. Cursor movement visits only crossed word ends, and a
validated result recolors only its token. Lesson changes diff dictionary keys;
they do not remove and reapply the chapter's existing underline formats.
"""
from bisect import bisect_right

from qt import QColor, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat

from retype.services.chord_detection import ValidatedChord
from retype.services.chords import WORD_RE


class ChordHighlighting:
    def __init__(self, document):
        # type: (QTextDocument) -> None
        self.document = document
        self.document.contentsChange.connect(self._documentChanged)
        self.text = document.toPlainText()
        self.words = {match.start(): match for match in
                      WORD_RE.finditer(self.text)}
        self.starts = list(self.words)
        self.ends = [match.end() for match in self.words.values()]
        self.by_key = {}  # type: dict[str, list[int]]
        self.qt_starts = {}  # type: dict[int, int]
        source_end = qt_end = 0
        for start, match in self.words.items():
            # Book progress uses Python offsets; QTextCursor uses UTF-16.
            # WORD_RE tokens are ASCII, but intervening prose need not be.
            qt_start = qt_end + len(
                self.text[source_end:start].encode('utf-16-le')) // 2
            self.qt_starts[start] = qt_start
            source_end, qt_end = match.end(), qt_start + len(match.group())
            self.by_key.setdefault(match.group().lower(), []).append(start)
        self.active_keys = set()  # type: set[str]
        self.original = {}  # type: dict[int, list[QTextCharFormat]]
        self.outcomes = {}  # type: dict[int, bool]
        self.colors = None  # type: tuple[QColor, QColor] | None
        self.line_start = None  # type: int | None
        self.cursor = None  # type: int | None

    def _documentChanged(self, position, chars_removed, chars_added):
        # type: (int, int, int) -> None
        delta = chars_added - chars_removed
        if not delta:
            return
        boundary = position + chars_removed
        for start, qt_start in self.qt_starts.items():
            if qt_start >= boundary:
                self.qt_starts[start] = qt_start + delta

    def refresh(self, chords, green, yellow):
        # type: (dict[str, object], QColor, QColor) -> None
        keys = set(chords)
        colors = (green, yellow)
        removed = self.active_keys - keys
        added = keys - self.active_keys
        recolor = self.active_keys & keys if colors != self.colors else set()
        self.colors = colors
        for key in removed:
            for start in self.by_key.get(key, ()):
                self._restore(start)
        self.active_keys = keys
        for key in added:
            for start in self.by_key.get(key, ()):
                self._capture(start)
                self._paint(start)
        for key in recolor:
            for start in self.by_key.get(key, ()):
                self._paint(start)

    def updateCursor(self, line_start, end):
        # type: (int, int) -> None
        """Called with source coordinates, before inserting mistake text."""
        previous = self.cursor if self.line_start == line_start and \
            self.cursor is not None else line_start
        self.line_start, self.cursor = line_start, end
        low, high = sorted((previous, end))
        for index in range(bisect_right(self.ends, low),
                           bisect_right(self.ends, high)):
            start = self.starts[index]
            if end < previous:
                self.outcomes.pop(start, None)
            else:
                # A projected line-end success arrives before textChanged.
                self.outcomes.setdefault(start, False)
            self._paint(start)

    def record(self, result):
        # type: (object) -> None
        if not isinstance(result, ValidatedChord):
            return
        match = self.words.get(result.book_cursor)
        if match is None or match.group() != result.word or \
           match.group().lower() not in self.active_keys:
            return
        self.outcomes[result.book_cursor] = True
        self._paint(result.book_cursor)

    def _selection(self, start, end):
        # type: (int, int) -> QTextCursor
        cursor = QTextCursor(self.document)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        return cursor

    def _capture(self, start):
        # type: (int) -> None
        qt_start = self.qt_starts[start]
        self.original[start] = [
            self._selection(position, position + 1).charFormat()
            for position in range(qt_start,
                                  qt_start + len(self.words[start].group()))]

    def _restore(self, start):
        # type: (int) -> None
        for position, source in enumerate(self.original.pop(start),
                                          self.qt_starts[start]):
            cursor = self._selection(position, position + 1)
            current = cursor.charFormat()
            for prop in (QTextFormat.TextUnderlineStyle,
                         QTextFormat.TextUnderlineColor):
                current.clearProperty(prop)
                if source.hasProperty(prop):
                    current.setProperty(prop, source.property(prop))
            cursor.setCharFormat(current)

    def _paint(self, start):
        # type: (int) -> None
        if start not in self.original or self.colors is None:
            return
        overlay = QTextCharFormat()
        overlay.setUnderlineStyle(QTextCharFormat.DotLine)
        # Untyped words retain the existing ambient green chord hint. Only a
        # completed non-chorded token uses the capitalization-mistake token.
        overlay.setUnderlineColor(self.colors[
            1 if self.outcomes.get(start) is False else 0])
        qt_start = self.qt_starts[start]
        self._selection(qt_start, qt_start + len(
            self.words[start].group())).mergeCharFormat(overlay)
