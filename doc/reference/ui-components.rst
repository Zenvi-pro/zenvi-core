UI Components
=============

This page defines reusable presentation patterns for documentation content.
Use these examples to keep guidance clear, scannable, and accessible.

Callouts
--------

.. note::

   **Tip:** Prefer short paragraphs and lists inside callouts. Long blocks are
   hard to scan on mobile.

.. warning::

   **Heads up:** Export quality settings can drastically increase render time.
   Validate bitrate presets on a short preview first.

.. important::

   Keep user-facing terminology consistent with in-app labels:
   *Timeline*, *Assistant*, *Export*, and *Project Files*.

Badges and Labels
-----------------

Use inline roles to mark state and release channel:

- :guilabel:`Stable`
- :guilabel:`Experimental`
- :kbd:`Ctrl` + :kbd:`S`

Tables
------

.. list-table:: Quick settings matrix
   :header-rows: 1
   :widths: 24 38 38

   * - Setting
     - Recommended default
     - Why it matters
   * - Preview Resolution
     - 720p
     - Keeps playback responsive on most systems.
   * - Proxy Mode
     - Auto
     - Balances smooth timeline playback with final quality.
   * - Export Preset
     - Match source FPS
     - Reduces motion stutter and frame conversion artifacts.

Code Blocks
-----------

Zenvi docs should prefer clear command samples with expected context.

.. code-block:: bash

   # Build docs locally
   cd doc
   make html

   # Open generated site
   xdg-open _build/html/index.html
