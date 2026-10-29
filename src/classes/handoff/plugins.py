"""Load the handoff packages that are present in this build.

Each handoff package lives in one module with a fixed name; importing it
registers its link provider (``linked_media.register_provider``) and its
menu entries (``ui_registry``). A package that is not in this build yet is
skipped, so the packages never edit a shared list. The imports are written
out (not built from strings) so frozen builds find them.

==============================  ===========================================
``classes.handoff.after_effects``  C2: Zenvi -> After Effects (.jsx export, Send To)
``classes.handoff.premiere``       C3: Premiere FCP7 XML export / import, Send To
``classes.handoff.remotion``       C4: Remotion provider, import / export
``classes.handoff.hyperframes``    C5: HyperFrames provider, import / export
``classes.handoff.aftereffects_link``  C1: re-render After Effects linked clips via Zenvi Link
==============================  ===========================================

Their editor tools live in ``classes.editor_tools.handoff_*`` (loaded by
``classes.editor_tools.handoff``).
"""

from __future__ import annotations

import threading
from typing import List

from classes.logger import log

_loaded: List[str] = []
_lock = threading.Lock()
_done = False


def _missing(exc: ModuleNotFoundError, name: str) -> bool:
    """True when *name* itself is absent (not a module it imports)."""
    return exc.name == name


def load_plugins() -> List[str]:
    """Import every handoff package in this build once; returns the loaded module names.

    A package that fails to import is logged with its traceback and left out
    (its menu entries do not appear); the others still load.
    """
    global _done
    with _lock:
        if _done:
            return list(_loaded)
        _done = True

    def _note(name: str) -> None:
        _loaded.append(name)

    try:
        import classes.handoff.aftereffects_link  # noqa: F401
        _note("classes.handoff.aftereffects_link")
    except ModuleNotFoundError as exc:
        if not _missing(exc, "classes.handoff.aftereffects_link"):
            log.error("Handoff plugin aftereffects_link failed to load", exc_info=True)
    except Exception:
        log.error("Handoff plugin aftereffects_link failed to load", exc_info=True)

    try:
        import classes.handoff.after_effects  # noqa: F401
        _note("classes.handoff.after_effects")
    except ModuleNotFoundError as exc:
        if not _missing(exc, "classes.handoff.after_effects"):
            log.error("Handoff plugin after_effects failed to load", exc_info=True)
    except Exception:
        log.error("Handoff plugin after_effects failed to load", exc_info=True)

    try:
        import classes.handoff.premiere  # noqa: F401
        _note("classes.handoff.premiere")
    except ModuleNotFoundError as exc:
        if not _missing(exc, "classes.handoff.premiere"):
            log.error("Handoff plugin premiere failed to load", exc_info=True)
    except Exception:
        log.error("Handoff plugin premiere failed to load", exc_info=True)

    try:
        import classes.handoff.remotion  # noqa: F401
        _note("classes.handoff.remotion")
    except ModuleNotFoundError as exc:
        if not _missing(exc, "classes.handoff.remotion"):
            log.error("Handoff plugin remotion failed to load", exc_info=True)
    except Exception:
        log.error("Handoff plugin remotion failed to load", exc_info=True)

    try:
        import classes.handoff.hyperframes  # noqa: F401
        _note("classes.handoff.hyperframes")
    except ModuleNotFoundError as exc:
        if not _missing(exc, "classes.handoff.hyperframes"):
            log.error("Handoff plugin hyperframes failed to load", exc_info=True)
    except Exception:
        log.error("Handoff plugin hyperframes failed to load", exc_info=True)

    if _loaded:
        log.info("Handoff plugins: %s", ", ".join(_loaded))
    return list(_loaded)


def loaded_plugins() -> List[str]:
    return list(_loaded)
