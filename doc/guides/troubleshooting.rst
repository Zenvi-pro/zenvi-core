Troubleshooting
===============

Common issues and the fastest known fixes.

Playback stutters on long timelines
-----------------------------------

.. tip::

   Enable proxy editing and lower preview resolution while trimming.

1. Open project settings and set preview to 720p.
2. Enable proxy media for 4K source clips.
3. Close heavy background apps during renders.

Search cannot find expected topic
---------------------------------

.. note::

   The docs search index is generated during ``make html``. Rebuild docs after
   large content updates.

- Use shorter keywords (for example, ``proxy`` instead of
  ``proxy media rendering``).
- Check section headings first; search relevance prioritizes headings.
- Open the docs from ``_build/html/index.html`` to ensure JS assets loaded.

Export has unexpected quality
-----------------------------

.. warning::

   Platform recompression can reduce visual quality. Validate with a short test
   export before final render.

* Match export frame rate to source footage.
* Use two-pass presets for bitrate-sensitive delivery.
* Verify color space and audio sample rate in export settings.
