.. raw:: html

  <h1>
  <img src="https://raw.githubusercontent.com/plu5/retype/main/docs/_static/img/retype.ico" width="32"/>
  retype
  </h1>

|version-badge| |docs-badge|
  
*retype* is a free and open-source typing practice application that allows you to type along to epub books. It saves your progress so you can come back where you left off.

.. figure:: https://raw.githubusercontent.com/plu5/retype/main/docs/_static/img/col.png
   :align: center

|
:Source code:   https://github.com/plu5/retype
:Issue tracker: https://github.com/plu5/retype/issues
:Documentation: https://retype.readthedocs.io/

.. _documentation: https://retype.readthedocs.io/

.. contents::

-----
Usage
-----

To run retype, you can `download the latest build for your operating system <https://github.com/plu5/retype/releases/latest>`_, `build it yourself <#build-instructions>`_, or `run it from sources <#running-from-sources>`_.

Build instructions
^^^^^^^^^^^^^^^^^^

#. Get a local copy of this repository: either clone it or download and extract `ZIP of latest <https://github.com/plu5/retype/archive/main.zip>`_   
#. Install `uv <https://docs.astral.sh/uv/>`_ and a supported Python (3.10--3.14).
#. From the repository root, run ``uv sync --locked --all-groups`` in the isolated project environment.
#. Run ``uv run --locked --group build python setup.py b``; help text will print with the build options you can use. For example, ``uv run --locked --group build python setup.py b -k onedir`` will build retype with pyinstaller in onedir mode.

The output will be in ``/dist``.

Running from sources
^^^^^^^^^^^^^^^^^^^^

#. Get a local copy of this repository: either clone it or download and extract `ZIP of latest <https://github.com/plu5/retype/archive/main.zip>`_   
#. Install ``uv`` and a supported Python (3.10--3.14).
#. From the repository root, run ``uv sync --locked`` in the isolated project environment.
#. Run ``uv run --locked bin/retype``. On Windows, you can simply double-click on ``bin/retype.pyw``. From a console, you can run ``uv run --locked python bin/retype``.

Dependencies
^^^^^^^^^^^^

**Required:**

- Python 3.10--3.14 (the declared range is ``>=3.10,<3.15``)
- ``PyQt5``
- ``ebooklib``
- ``tinycss2``

Install these from the locked ``pyproject.toml`` contract with ``uv sync --locked``;
do not install them into global Python. See ``CONTRIBUTING.md`` for the macOS
contributor and native visual-evidence workflow.

**Optional:**

- ``pywin32`` -- Windows-only. This is only used for optionally hiding the System Console window.
- ``pytest`` and ``pytest-qt`` -- to run pure and offscreen GUI tests
- ``pyinstaller`` and ``setuptools`` -- to build retype
- ``Sphinx`` and ``sphinx-rtd-theme`` -- to build the docs locally
  
Getting started
^^^^^^^^^^^^^^^
 
When you launch retype, you should see 5 epub books that it comes with of short classic works. You can begin reading one of them by clicking on its cover or entering ``>load #`` into the console, where ``#`` is the numerical id of the book which can be seen above the cover.

Type to progress through the book. You can see your current speed in words per minute on the graph above the modeline and your personal best.

Other than typing, you can navigate the book with toolbar buttons and console commands.

You can add more library search paths and customise retype’s operation in the Customisation Dialog, which can be accessed from the menu or by :kbd:`Ctrl+O`.

CharaChorder chord library
^^^^^^^^^^^^^^^^^^^^^^^^^

At startup, *retype* makes one read-only connection to a supported
CharaChorder Two S3 running CCOS 3.x. It reads profile A's layout and complete
chord map, then releases the serial port. Chord hints become available when
that read completes; ordinary typing stays available while it runs.

If the device is disconnected, busy, unsupported, or cannot be read completely,
chord features remain unavailable for that session. The window status message
and application log explain the failure and how to recover (connect the
supported device and restart *retype*). No partial device data is used.

By default, retype teaches at most five unmastered chords at a time, prioritising chords already in progress. Words in the active lesson are highlighted with a dotted underline. Completed chords remain available as hints but are not new teaching targets. To restore the full loaded chord list, uncheck **Limit chord lessons to five unmastered chords (recommended)** in Customisation. The detailed lesson and progress-storage behavior is documented in the Book View reference.

The chording-hints banner above the words you type shows the current or next known chord, followed by upcoming words with known chords. Each hint labels its word-order notation and shows a second device-order row derived from the profile-A layout. The banner is hidden when no device chord library is available.

More information on the user interface and available features can be found in the documentation_.

Keyboard-only chord feedback
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

retype also provides separate keyboard-only timing observations in the Stats
Dock and bounded congratulations for rapid, correct known chord words. The
conditions and device-attribution limitations are documented in the Book View
reference in the documentation_; this feature uses no device access.

-----------------------------
Influences & acknowledgements
-----------------------------

- `QTodoTxt <https://github.com/QTodoTxt/QTodoTxt>`_
- `calibre 3 <https://github.com/kovidgoyal/calibre/tree/v3.48.0>`_
- `Blender <https://github.com/blender/blender>`_
- `Standard Ebooks <https://github.com/standardebooks/>`_
- `Typespeed <https://typespeed.sourceforge.net/>`_
- `Steno Jig <https://github.com/joshuagrams/steno-jig>`_


.. |version-badge| image:: https://img.shields.io/github/v/release/plu5/retype?color=success&label=stable
   :alt: GitHub latest release
   :target: ../../releases/latest
.. |docs-badge| image:: https://img.shields.io/readthedocs/retype
   :alt: Read the Docs
   :target: https://retype.readthedocs.io/
