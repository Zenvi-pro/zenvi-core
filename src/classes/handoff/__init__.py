"""Zenvi Handoff: exchange projects with After Effects, Premiere Pro, Remotion and HyperFrames.

The shared core every handoff builds on (docs/handoff.md has the full picture):

* ``timeline_view``  -- ``TimelineSnapshot``: an immutable, read-only view of a
  project for exporters (tracks, clips, files, curves, effects, transitions).
* ``keyframes``      -- ``Curve``: keyframes evaluated like libopenshot 1.0, with
  per-segment easing as CSS cubic-bezier control points.
* ``transform``      -- clip geometry on the canvas in pixels (libopenshot 1.0).
* ``linked_media``   -- linked clips (``zenvi_link`` on a file): schema, the
  ``LinkProvider`` registry, add / swap / re-render / unlink as one undo step.
* ``alpha``          -- the alpha rule (never WebM) and ffmpeg helpers.
* ``node_runtime``   -- find Node.js and run npm/npx safely off the GUI thread.
* ``jobs``           -- the executor that owns handoff work + a running-job registry.
* ``adobe_link``     -- discover Zenvi Link in After Effects / Premiere and call its MCP tools.
* ``open_source``    -- open a source file at a line in the user's code editor.

Importing this package imports nothing else: modules are loaded on use, and
the pure ones (timeline_view, keyframes, transform, linked_media's schema)
never touch Qt.
"""
