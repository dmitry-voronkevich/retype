Running from sources
====================

#. Get a local copy of `the code repository <https://github.com/plu5/retype>`_: either clone it or download and extract `ZIP of latest <https://github.com/plu5/retype/archive/main.zip>`_   
#. Install ``uv`` and a supported Python (3.10--3.14).
#. From the repository root, run ``uv sync --locked`` in the isolated project environment.
#. Run ``uv run --locked bin/retype``. On Windows, you can simply double-click on ``bin/retype.pyw``. Alternatively, you can run ``uv run --locked python retype-target.py``.
