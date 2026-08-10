"""A semantic, aligned dual-row CharaChorder hint bar.

Each listed chord gets a small two-row table: the prominent word-order row is
followed immediately by the secondary physical device-order row.  The labels
are intentionally visible so the distinction does not depend on colour.
"""
from qt import QLabel, Qt

from typing import TYPE_CHECKING

from retype.services.theme import theme, C, Theme
from retype.services.chords import WORD_RE, escape_chord_text

# How many upcoming known-chord words to show at once.
MAX_HINTS = 4


@theme('BookView.ChordHintBar', C(fg='#666', bg='#EAE5D2'))
@theme('BookView.ChordHintBar.WordOrder', C(fg='#4A4A4A'))
@theme('BookView.ChordHintBar.DeviceOrder', C(fg='#777777'))
@theme('BookView.ChordHintBar.Active', C(fg='#1a7f37'))
class ChordHintBar(QLabel):
    def __init__(self, chords=None, parent=None):
        # type: (ChordHintBar, dict[str, object] | None, QWidget | None) -> None
        super().__init__(parent)
        self.setObjectName('chord-hint-bar')
        self.chords = chords or {}
        self._last_hints = []
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setWordWrap(True)
        self.setContentsMargins(6, 2, 6, 2)
        self.setAccessibleName('Chord hints')
        self.setAccessibleDescription(
            'Chord hints show word order first and device order beneath it.')
        self.setToolTip(
            'Chord hints: word order is the memorisation order; '
            'device order is physical left to right.')

        (self.c_bar, self.c_word_order, self.c_device_order,
         self.c_active) = self._loadTheme()
        for color in (self.c_bar, self.c_word_order, self.c_device_order,
                      self.c_active):
            color.changed.connect(self.themeUpdate)
        self.themeUpdate()

        self._applyVisibility()

    def _loadTheme(self):
        # type: (ChordHintBar) -> tuple[C, C, C, C]
        return (Theme.get('BookView.ChordHintBar'),
                Theme.get('BookView.ChordHintBar.WordOrder'),
                Theme.get('BookView.ChordHintBar.DeviceOrder'),
                Theme.get('BookView.ChordHintBar.Active'))

    def themeUpdate(self):
        # type: (ChordHintBar) -> None
        qss = Theme.getQss('BookView.ChordHintBar').replace(
            'BookView.ChordHintBar', 'QLabel')
        self.setStyleSheet(qss)
        # Theme roles are rendered inline because Qt rich text does not resolve
        # application QSS selectors inside its document.
        if self.text():
            self._render(self._last_hints)

    def setChords(self, chords):
        # type: (ChordHintBar, dict[str, object]) -> None
        self.chords = chords or {}
        self._applyVisibility()

    def _applyVisibility(self):
        # type: (ChordHintBar) -> None
        # With no chords loaded the bar stays out of the way entirely.
        self.setVisible(bool(self.chords))
        if not self.chords:
            self.clear()

    def upcomingHints(self, line, offset):
        # type: (ChordHintBar, str, int) -> list[tuple[str, object, bool]]
        """Return ``(word, chord, is_active)`` for known upcoming words.

        ``offset`` is the number of characters already typed on ``line``. The
        first word whose end is past ``offset`` is the active one; later known
        words retain the existing preview ordering and maximum hint count.
        """
        hints = []  # type: list[tuple[str, object, bool]]
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

    @staticmethod
    def _word_order(chord):
        # type: (object) -> str
        return str(getattr(chord, 'word_order', chord))

    @staticmethod
    def _device_order(chord):
        # type: (object) -> str | None
        value = getattr(chord, 'device_order', None)
        return str(value) if value is not None else None

    def _render(self, hints):
        # type: (ChordHintBar, list[tuple[str, object, bool]]) -> None
        if not hints:
            self.setText('')
            return

        word_color = escape_chord_text(self.c_word_order.fg().name())
        device_color = escape_chord_text(self.c_device_order.fg().name())
        active_color = escape_chord_text(self.c_active.fg().name())
        cells = []
        accessible = [
            'Chord hints. Each hint has a word order row and, when layout data '
            'is available, a device order row.'
        ]

        for word, chord, is_active in hints:
            word_order = self._word_order(chord)
            device_order = self._device_order(chord)
            safe_word = escape_chord_text(word)
            safe_word_order = escape_chord_text(word_order)
            top_style = (
                'color:{};'.format(active_color) if is_active else
                'color:{};'.format(word_color))
            top_text = '{} → {}'.format(safe_word, safe_word_order)
            if is_active:
                top_text = '<strong>{}</strong>'.format(top_text)
            rows = [
                '<tr><th scope="row" class="chord-word-order" '
                'style="color:{};">word order:&nbsp;</th><td '
                'class="chord-word-order" style="{}">{}</td></tr>'.format(
                    word_color, top_style, top_text),
            ]
            if device_order is not None:
                safe_device_order = escape_chord_text(device_order)
                rows.append(
                    '<tr><th scope="row" class="chord-device-order" '
                    'style="color:{};">device order:&nbsp;</th><td '
                    'class="chord-device-order" style="color:{};">{}</td>'
                    '</tr>'.format(device_color, device_color,
                                   safe_device_order))
                accessible.append(
                    '{}; word order: {}; device order: {}'.format(
                        word, word_order, device_order))
            else:
                accessible.append('{}; word order: {}'.format(word, word_order))

            # The nested table keeps the physical row directly beneath its
            # corresponding word row. The outer table gives every hint an
            # aligned column while allowing Qt to wrap long chord strings.
            cells.append(
                '<td valign="top"><table class="chord-hint" '
                'role="group" cellspacing="0" cellpadding="0">{}</table>'
                '</td>'.format(''.join(rows)))

        markup = (
            '<table class="chord-hints" role="presentation" width="100%" '
            'cellspacing="8" cellpadding="0"><tr>{}</tr></table>'
            .format(''.join(cells)))
        self.setAccessibleDescription(' '.join(accessible))
        self.setText(markup)

    def update_(self, line, offset):
        # type: (ChordHintBar, str, int) -> None
        if not self.chords:
            return
        hints = self.upcomingHints(str(line), offset)
        self._last_hints = hints
        self._render(hints)


if TYPE_CHECKING:
    from qt import QWidget  # noqa: F401
