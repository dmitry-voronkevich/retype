Book View
=========

.. image:: ../_static/img/bookview.png 
           :alt: retype’s Book View with the book The Yellow Wallpaper
           :align: center

1. Toolbar
    The bar at the top of the view, which contains :ref:`common Book-View-specific actions <toolbar-actions>`.
2. Cursor
    Represents the current position in the text.
3. Display
    The main book display which contains the text to be typed, with the text already typed highlighted in yellow.
4. :ref:`modeline`
    The bar at the bottom of the view, which contains some information about the book and the current position therein.
5. :ref:`stats-dock`
    A collapsible and resizeable dock which displays statistics on the user’s typing rate in WPM (words per minute).

.. _toolbar-actions:

Toolbar actions
---------------

- Back to shelves
   Return to :doc:`shelf-view`.
- Cursor position
   Go to the cursor position. Hold :kbd:`Ctrl` to instead move cursor to your current position.
- Previous chapter
   Go to the previous chapter. Hold :kbd:`Ctrl` to move cursor with you as well.
- Next chapter
   Go to the next chapter. Hold :kbd:`Ctrl` to move cursor with you as well.
- Skip line
   Move cursor to the next line.
- Increase font size
   Increase the Display’s font size.
- Decrease font size
   Decrease the Display’s font size.

..
   possibly should be generated from the dict in book_view as well, like the console commands? the issue is the tooltip there needs to be short while here we could have more information. however, i could simply add there a 'desc' attr with a longer description and use that.

Most of these actions have corresponding :ref:`console-commands`.

The font size can also be increased or decreased using the mousewheel while holding :kbd:`Ctrl`.

.. _modeline:

Modeline
--------

.. image:: ../_static/img/modeline.png 
           :alt: retype’s Modeline
           :align: center

1. Line position
    The line position of the cursor in the current chapter.
2. Cursor position
    The raw cursor position in the current chapter.
3. Book title
    The title of the book currently loaded.
4. Percentage completed
    How far through the book the cursor is located.
5. Chapter position (view)
    The chapter position of the chapter currently viewed.
6. Chapter position (cursor)
    The chapter position of the cursor.

.. _stats-dock:

Stats Dock
----------

The Stats Dock is the graph above the Modeline. It can be resized or collapsed using the drag handle above it. It contains the following main elements:

#. PB
    The personal best WPM (words per minute) typing speed achieved in the current session.
#. Current
    The current typing speed in WPM.
#. Rectangles
    Each rectangle’s height represents the WPM achieved as a proportion of the PB. New rectangles appear on the right; the right-most rectangle represents the current WPM.
#. Dashed lines
    Dashed lines appear every 50 WPM and are there to help gauge the WPM each rectangle represents. For example, a rectangle whose height matches the first dashed line from the bottom represents 50 WPM.
#. Chords
    The ``Chords: N`` label and green chart segments represent validated rapid, correct known words at the book cursor. Timing-only observations are retained only as internal diagnostics and never affect this user-facing count.

The Stats Dock updates on every letter typed correctly.

Keyboard-only chord feedback
----------------------------

The keyboard-only heuristic watches ordinary Qt keyboard events that retype
already receives. Timing-only observations are internal diagnostics; they are
not displayed as chord successes and never affect the ``Chords`` count or green
chart segments.

After a word delimiter (including punctuation or Return), or when automatic
line completion survives the edit, a validated rapid, correct known chord word
at the current book cursor is announced in the application status bar as
``Chord detected: WORD``. The word must match the loaded dictionary and book
word, and each surviving character must be no more than 35 milliseconds apart
with a total span no greater than 120 milliseconds. CharaChorder-style
incorrect prefixes followed by Backspace cleanup are accepted only when the
final surviving word meets those conditions. Pastes, selection replacement,
IME-like or programmatic edits, ambiguous timing resets, and uncorrected
prefixes fail closed.

Detection messages remain visible for 1.2 seconds and are coalesced within a
150-millisecond burst, so rapid events do not make the status bar unreadable.
When a validated event changes lesson progress, the status bar instead reports
``Chord learned: WORD`` and/or ``New chord to learn: WORD``. Progression
messages have priority over ordinary detection messages and remain visible for
4 seconds. Expiry restores the permanent application or device status message.
Console clears, automatic completion, navigation, and statistics resets do not
control this feedback.

This is a bounded timing heuristic for the observed study conditions, not a
device detector or attribution claim. A fast ordinary typist or macro can
receive the same feedback, notably for a valid short known word such as ``at``;
slower, interrupted, or mixed output can miss it. The detector uses no USB,
serial, HID, Web Serial, raw-device, or device-companion live-key access. A
device snapshot read may provide its chord dictionary, either at startup or on
demand from the :doc:`customisation-dialog`; it never attributes typed keys to
the device.

Session-only chord mastery statistics
-------------------------------------

``ChordMasteryTracker`` is a domain service that separately consumes only
``ValidatedChord`` results. It retains per-dictionary-key successful-use
counts, word identities, timing summaries, and final event position data for
the current session, and emits debug console logs for progress and threshold
transitions. It does not store data, change lesson selection, or drive a UI.
Timing-only observations and ordinary typed characters never enter these
statistics.

Adaptive chord lessons
----------------------

When a chord dictionary is loaded, retype normally teaches at most five
incomplete chords in a chapter. Chords with recorded progress are presented
before unseen chords; unseen chords are then ordered by how often their words
occur in that chapter. A completed target remains available to the hint bar,
but does not use one of the teaching places. The cumulative validated-use
counts are stored as ``chord-mastery.json`` beside ``save.json`` in the user
directory, so they survive application sessions. Manual mastered/unmastered
overrides are stored there too and win over the measured progress until they
are restored.

To opt out, open Customisation, choose **Chords & CharaChorder**, and uncheck
**Limit number of chords per lesson**. This restores the full loaded chord list;
when the limit is enabled, **Chords introduced per lesson** controls how many
incomplete chords are taught. Changing either setting or the loaded dictionary
refreshes the current chapter's underlines and hints immediately. With no
loaded dictionary, ordinary typing is unchanged and no chord lesson is active.

A lesson target completes after ten validated successes. This is curriculum
progress, not a claim that a chord device made the entry: the available event
reports a rapid character stream but cannot distinguish a fast typist or macro.
The session tracker therefore continues to report this as a mastery candidate,
not direct-entry evidence. Corrupt or newer ``chord-mastery.json`` formats are
never overwritten; retype logs the problem and treats the run as having no
stored progress. In a valid file, malformed individual entries are ignored for
selection but preserved when other progress is saved.
