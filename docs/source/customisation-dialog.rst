Customisation Dialog
====================

The Customisation Dialog allows you to customise many aspects of *retype*. It is saved in file ``config.json`` in the :ref:`user-dir`.

When all the options are set to the same values as they are in the config, the Revert button is greyed out. When you change them it will be clickable and allows you to revert to the saved values. In order to save changed settings, the Save button must be pressed. After doing so, the Revert button will be greyed out again since the options values match the saved ones again.

When the options are set to the same values as the default config, the Restore Defaults button is greyed out. When you change them, it will be clickable and allows you to revert to the default values. In order to return to the default values, the Save button must be pressed after clicking Restore Defaults.

Paths
-----

.. _user-dir:

User dir
^^^^^^^^

Path to the user dir, which is where the save and config files are stored.

.. _library-search-paths:

Library search paths
^^^^^^^^^^^^^^^^^^^^

Paths where *retype* should look for epub files.

Chords & CharaChorder
---------------------

Chord hints can be loaded from a connected CharaChorder Two S3 running CCOS
3.x. **Load chords from CharaChorder on startup** is enabled by default,
including for existing configurations without this saved setting. Uncheck it
to skip the startup read; this does not clear local or previously loaded chord
data.

Use **Load chords now** to read the device snapshot on demand, including when
startup loading is disabled. The action is disabled while a read is active.
The status text reports progress and whether loading succeeded, was cancelled,
was unavailable because no supported device could be read, or failed for
another reason. A new chord map is applied only after the complete snapshot
has been read successfully; otherwise existing chord data is retained.

When **Limit number of chords per lesson** is checked, the Book View limits
incomplete chords according to **Chords introduced per lesson**; uncheck it to
show the full loaded chord list. The configured number is used again when the
limit is turned back on. With no available device chord map, these settings
have no effect.

Chord mastery is shown below the load controls as a table-first overview of the
currently loaded chords. The summary counts teaching targets in progress,
mastered hint-eligible chords, and other loaded chords. The Chord, Progress,
and Status columns can be sorted; ascending status sorting places mastered
chords first, in-progress chords second, and other chords last, with a visible
sort indicator. Column headers can be focused and activated from the keyboard,
and stable ties retain the chord order. The Select column is not sortable. Rows
are selected with checkboxes, and bulk actions appear once one or more rows are
selected. Manual changes show an Undo strip and a link to restore the measured
state. The table is refreshed from current loaded chords and mastery progress
each time the dialog is shown, including when an existing dialog is reopened;
pending manual changes remain pending while the measured progress is refreshed.

Console
-------

.. _prompt-customisation:

Prompt
^^^^^^

The prompt is a string console commands must be prefixed by. Can be any length, including an empty string if you do not want to prefix them with anything.

Hide System Console window on UI load (Windows-only)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

On Windows, applications can be built in either console or windowed mode. The downside of the latter is you get no debug output even if you run it from a terminal. Therefore, *retype* is built in console mode, but in order to prevent the console window from getting in the users way, it is by default hidden when the UI loads. This checkbox enables you to disable this, if you prefer the console window to remain visible.

You can toggle the console window at any time by clicking on the menu option ``View > Toggle System Console``.

Blender users will be familiar with this concept.

.. note::
   Hiding the console window is only possible if *retype* was built with ``pywin32``.

Book View
---------

Font size
^^^^^^^^^

The font size can be saved on quit or set to a constant default.

The font size can be changed at any time in :doc:`book-view` using the :ref:`toolbar-actions` or using the mousewheel while holding :kbd:`Ctrl`.

.. _replacements:

Replacements
------------

Configure substrings that can be typeable by any one of the set comma-separated list of replacements. This is useful for unicode characters that you don’t have an easy way to input. Each replacement should be of equal length to the original substring. 

Window Geometry
---------------

The window geometry can be saved on quit or set to a constant default.

The state of the splitters can be saved on quit. This refers to the position of the drag handle above the :doc:`console` and :ref:`stats-dock`; their size and whether they should be collapsed or not.
