"""Which Remotion project folders the user agreed to run this session.

Reading sizes and props, rendering and Remotion Studio run the project's
own code (``remotion.config.*``, its components) in Node.js and a headless
browser, like ``npx remotion render``. Zenvi asks once per project folder
(its real path) per session before doing that for something the user
clicked -- File > Import Project, a re-render from the toolbar pill, Linked
Source > Re-render / Edit Props / Open in Studio -- and remembers the
answer until it quits. A project imported this session is already trusted.

Work an agent asked for keeps the tools' documented behaviour (their
descriptions say they run the project's code): it is not gated. The two are
told apart by where the work runs -- Zenvi's own menus and dialogs hand it
to ``handoff.jobs.submit_job`` (the ``handoff`` executors), while tool calls
run it on their own thread with ``jobs.track_job``.

The question itself is a Qt message box (``dialogs.ask_trust``), shown on
the GUI thread while the job waits for the answer.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from classes.handoff.jobs import JobCancelled
from classes.logger import log

# handoff.jobs' executors (ThreadPoolExecutor names its threads "<prefix>_<n>")
UI_THREAD_PREFIXES = ("handoff", "handoff-ui", "handoff-check")
ANSWER_TIMEOUT = 15 * 60      # seconds a job waits for the user's answer

_lock = threading.Lock()
_trusted: set = set()
# Shows the question on the GUI thread: (project root, project name, action, trust key) -> True to run.
_asker: Optional[Callable[[str, str, str, str], bool]] = None


class NotTrusted(JobCancelled):
    """The user chose not to run the project's code: handled like a cancel (nothing changes)."""


def trust_key(root: str) -> str:
    """The key a project folder is trusted under (its real path). Blocking (resolves links)."""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(root or ""))))


def is_trusted(key: str) -> bool:
    with _lock:
        return key in _trusted


def remember(key: str) -> None:
    """Trust *key* (a :func:`trust_key`) for the rest of the session."""
    with _lock:
        _trusted.add(key)


def trust_folder(root: str) -> None:
    remember(trust_key(root))


def reset() -> None:
    """Forget every answer (tests)."""
    with _lock:
        _trusted.clear()


def set_asker(asker: Optional[Callable[[str, str, str, str], bool]]) -> None:
    """Install the GUI question (``dialogs`` does, on first use); None restores the default."""
    global _asker
    _asker = asker


def ui_initiated() -> bool:
    """True on a ``handoff.jobs`` executor thread: work the user started from Zenvi's menus, dialogs or pill."""
    name = threading.current_thread().name
    return name.rsplit("_", 1)[0] in UI_THREAD_PREFIXES


def _default_asker(root: str, name: str, action: str, key: str) -> bool:
    from classes.qt_main_thread import call_on_gui

    def _ask() -> bool:
        from classes.handoff.remotion import dialogs
        yes = bool(dialogs.ask_trust_for(root, name, action))
        if yes:  # recorded here too: a "yes" after the job stopped waiting still counts for the next click
            remember(key)
        return yes

    return bool(call_on_gui(_ask, timeout=ANSWER_TIMEOUT))


def require(project_root: str, project_name: str, *, action: str) -> None:
    """Before running *project_root*'s code for something the user clicked: ask once per folder per session.

    Returns when the folder is trusted (or the work is not the user's click:
    agent tools keep their documented behaviour). Raises :class:`NotTrusted`
    (a ``JobCancelled``) when the user says no -- or does not answer within
    :data:`ANSWER_TIMEOUT`; a batch (several clips) stops at that first "no",
    and the next click asks again. Blocking: call from the job, never the GUI
    thread.
    """
    if not ui_initiated():
        return
    key = trust_key(project_root)
    if is_trusted(key):
        return
    asker = _asker or _default_asker
    try:
        yes = asker(project_root, project_name, action, key)
    except TimeoutError:
        yes = False
        log.info("No answer to the trust question for %s; not running it", project_root)
    if yes:
        remember(key)
        return
    raise NotTrusted(f"{project_name} was not run: you chose not to run its code")


__all__ = ["NotTrusted", "trust_key", "is_trusted", "remember", "trust_folder", "reset", "set_asker", "require",
           "ui_initiated", "UI_THREAD_PREFIXES"]
