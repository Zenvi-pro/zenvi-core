"""Workstream: handoff -- Zenvi <-> After Effects / Premiere Pro / Remotion / HyperFrames, and linked clips.

This module registers the shared handoff tools (Adobe hosts, linked clips);
each handoff package registers its own tools in a sibling module that is
loaded at the bottom of this file when it is present in the build:

=====================================  ====================================
``editor_tools.handoff_after_effects``  C2: export/send to After Effects
``editor_tools.handoff_premiere``       C3: Premiere XML export/send/import
``editor_tools.handoff_remotion``       C4: Remotion list/import/export
``editor_tools.handoff_hyperframes``    C5: HyperFrames import/export
=====================================  ====================================

Each sibling's workstream (``handoff-after-effects`` ...) is mapped in
``editor_tools.WORKSTREAM_OF_MODULE`` and owns its ``handoff.*`` capability
ids in ``coverage.py``. The core lives in ``classes.handoff``.
"""

from __future__ import annotations

from classes.logger import log


def _load_package_tools() -> None:
    """Import the handoff packages' tool modules that exist in this build (written out for frozen builds)."""
    try:
        import classes.editor_tools.handoff_after_effects  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_after_effects":
            raise
    try:
        import classes.editor_tools.handoff_premiere  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_premiere":
            raise
    try:
        import classes.editor_tools.handoff_remotion  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_remotion":
            raise
    try:
        import classes.editor_tools.handoff_hyperframes  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_hyperframes":
            raise
    log.debug("handoff editor tools loaded")


_load_package_tools()
