"""A thin bar that shows the CharaChorder chord for the word you are about to
 type, plus a few upcoming words that also have a known chord.

Only words present in the loaded chord map are shown, so it doubles as a cue
 for which chords you already know while typing along to a book.
"""
from qt import QLabel, Qt

from typing import TYPE_CHECKING

from retype.services.theme import theme, C, Theme
from retype.services.chords import WORD_RE

# How many upcoming known-chord words to show at once.
MAX_HINTS = 4


@theme('BookView.ChordHintBar', C(fg='#666', bg='#EAE5D2'))
@theme('BookView.ChordHintBar.Active', C(fg='#1a7f37'))
class ChordHintBar(QLabel):
    def __init__(self, chords=None, parent=None):
        # type: (ChordHintBar, dict[str, str] | None, QWidget | None) -> None
        super().__init__(parent)
        self.chords = chords or {}
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setContentsMargins(6, 2, 6, 2)

        self.c_bar, self.c_active = self._loadTheme()
        self.c_bar.changed.connect(self.themeUpdate)
        self.themeUpdate()

        self._applyVisibility()

    def _loadTheme(self):
        # type: (ChordHintBar) -> tuple[C, C]
        return (Theme.get('BookView.ChordHintBar'),
                Theme.get('BookView.ChordHintBar.Active'))

    def themeUpdate(self):
        # type: (ChordHintBar) -> None
        qss = Theme.getQss('BookView.ChordHintBar').replace(
            'BookView.ChordHintBar', 'QLabel')
        self.setStyleSheet(qss)

    def setChords(self, chords):
        # type: (ChordHintBar, dict[str, str]) -> None
        self.chords = chords or {}
        self._applyVisibility()

    def _applyVisibility(self):
        # type: (ChordHintBar) -> None
        # With no chords loaded the bar stays out of the way entirely.
        self.setVisible(bool(self.chords))
        if not self.chords:
            self.clear()

    def upcomingHints(self, line, offset):
        # type: (ChordHintBar, str, int) -> list[tuple[str, str, bool]]
        """Return ``(word, chord, is_active)`` for the next known-chord words.

        ``offset`` is the number of characters already typed on ``line``. The
         first word whose end is past ``offset`` is the "active" one (the word
         being typed or about to be typed); the active flag marks the nearest
         known-chord word from there on.
        """
        hints = []  # type: list[tuple[str, str, bool]]
        first = True
        for m in WORD_RE.finditer(line):
            if m.end() <= offset:
                continue  # already fully typed
            chord = self.chords.get(m.group().lower())
            if not chord:
                continue
            hints.append((m.group(), chord, first))
            first = False
            if len(hints) >= MAX_HINTS:
                break
        return hints

    def update_(self, line, offset):
        # type: (ChordHintBar, str, int) -> None
        if not self.chords:
            return
        hints = self.upcomingHints(str(line), offset)
        if not hints:
            self.setText("")
            return

        active_color = self.c_active.fg().name()
        parts = []
        for word, chord, is_active in hints:
            text = f"{word} → {chord}"
            if is_active:
                parts.append(
                    f'<b style="color:{active_color}">{text}</b>')
            else:
                parts.append(text)
        self.setText("&nbsp;&nbsp;&nbsp;".join(parts))


if TYPE_CHECKING:
    from qt import QWidget  # noqa: F401
