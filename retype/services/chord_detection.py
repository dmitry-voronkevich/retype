"""Keyboard-only likely-chord detection.

This module intentionally classifies timing patterns, not devices.  A regular
keyboard, a CharaChorder, a macro, or an automated test can produce the same
pattern, so callers must describe the result as *likely* rather than as
confirmed device-originated chording.
"""
from dataclasses import dataclass
from math import isfinite
from time import monotonic


# These thresholds are deliberately public so the heuristic is reviewable and
# deterministic tests can exercise the exact boundaries.  Qt key event times
# are milliseconds; the fallback clock below is converted to milliseconds too.
MIN_BURST_CHARS = 3
MAX_INTERCHAR_MILLISECONDS = 35.0
MAX_BURST_MILLISECONDS = 120.0

# Qt::Key_Backspace is kept numeric here so this timing service stays usable
# without importing Qt. The event path still identifies the key through its
# normal QKeyEvent.key() value, not through device access.
BACKSPACE_KEY = 0x01000003


@dataclass(frozen=True)
class LikelyChordBurst:
    """One observation in a short output burst.

    ``is_new`` is true only for the observation that crosses the minimum burst
    length.  Later characters in the same burst are returned as continuations
    so the chart can colour all output without counting one burst repeatedly.
    """

    output: str
    duration_ms: float
    is_new: bool


class KeyboardChordDetector:
    """Classify short, closely-spaced printable output bursts.

    The detector receives generated text from the normal Qt key event path and
    never reads a USB/serial device. Invalid or non-printable output resets
    the current burst; a reported burst can treat rapid Backspace cleanup as
    neutral editing events. Timestamps are accepted in milliseconds for tests
    and Qt events; when unavailable, monotonic arrival time is used.
    """

    def __init__(self, clock=None):
        # type: (object | None) -> None
        self._clock = clock or monotonic
        self.reset()

    def reset(self):
        """Forget the current output burst and its one-count state."""
        self._output = []  # type: list[str]
        self._first_timestamp = None  # type: float | None
        self._last_timestamp = None  # type: float | None
        self._reported = False

    @staticmethod
    def _valid_output(output):
        # type: (object) -> bool
        return isinstance(output, str) and len(output) == 1 and \
            output.isprintable()

    @staticmethod
    def is_backspace_event(event):
        # type: (object) -> bool
        """Return whether an event is Qt's Backspace key.

        Some platforms provide ``\\b`` as the event text and some provide an
        empty string, so text alone cannot identify cleanup Backspaces.
        """
        key_method = getattr(event, 'key', None)
        if callable(key_method):
            try:
                if int(key_method()) == BACKSPACE_KEY:
                    return True
            except (TypeError, ValueError):
                pass
        text_method = getattr(event, 'text', None)
        text = text_method() if callable(text_method) else None
        return text in ('\b', '\x7f')

    @property
    def output_length(self):
        # type: (KeyboardChordDetector) -> int
        """Number of printable outputs in the current timing burst."""
        return len(self._output)

    @property
    def has_reported_burst(self):
        # type: (KeyboardChordDetector) -> bool
        """Whether the current burst crossed the chord threshold."""
        return bool(self._output) and self._reported

    def _timestamp(self, timestamp_ms):
        # type: (object | None) -> float | None
        if timestamp_ms is None:
            timestamp_ms = self._clock() * 1000.0
        try:
            timestamp = float(timestamp_ms)
        except (TypeError, ValueError):
            return None
        return timestamp if isfinite(timestamp) else None

    def observe(self, output, timestamp_ms=None):
        # type: (object, object | None) -> LikelyChordBurst | None
        """Observe one generated character and maybe classify its burst.

        The threshold comparisons are inclusive: exactly
        ``MAX_INTERCHAR_MILLISECONDS`` between characters and exactly
        ``MAX_BURST_MILLISECONDS`` from first to last are still accepted.
        """
        timestamp = self._timestamp(timestamp_ms)
        if not self._valid_output(output) or timestamp is None:
            self.reset()
            return None

        if self._last_timestamp is None or \
           timestamp < self._last_timestamp or \
           timestamp - self._last_timestamp > MAX_INTERCHAR_MILLISECONDS:
            self.reset()
            self._output = [output]
            self._first_timestamp = timestamp
            self._last_timestamp = timestamp
            return None

        # At this point a first timestamp exists because a last timestamp
        # exists.  Keep the guard explicit for type checkers and malformed
        # state recovery.
        if self._first_timestamp is None or \
           timestamp - self._first_timestamp > MAX_BURST_MILLISECONDS:
            self.reset()
            self._output = [output]
            self._first_timestamp = timestamp
            self._last_timestamp = timestamp
            return None

        self._output.append(output)
        self._last_timestamp = timestamp
        if len(self._output) < MIN_BURST_CHARS:
            return None

        observation = LikelyChordBurst(
            ''.join(self._output), timestamp - self._first_timestamp,
            not self._reported)
        self._reported = True
        return observation

    def _observe_backspace(self, timestamp_ms):
        # type: (KeyboardChordDetector, object | None) -> None
        """Keep a reported burst alive across rapid chord cleanup output.

        Backspace is an editing event, not generated character output. A
        Backspace is only neutral after a burst is already reported and only
        while it remains inside the same timing window. Thus an ordinary
        Backspace cannot join an unfinished burst, and a later burst still
        starts after the normal inter-character boundary.
        """
        timestamp = self._timestamp(timestamp_ms)
        if not self.has_reported_burst or self._last_timestamp is None or \
           self._first_timestamp is None or timestamp is None or \
           timestamp < self._last_timestamp or \
           timestamp - self._last_timestamp > MAX_INTERCHAR_MILLISECONDS or \
           timestamp - self._first_timestamp > MAX_BURST_MILLISECONDS:
            self.reset()
            return
        self._last_timestamp = timestamp

    def observe_event(self, event):
        # type: (object) -> LikelyChordBurst | None
        """Observe a ``QKeyEvent`` without requiring Qt in unit tests."""
        timestamp = None
        timestamp_method = getattr(event, 'timestamp', None)
        if callable(timestamp_method):
            timestamp = timestamp_method()
            # Qt can report zero for synthetic events. Let ``observe`` use
            # the deterministic fallback clock in that case.
            if timestamp == 0:
                timestamp = None

        if self.is_backspace_event(event):
            self._observe_backspace(timestamp)
            return None

        text_method = getattr(event, 'text', None)
        output = text_method() if callable(text_method) else None
        return self.observe(output, timestamp)
