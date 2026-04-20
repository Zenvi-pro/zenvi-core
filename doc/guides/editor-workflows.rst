Editor Workflows
================

Zenvi focuses on a fast loop: import, rough cut, polish, and export.

Build a clean rough cut
-----------------------

.. rst-class:: zv-callout zv-callout-tip

Use ``Q`` and ``W`` to trim around the playhead quickly while reviewing footage.

1. Import clips into the project panel.
2. Add your A-roll to the timeline.
3. Create a rough cut by trimming obvious dead space first.
4. Add B-roll and music in secondary tracks.

AI-assisted tools
-----------------

.. rst-class:: zv-callout zv-callout-info

Assistant suggestions work best with concise prompts that include tone, target platform, and clip length.

Example prompt:

.. code-block:: text

   Build a 30-second upbeat recap for Instagram Reels.
   Keep pacing fast and add lower-third captions for speakers.

Export checklist
----------------

.. list-table:: Before exporting
   :header-rows: 1
   :widths: 45 55

   * - Check
     - Why it matters
   * - Frame rate matches source
     - Avoids stutter and cadence artifacts.
   * - Audio peaks below -1 dB
     - Prevents clipping after platform re-encoding.
   * - Captions enabled
     - Improves accessibility and watch time.
