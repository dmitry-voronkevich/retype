Build docs locally
==================

#. Get a local copy of `the code repository <https://github.com/plu5/retype>`_: either clone it or download and extract `ZIP of latest <https://github.com/plu5/retype/archive/main.zip>`_
#. From the repository root, install the isolated locked docs environment with ``uv sync --locked --all-groups``
#. cd into the ``docs`` folder
#. Run ``uv run --locked --group docs sphinx-build -M html source build`` (or ``make html``), or on Windows ``./make.bat html``
#. If build succeeds, open ``build/html/index.html`` in your browser

Between builds you may sometimes have to ``make clean`` for things to work properly, for example when updating public files like images or css.

There are a few things that don’t seem to work locally, that do work on readthedocs:

- No twisties to open subsections in the sidebar
- When you navigate to another section or subsection, the sidebar does not automatically scroll to where you were
