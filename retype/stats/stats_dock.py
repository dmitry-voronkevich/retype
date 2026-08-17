from math import floor, ceil
from time import time
from qt import QWidget, QPainter, Qt, QSize, QFontMetricsF, pyqtSignal

from typing import TYPE_CHECKING

from retype.ui.painting import rectPixmap, textPixmap, linePixmap, Font
from retype.services.chord_detection import (
    KeyboardChordDetector, MAX_BURST_MILLISECONDS,
    MAX_INTERCHAR_MILLISECONDS, ValidatedChord,
)
from retype.services.chords import WORD_RE
from retype.services.theme import theme, C, Theme


@theme('BookView.StatsDock.Main', C(fg='white', bg='#CDCDC1'))
@theme('BookView.StatsDock.Text', C(fg='black'))
@theme('BookView.StatsDock.Grid', C(fg='gray'))
@theme('BookView.StatsDock.LikelyChord', C(fg='#1a7f37'))
class StatsDock(QWidget):
    # Likely is a diagnostic timing observation only; it is never success.
    likelyChordDetected = pyqtSignal(int)
    # The authoritative domain event. Every success subscriber (banner,
    # counter, and green chart marking) consumes this typed result.
    validatedChordDetected = pyqtSignal(object)
    successfulChordFeedbackReset = pyqtSignal()

    def __init__(self, book_view, parent=None):
        # type: (StatsDock, BookView, QWidget | None) -> None
        super().__init__(parent)
        self.book_view = book_view

        self.setAccessibleName("Typing statistics")
        self.connected = False

        self.prev_cursor_pos = 0
        self.prev_seconds = 0
        self.prev_ts = 0
        self.c = 0
        self.cpm = 0
        self.wpm = 0
        self.wpm_pb = 0
        self.rect_w = 15
        self.wpms = []  # type: list[int]
        # Kept separately from ``wpms`` so ordinary chart data remains intact.
        # Green segments represent validated outcomes, never timing-only Likely
        # observations.
        self.wpms_validated_chords = []  # type: list[bool]
        self.likely_chords = 0
        self.successful_chords = 0
        self._pending_validated_segment = False
        # The final cleanup Backspace can empty the console. Keep that event
        # from looking like a programmatic context reset until textChanged has
        # handled it; later key events clear this one-event allowance.
        self._preserve_empty_text_reset = False
        self.chord_detector = KeyboardChordDetector()
        self._resetCandidate()
        self.validatedChordDetected.connect(self._recordValidatedChord)

        self.main_c, self.text_c, self.grid_c, self.likely_c = self._loadTheme()
        self.main_c.changed.connect(self.themeUpdate)
        self.likely_c.changed.connect(self.themeUpdate)
        self._update_accessibility()

    def _loadTheme(self):
        # type: (StatsDock) -> tuple[C, ...]
        return (Theme.get('BookView.StatsDock.Main'),
                Theme.get('BookView.StatsDock.Text'),
                Theme.get('BookView.StatsDock.Grid'),
                Theme.get('BookView.StatsDock.LikelyChord'))

    def themeUpdate(self):
        # type: (StatsDock) -> None
        self._update_accessibility()
        self.update()

    def _update_accessibility(self):
        # type: (StatsDock) -> None
        self.setAccessibleDescription(
            "Typing statistics. Likely timing bursts: {}. Validated chord "
            "words: {}. Green chart segments mark validated rapid correct "
            "known words; Likely timing is a heuristic and does not identify "
            "a device.".format(self.likely_chords, self.successful_chords))

    def connectConsole(self, console):
        # type: (StatsDock, Console) -> None
        self._hs = console.highlighting_service
        self._console = console
        console.cleared.connect(self._onConsoleCleared)
        console.keyPressAboutToBeProcessed.connect(self._onKeyPress)
        console.textEdited.connect(self._onTextEdited)
        console.textEdited.connect(self.onUpdate)
        console.textChanged.connect(self._onTextChanged)
        self.connected = True

    @staticmethod
    def _eventText(event):
        # type: (object) -> str | None
        text_method = getattr(event, 'text', None)
        text = text_method() if callable(text_method) else None
        return text if isinstance(text, str) else None

    def _eventTimestamp(self, event):
        # type: (StatsDock, object) -> float | None
        return self.chord_detector.timestamp_for_event(event)

    @staticmethod
    def _isDelimiter(text, event):
        # type: (str | None, object) -> bool
        """Whether a key can finish a word before it edits the console."""
        if isinstance(text, str) and len(text) == 1:
            return text.isspace() or (text.isprintable() and
                                      WORD_RE.fullmatch(text) is None)
        key_method = getattr(event, 'key', None)
        key = key_method() if callable(key_method) else None
        return key in (int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter))

    def _editorState(self):
        # type: (StatsDock) -> tuple[str, int, bool] | None
        console = getattr(self, '_console', None)
        if console is None:
            return None
        cursor = console.textCursor()
        return (console.text(), cursor.position(), cursor.hasSelection())

    def _resetCandidate(self):
        # type: (StatsDock) -> None
        """Discard unfinalized output provenance, never a Likely observation."""
        self._candidate_text = []  # type: list[str]
        self._candidate_timestamps = []  # type: list[float]
        self._candidate_editor_start = None  # type: int | None
        self._candidate_book_cursor = None  # type: int | None
        self._candidate_timing_invalid = False
        self._expected_editor_text = None  # type: str | None
        self._expected_editor_text_seen = False

    def _appendCandidateCharacter(self, output, timestamp):
        # type: (StatsDock, str, float | None) -> None
        state = self._editorState()
        v = self.book_view
        if state is None or timestamp is None:
            self._resetCandidate()
            return
        text, cursor, has_selection = state
        if has_selection or self._expected_editor_text is not None:
            self._resetCandidate()
            return

        if self._candidate_text:
            expected_cursor = self._candidate_editor_start + len(
                self._candidate_text)
            if cursor != expected_cursor or text[cursor:cursor + 1]:
                self._resetCandidate()
                return
            previous = self._candidate_timestamps[-1]
            if timestamp < previous or \
               timestamp - previous > MAX_INTERCHAR_MILLISECONDS or \
               timestamp - self._candidate_timestamps[0] > \
               MAX_BURST_MILLISECONDS:
                self._candidate_timing_invalid = True
        else:
            self._candidate_editor_start = cursor
            self._candidate_book_cursor = v.cursor_pos
            self._candidate_timing_invalid = False

        self._candidate_text.append(output)
        self._candidate_timestamps.append(timestamp)
        self._expected_editor_text = text[:cursor] + output + text[cursor:]

        # Auto-newline can clear the console synchronously while processing
        # this character. Finalize from the projected editor text first.
        if self._wouldCompleteLine(self._expected_editor_text):
            self._finalizeCandidate(self._expected_editor_text, cursor + 1,
                                    projected=True)
            self._resetCandidate()

    def _removeCandidateCharacter(self):
        # type: (StatsDock) -> None
        state = self._editorState()
        if state is None or not self._candidate_text or \
           self._expected_editor_text is not None:
            self._resetCandidate()
            return
        text, cursor, has_selection = state
        expected_cursor = self._candidate_editor_start + len(
            self._candidate_text)
        if has_selection or cursor != expected_cursor or cursor <= 0 or \
           text[cursor - 1] != self._candidate_text[-1]:
            self._resetCandidate()
            return
        self._candidate_text.pop()
        self._candidate_timestamps.pop()
        self._expected_editor_text = text[:cursor - 1] + text[cursor:]
        if not self._candidate_text:
            self._candidate_editor_start = None
            self._candidate_book_cursor = None
            self._candidate_timing_invalid = False

    def _wouldCompleteLine(self, text):
        # type: (StatsDock, str) -> bool
        line = getattr(self.book_view, 'current_line', None)
        if line is None:
            return False
        # HighlightingService accepts either the literal line or its trailing
        # non-breaking-space-stripped form as a completed line.
        from retype.extras.space import nrspacerstrip
        return text == str(line) or text == nrspacerstrip(line)

    def _finalizeCandidate(self, editor_text=None, editor_cursor=None,
                           projected=False):
        # type: (StatsDock, str | None, int | None, bool) -> None
        """Emit one bounded rapid-output encouragement for a surviving token."""
        if not self._candidate_text or self._candidate_timing_invalid or \
           (self._expected_editor_text is not None and not projected):
            return
        state = self._editorState()
        if editor_text is None:
            if state is None:
                return
            editor_text, editor_cursor, has_selection = state
            if has_selection:
                return
        if editor_cursor is None or self._candidate_editor_start is None or \
           self._candidate_book_cursor is None:
            return

        token = ''.join(self._candidate_text)
        editor_match = None
        for match in WORD_RE.finditer(editor_text):
            if match.end() == editor_cursor:
                editor_match = match
                break
        # The candidate must be the entire surviving editor token. This blocks
        # a rapid suffix after a timing reset from accepting ``xat`` as ``at``.
        if editor_match is None or editor_match.start() != \
           self._candidate_editor_start or editor_match.group() != token:
            return
        if self.book_view.isSuccessfulChordOutput(
                token, self._candidate_book_cursor):
            line = getattr(self.book_view, 'current_line', '')
            suffix = line[len(editor_text or ''):] if isinstance(line, str) \
                and isinstance(editor_text, str) and line.startswith(editor_text) \
                else ''
            completing_line = bool(suffix) and all(
                not character.isalnum() and character != '_' for character in suffix)
            completed_on_line_end = projected or completing_line
            gaps = [later - earlier for earlier, later in zip(
                self._candidate_timestamps, self._candidate_timestamps[1:])]
            result = ValidatedChord(
                token, token.lower(), token, self._candidate_book_cursor,
                self._candidate_editor_start,
                self._candidate_timestamps[-1] -
                self._candidate_timestamps[0], max(gaps) if gaps else 0.0,
                completed_on_line_end)
            self.validatedChordDetected.emit(result)

    def _recordValidatedChord(self, result):
        # type: (StatsDock, ValidatedChord) -> None
        """Update every in-dock success representation from one domain event."""
        if not isinstance(result, ValidatedChord):
            return
        self.successful_chords += 1
        # At a delimiter the full word already has bars. At a projected line
        # end, the final character's bar is still pending its editor mutation.
        completed_bars = len(result.word) - (1 if result.completed_on_line_end
                                             else 0)
        for index in range(1, min(completed_bars, len(self.wpms)) + 1):
            self.wpms_validated_chords[-index] = True
        self._pending_validated_segment = result.completed_on_line_end
        self._update_accessibility()
        self.update()

    def _onKeyPress(self, event):
        # type: (StatsDock, QKeyEvent) -> None
        v = self.book_view
        self._preserve_empty_text_reset = False
        if not v.isVisible() or v.cursor_pos is None:
            self.chord_detector.reset()
            self._resetCandidate()
            return

        was_reported = self.chord_detector.has_reported_burst
        is_backspace = self.chord_detector.is_backspace_event(event)
        text = self._eventText(event)
        if is_backspace:
            self._removeCandidateCharacter()
        elif isinstance(text, str) and len(text) == 1 and \
                (WORD_RE.fullmatch(text) is not None or
                 (text == "'" and self._candidate_text)):
            self._appendCandidateCharacter(text, self._eventTimestamp(event))
        elif self._isDelimiter(text, event):
            self._finalizeCandidate()
            self._resetCandidate()
        else:
            self._resetCandidate()

        observation = self.chord_detector.observe_event(event)
        self._preserve_empty_text_reset = (
            is_backspace and was_reported and
            self.chord_detector.has_reported_burst)
        if observation is None:
            return

        if observation.is_new:
            self.likely_chords += 1
            self.likelyChordDetected.emit(self.likely_chords)
            self._update_accessibility()

        # Likely observations are intentionally diagnostic only. They never
        # colour a successful segment or update successful-chord state.
        self.update()

    def _onConsoleCleared(self):
        # type: (StatsDock) -> None
        self.chord_detector.reset()
        self._resetCandidate()
        self._preserve_empty_text_reset = False
        self.successfulChordFeedbackReset.emit()

    def _onTextChanged(self, text):
        # type: (StatsDock, str) -> None
        # Only the exact post-edit state projected from our key event can keep
        # candidate provenance. Paste, IME, selection replacement, setText,
        # fillChars, and any other programmatic mutation therefore fail closed.
        if self._expected_editor_text is not None:
            if text == self._expected_editor_text:
                # Wait for textEdited before accepting this projected state:
                # setText/paste can produce identical textChanged output but
                # must never inherit keyboard-event provenance.
                self._expected_editor_text_seen = True
                return
            self._resetCandidate()
        # A programmatic clear starts a new output context (line advance,
        # chapter navigation, or a view switch).
        if not text:
            if self._preserve_empty_text_reset:
                self._preserve_empty_text_reset = False
            else:
                self.chord_detector.reset()
            self._resetCandidate()
            # Keep a pending mark until the matching textEdited/onUpdate
            # callback consumes it. This matters when completing a line
            # clears the console synchronously during highlighting.

    def _onTextEdited(self, text):
        # type: (StatsDock, str) -> None
        """Accept a projected mutation only when Qt marks it user-edited."""
        if self._expected_editor_text is None:
            return
        if self._expected_editor_text_seen and text == \
                self._expected_editor_text:
            self._expected_editor_text = None
            self._expected_editor_text_seen = False
        else:
            self._resetCandidate()

    def onUpdate(self, text):
        # type: (StatsDock, str) -> None
        v = self.book_view
        if not v.isVisible() or v.cursor_pos is None:
            return

        ts = round(time())
        # Start “timer”
        if not self.prev_ts:
            self.prev_ts = ts

        seconds = (ts - self.prev_ts) or 1

        # Reset if been inactive
        if seconds - self.prev_seconds > 2:
            self.prev_ts = ts
            self.c = 0

        graphShouldUpdate = False
        # FIXME: This is probably not very robust; it’s an attempt to make
        #  things work for both character-by-character typing and stenography
        #  where whole words can be inputted at once, while preventing the
        #  cursor-maniplation commands from affecting the count undesirably.
        if len(text) >= v.cursor_pos - self.prev_cursor_pos > 0:
            self.c += v.cursor_pos - self.prev_cursor_pos
            graphShouldUpdate = True

        # CPM and WPM calculation
        self.cpm = floor((self.c / seconds) * 60)
        self.wpm = floor(self.cpm / 5)

        # Personal best
        if self.wpm > self.wpm_pb:
            self.wpm_pb = self.wpm

        # Periodic refreshment
        if seconds > 20:
            self.prev_ts = ts - 5
            self.c = int(self.cpm / 12)

        # Graph update
        if graphShouldUpdate:
            w = self.size().width()
            amount = floor(w / self.rect_w)
            if len(self.wpms) > amount:
                length = len(self.wpms)
                self.wpms = self.wpms[length-amount:length]
                self.wpms_validated_chords = self.wpms_validated_chords[
                    length-amount:length]
            self.wpms.append(self.wpm)
            self.wpms_validated_chords.append(
                self._pending_validated_segment)
            self._pending_validated_segment = False
            self.update()

        if not graphShouldUpdate:
            self._pending_validated_segment = False
        self.prev_seconds = seconds
        self.prev_cursor_pos = v.cursor_pos

    def resetSession(self):
        # type: (StatsDock) -> None
        """Reset the detector and the statistics represented by this dock."""
        self._onConsoleCleared()
        cursor_pos = getattr(self.book_view, 'cursor_pos', None)
        self.prev_cursor_pos = cursor_pos if cursor_pos is not None else 0
        self.prev_seconds = 0
        self.prev_ts = 0
        self.c = 0
        self.cpm = 0
        self.wpm = 0
        self.wpm_pb = 0
        self.wpms = []
        self.wpms_validated_chords = []
        self.likely_chords = 0
        self.successful_chords = 0
        self._pending_validated_segment = False
        self._preserve_empty_text_reset = False
        self._update_accessibility()
        self.likelyChordDetected.emit(0)
        self.update()

    def paintEvent(self, e):
        # type: (StatsDock, QPaintEvent) -> None
        w = self.size().width()
        h = self.size().height()
        factor = 1 if not self.wpm_pb else h/self.wpm_pb

        qp = QPainter()
        qp.begin(self)
        draw = qp.drawPixmap

        # Background
        qp.fillRect(0, 0, w, h, self.main_c.bg())

        # WPM rects
        i = 0
        for index, wpm in enumerate(self.wpms):
            rect_h = floor(wpm * factor)
            validated = (index < len(self.wpms_validated_chords) and
                         self.wpms_validated_chords[index])
            bar_c = self.likely_c.fg() if validated else self.main_c.fg()
            draw(i, h - rect_h,
                 rectPixmap(self.rect_w, int(wpm * factor),
                            self.main_c.bg(), bar_c))
            i += self.rect_w

        # Gridlines
        i = 50
        while i < self.wpm_pb:
            y = h - int(i * factor)
            qp.drawPixmap(0, y,
                          linePixmap(w, 0, self.grid_c.fg(), 1,
                                     style=Qt.PenStyle.DashLine))
            i += 50

        # Text
        font = Font.GENERAL.toQFont()
        fm = QFontMetricsF(font)
        font_h = ceil(fm.height())
        pb_txt = "PB: {}".format(self.wpm_pb)
        cur_txt = "Current: {} WPM".format(self.wpm)
        draw(2, 2,
             textPixmap(pb_txt, ceil(fm.horizontalAdvance(pb_txt)), font_h,
                        font, self.text_c.fg()))
        cur_w = ceil(fm.horizontalAdvance(cur_txt))
        draw(w - cur_w - 2, 2,
             textPixmap(cur_txt, cur_w, font_h, font, self.text_c.fg()))
        chord_txt = "Likely: {}  Success: {}".format(
            self.likely_chords, self.successful_chords)
        chord_w = ceil(fm.horizontalAdvance(chord_txt))
        draw(max(2, (w - chord_w) // 2), 2,
             textPixmap(chord_txt, chord_w, font_h, font,
                        self.likely_c.fg() if self.likely_chords else
                        self.text_c.fg()))

        qp.end()

    def sizeHint(self):
        # type: (StatsDock) -> QSize
        return QSize(50, 25)


if TYPE_CHECKING:
    from retype.ui import BookView  # noqa: F401
    from retype.console import Console  # noqa: F401
    from qt import QPaintEvent  # noqa: F401
