"""Whether an assistant turn is running in this editor.

The editor MCP serves tools for external harnesses that drive Zenvi
(send_assistant_prompt_tool, zenvi_api_request). The CLI agents Zenvi runs in
its own chat dock connect to that same MCP, so those tools check this and
refuse while a turn runs: the assistant must never prompt itself or call the
backend API on the user's behalf.
"""

import contextlib
import functools
import threading

_lock = threading.Lock()
_running = 0


@contextlib.contextmanager
def turn():
    global _running
    with _lock:
        _running += 1
    try:
        yield
    finally:
        with _lock:
            _running -= 1


def running() -> bool:
    with _lock:
        return _running > 0


def counts_as_turn(func):
    """Mark a turn's entry point (a chat worker's or a CLI runner's run_request)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with turn():
            return func(*args, **kwargs)

    return wrapper


REFUSAL = ("Error: an assistant turn is running in this editor. This tool is for external "
           "harnesses driving Zenvi; the assistant cannot use it on itself.")
