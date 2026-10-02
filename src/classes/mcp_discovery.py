"""Discovery files: where a running Zenvi's MCP server can be reached.

While its in-app MCP server is listening, the desktop window writes
``~/.openshot_qt/gui_mcp.json`` and a ``launch.py --headless`` session writes
``~/.openshot_qt/headless_mcp.json``, so an external CLI never has to guess the
port (the server falls back from 7434 to an ephemeral port when 7434 is taken)::

    {
      "pid": 4242,
      "project": "/Users/me/Movies/cut.zvn",
      "token_file": "/Users/me/.openshot_qt/mcp_token",
      "url": "http://127.0.0.1:7434/mcp",
      "version": "1.0.188"
    }

``project`` is null while the project is untitled. The bearer token itself stays
in ``token_file`` (created 0600 by the server); this file only points at it.

Each file is replaced atomically and is readable only by its owner. It is
removed on a clean exit, but a crashed process leaves it behind, so a reader
must treat ``pid`` and ``url`` as a claim to verify, not a fact.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

GUI = "gui"
HEADLESS = "headless"
KINDS = (GUI, HEADLESS)


def discovery_path(user_dir: str, kind: str) -> str:
    """``<user_dir>/gui_mcp.json`` or ``<user_dir>/headless_mcp.json``."""
    if kind not in KINDS:
        raise ValueError("unknown discovery kind %r (expected one of %s)" % (kind, ", ".join(KINDS)))
    return os.path.join(user_dir, "%s_mcp.json" % kind)


def build_payload(url: str, token_file: str, pid: int, project: str | None, version: str) -> dict:
    """The file's contents: exactly these five keys."""
    return {
        "url": url,
        "token_file": os.path.abspath(token_file),
        "pid": int(pid),
        "project": os.path.abspath(project) if project else None,
        "version": version,
    }


def write(path: str, payload: dict) -> None:
    """Replace *path* with *payload* in one step, readable only by this user.

    The JSON goes to a private temporary file in the same directory first
    (``mkstemp`` creates it 0600), is flushed to disk, and is then renamed over
    *path* -- a reader sees the old file or the new one, never half of one.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace(src: str, dst: str, attempts: int = 5) -> None:
    # Windows refuses to rename over a file another process has open at that
    # instant (a CLI reading it); that is transient, so retry briefly.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == attempts - 1:
                raise
            time.sleep(0.05)


def read(path: str) -> dict | None:
    """The parsed file, or None if it is missing or not a JSON object."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove(path: str, pid: int) -> bool:
    """Delete *path* if it still describes process *pid*; True if it was removed.

    Never deletes a file another process wrote after us, so a late exit cannot
    take down a newer instance's entry.
    """
    data = read(path)
    if data is None or data.get("pid") != pid:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True
