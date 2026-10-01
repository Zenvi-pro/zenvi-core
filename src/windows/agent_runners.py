"""Pluggable agent backends for the chat dock.

Each runner is a ``QObject`` worker (moved onto a ``QThread`` by
``AIChatWindow._make_worker``) that exposes the *same* six signals and the
``run_request(...)`` / ``clear_session()`` slots as the built-in
``AIChatWorker`` ΓÇö so the existing chat rendering works unchanged regardless of
which backend produced the events.

The CLI runners (Claude Code, Codex, Cursor CLI) spawn the agent CLI as a headless
subprocess in streaming-JSON mode and point it at the in-app MCP server
(:mod:`classes.agent_mcp_server`) so the agent can drive the editor using the
same tools the built-in assistant uses. They keep their own file/shell/web
tools too (full agent abilities).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

from qt_api import QObject, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


# Backend identifiers (kept in sync with ai_chat_ui constants).
BACKEND_CLAUDE = "claude_code"
BACKEND_CODEX = "codex"
BACKEND_CURSOR = "cursor_cli"


# Models offered in the chat model picker per backend, in menu order. ``id`` is
# passed straight through to the CLI's ``--model`` flag; Claude Code accepts a
# full model name or a latest-alias ("opus", "sonnet"), and we use full names so
# the picker keeps meaning the same model after a new release ships.
#
# The lineup the user sees comes from the Zenvi backend's ``GET /models/cli``
# whenever it has answered (``set_live_lineups``): the backend builds it from
# each provider's live model list, so a release shows up in the picker the
# next time it refreshes, with no desktop update.  Each runner's ``MODELS`` is
# the built-in fallback for when the backend is unreachable or too old to
# serve the route.
#
# A backend with an empty list hides the model pill and lets the CLI use
# whatever its own config selects. Codex's built-in list is empty because we
# do not track the OpenAI lineup here; the live one fills it in.
_live_lineups: dict = {}
_live_lineups_lock = threading.Lock()

_PICKER_KEYS = ("id", "name", "provider", "featured", "rank", "tags", "default")


def _clean_lineup(rows) -> list:
    """Keep only well-formed picker entries; the backend payload is data."""
    out = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {k: row[k] for k in _PICKER_KEYS if k in row}
        entry.setdefault("name", mid)
        out.append(entry)
    return out


def set_live_lineups(lineups: dict) -> None:
    """Install the backend-served lineups (``{backend_id: [entries]}``).

    An empty or missing list for a backend means "nothing to offer", and the
    built-in list takes over for it; passing ``{}`` clears everything.
    """
    cleaned = {}
    for backend, rows in (lineups or {}).items():
        rows = _clean_lineup(rows)
        if rows:
            cleaned[backend] = rows
    with _live_lineups_lock:
        _live_lineups.clear()
        _live_lineups.update(cleaned)


def live_lineup_for(backend: str) -> list:
    """The backend-served list for *backend*, or ``[]`` when none has landed."""
    with _live_lineups_lock:
        return [dict(m) for m in _live_lineups.get(backend, [])]


# The picker entry that passes no --model, so the CLI's own config decides
# (issue #136: a clear choice instead of a hidden pill).
CLI_DEFAULT_MODEL_ID = "cli-default"


def _cli_default_entry(uses: str = "") -> dict:
    """"CLI default", tagged with the model the CLI says it would use."""
    entry = {"id": CLI_DEFAULT_MODEL_ID, "name": "CLI default", "rank": 0,
             "featured": True, "default": True}
    if uses:
        entry["tags"] = [uses]
    return entry


# Lineups a CLI reported about itself (``cursor-agent models``), for a backend
# whose models depend on the user's account. The backend's lineup still wins.
_cli_lineups: dict = {}


def set_cli_lineup(backend: str, rows) -> bool:
    """Install what *backend*'s CLI listed; True when that changed the list.

    An empty list is ignored so a failed read keeps the previous lineup.
    """
    rows = _clean_lineup(rows)
    if not rows:
        return False
    with _live_lineups_lock:
        if _cli_lineups.get(backend) == rows:
            return False
        _cli_lineups[backend] = rows
    return True


def models_for_backend(backend: str) -> list:
    """Model-picker entries for *backend* (see ``setModels`` in chat.js)."""
    live = live_lineup_for(backend)
    if live:
        return live
    with _live_lineups_lock:
        listed = [dict(m) for m in _cli_lineups.get(backend, [])]
    if listed:
        return listed
    if backend == BACKEND_CLAUDE:
        return [dict(m) for m in ClaudeCodeRunner.MODELS]
    if backend == BACKEND_CODEX:
        return [dict(m) for m in CodexRunner.MODELS]
    if backend == BACKEND_CURSOR:
        return [dict(m) for m in CursorCliRunner.MODELS]
    return []


def _resolved_home() -> str:
    """Windows-safe home directory (never the literal ``~``)."""
    try:
        from classes.info import HOME_PATH
        if HOME_PATH and HOME_PATH not in ("~", "~/") and not HOME_PATH.startswith("~" + os.sep):
            return HOME_PATH
    except Exception:
        pass
    home = os.path.expanduser("~")
    if home and home != "~":
        return home
    return os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""


def _cli_install_dirs() -> list:
    """Well-known locations for native Claude Code / Codex installs.

    Codex's Windows installer puts ``codex.exe`` under
    ``~/.codex/packages/standalone/current/bin`` and does *not* always add
    that folder to PATH ΓÇö so ``shutil.which("codex")`` fails even when the
    CLI is installed and logged in.
    """
    home = _resolved_home()
    dirs = []
    if home:
        dirs.append(os.path.join(home, ".local", "bin"))
        dirs.append(os.path.join(home, ".codex", "packages", "standalone", "current", "bin"))
    appdata = os.environ.get("APPDATA") or (
        os.path.join(home, "AppData", "Roaming") if home else ""
    )
    if appdata:
        dirs.append(os.path.join(appdata, "npm"))
    local = os.environ.get("LOCALAPPDATA") or (
        os.path.join(home, "AppData", "Local") if home else ""
    )
    if local:
        dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    return dirs


def _in_cursor_install(path: str) -> bool:
    """True if *path* is, or links to, a file inside a Cursor agent install."""
    folders = re.split(r"[\\/]", os.path.dirname(os.path.realpath(path)))
    return "cursor-agent" in (f.lower() for f in folders)


def _cursor_cli_candidates() -> list:
    """Where the Cursor installers put the CLI, for when it is not on PATH.

    macOS/Linux: ``~/.local/bin/{cursor-agent,agent}``, symlinks into
    ``~/.local/share/cursor-agent/versions/<version>/``. Windows:
    ``%LOCALAPPDATA%\\cursor-agent\\{cursor-agent,agent}.cmd``. A GUI app's
    PATH often has neither folder. (The ``.ps1`` launchers beside the
    ``.cmd`` ones are left out: Popen cannot start a PowerShell script.)
    """
    home = _resolved_home()
    out = []
    if home:
        out.append(os.path.join(home, ".local", "bin", "cursor-agent"))
        out.append(os.path.join(home, ".local", "bin", "agent"))
    local = os.environ.get("LOCALAPPDATA") or (
        os.path.join(home, "AppData", "Local") if home else ""
    )
    if local:
        root = os.path.join(local, "cursor-agent")
        out += [os.path.join(root, name) for name in (
            "cursor-agent.cmd", "cursor-agent.exe", "agent.cmd", "agent.exe")]
    if home:
        # The ~/.local/bin links are gone but a version is still installed;
        # version folders are named by date, so the newest sorts last.
        versions = os.path.join(home, ".local", "share", "cursor-agent", "versions")
        try:
            names = sorted((n for n in os.listdir(versions) if not n.startswith(".")),
                           reverse=True)
        except OSError:
            names = []
        out += [os.path.join(versions, name, "cursor-agent") for name in names]
    return out


def _which_cursor_cli():
    """Find Cursor's CLI, never some other program that happens to be named ``agent``.

    ``cursor-agent`` is Cursor's own name. ``agent`` is the alias Cursor's
    docs now lead with, but it is too generic to trust unless it resolves
    into a Cursor install.
    """
    exts = ("", ".cmd", ".exe") if os.name == "nt" else ("",)
    for ext in exts:
        found = shutil.which("cursor-agent" + ext)
        if found:
            return found
    for ext in exts:
        found = shutil.which("agent" + ext)
        if found and _in_cursor_install(found):
            return found
    for path in _cursor_cli_candidates():
        if not os.path.isfile(path):
            continue
        if os.path.basename(path).lower().startswith("cursor-agent") or _in_cursor_install(path):
            return path
    return None


def _bash_major(path):
    """Major version of a bash binary, or 0 if it cannot be probed."""
    try:
        result = subprocess.run(
            [path, "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
            capture_output=True, text=True, timeout=2,
        )
        return int((result.stdout or "").strip() or 0)
    except Exception:
        return 0


def _bash_candidates():
    """Possible bash binaries; Homebrew/local first so they beat /bin/bash 3.2."""
    home = _resolved_home()
    out = ["/opt/homebrew/bin/bash", "/usr/local/bin/bash"]
    if home:
        out.append(os.path.join(home, ".local", "bin", "bash"))
    which = shutil.which("bash")
    if which:
        out.append(which)
    seen = set()
    uniq = []
    for path in out:
        if path not in seen:
            seen.add(path)
            uniq.append(path)
    return uniq


@functools.lru_cache(maxsize=1)
def _resolve_cli_bash():
    """A bash ΓëÑ4 binary for Claude Code's Bash tool, or None.

    macOS ``/bin/bash`` is 3.2 (no associative arrays). Pointing
    ``CLAUDE_CODE_SHELL`` at it would break ``declare -A``. Claude Code's
    documented override is ``CLAUDE_CODE_SHELL``; ``$SHELL`` alone is often
    ignored in favour of zsh auto-detection.
    """
    if os.name == "nt":
        return None
    for path in _bash_candidates():
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        if _bash_major(path) >= 4:
            return path
    return None


def _agent_bash_prompt():
    """Recipes the Claude Code Bash tool has already failed on (HEIC, zsh)."""
    return (
        "Media conversion (Bash tool):\n"
        "- HEIC/HEIF stills: never ffmpeg -vf on the HEIC itself. ffmpeg 7 "
        "decodes HEIC through a complex filtergraph; combining that with -vf "
        "fails with 'Simple and complex filtering cannot be used together'.\n"
        "- macOS: sips -s format jpeg IN.HEIC --out OUT.jpg, then scale the "
        "jpeg with -vf if needed.\n"
        "- Fallback: ffmpeg -i IN.HEIC -filter_complex "
        "'[0:v:0]scale=W:-2[o]' -map '[o]' -frames:v 1 -update 1 -y OUT.jpg\n"
        "- This Bash tool may still run zsh on macOS. Never use bash "
        "${!assoc[@]} key expansion (zsh reports 'bad substitution'). "
        "Iterate a plain path list, or zsh: "
        'for name in "${(@k)files}"; do ...; done.'
    )


def _cli_child_env(extra=None):
    """Environment for a Windows-native Claude/Codex subprocess.

    Login state lives under USERPROFILE (``C:\\Users\\...``), not MSYS HOME.
    """
    env = dict(os.environ)
    home = _resolved_home()
    if os.name == "nt" and home and not home.startswith("~"):
        env["USERPROFILE"] = home
        drive, tail = os.path.splitdrive(os.path.abspath(home))
        if drive:
            env.setdefault("HOMEDRIVE", drive)
        if tail:
            env.setdefault("HOMEPATH", tail)
        env.setdefault("APPDATA", os.path.join(home, "AppData", "Roaming"))
        env.setdefault("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    # Claude Code auto-detects zsh on macOS; bash ΓëÑ4 makes ${!files[@]} work.
    # Do not clobber a user-set CLAUDE_CODE_SHELL.
    if "CLAUDE_CODE_SHELL" not in env:
        bash = _resolve_cli_bash()
        if bash:
            env["CLAUDE_CODE_SHELL"] = bash
            env["SHELL"] = bash
    if extra:
        env.update(extra)
    return env


def _add_dir_args():
    """``--add-dir`` flags for typical footage folders (Desktop, Downloads, ΓÇª).

    cwd stays the project / agent_workspace so the CLI is not rooted at
    ``$HOME``; these extra dirs let Glob/Read see local media the user points at.
    """
    try:
        from classes.file_drop import media_add_dirs
        dirs = media_add_dirs(_resolved_home())
    except Exception:
        dirs = []
    args = []
    for path in dirs:
        args.extend(["--add-dir", path])
    return args


def _which_cli(binary_name: str):
    """Locate ``claude`` / ``codex`` / ``cursor-agent`` on PATH or in known install dirs."""
    if binary_name == "cursor-agent":
        return _which_cursor_cli()
    found = shutil.which(binary_name)
    if found:
        return found
    names = [binary_name]
    if os.name == "nt":
        names.extend([binary_name + ".exe", binary_name + ".cmd", binary_name + ".bat"])
        for name in names[1:]:
            found = shutil.which(name)
            if found:
                return found
    for directory in _cli_install_dirs():
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _agent_mcp_dir() -> str:
    from classes import info
    path = os.path.abspath(os.path.join(info.USER_PATH, "agent_mcp"))
    os.makedirs(path, exist_ok=True)
    return path


def _project_cwd() -> str:
    """Working directory for the agent ΓÇö the current project's folder if any.

    With no saved project the fallback is a scratch folder under the user's
    Zenvi data dir, never ``$HOME``: these CLIs run with approvals and sandbox
    bypassed, and an unsaved project is the state the app launches in, so a
    home-rooted cwd would hand the agent unattended write access to
    everything the user owns.
    """
    try:
        from classes.app import get_app
        fp = getattr(get_app().project, "current_filepath", "") or ""
        if fp and os.path.isdir(os.path.dirname(fp)):
            return os.path.abspath(os.path.dirname(fp))
    except Exception:
        pass
    from classes import info
    path = os.path.abspath(os.path.join(info.USER_PATH, "agent_workspace"))
    os.makedirs(path, exist_ok=True)
    return path


def _strip_mcp_prefix(name: str) -> str:
    """``mcp__zenvi-editor__add_track_tool`` -> ``add_track_tool`` (native tools unchanged)."""
    if name and name.startswith("mcp__"):
        return name.split("__")[-1]
    return name or ""


def detect_cli(binary_name: str) -> dict:
    """Check whether *binary_name* (``claude`` or ``codex``) is on PATH, and
    (if installed) whether Zenvi's MCP server is already registered with it.

    Runs synchronously with short timeouts; callers on the GUI thread must
    offload this to a background thread (see ``AIChatWindow``'s detection
    worker) rather than call it directly.
    """
    cli = _which_cli(binary_name)
    if not cli:
        return {"installed": False, "version": None, "registered": False}
    version = None
    try:
        result = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=3
        )
        version = (result.stdout or result.stderr or "").strip() or None
    except Exception:
        version = None
    return {"installed": True, "version": version, "registered": _is_registered(binary_name)}


def _is_registered(binary_name: str) -> bool:
    if binary_name == "claude":
        return _claude_is_registered()
    if binary_name == "codex":
        return _codex_is_registered()
    if binary_name == "cursor-agent":
        return _cursor_is_registered()
    return False


def _claude_config_path() -> str:
    return os.path.join(_resolved_home(), ".claude.json")


def _claude_is_registered() -> bool:
    """Check the ``mcpServers`` table in ``~/.claude.json`` directly.

    ``claude mcp list`` also reports this, but it live health-checks every
    configured server (including ones needing OAuth) before printing
    anything ΓÇö slow and network-dependent, and observed to occasionally
    exceed a reasonable subprocess timeout right after a fresh registration,
    which would misreport a real registration as absent. Reading the config
    file is instant and has no such race.
    """
    try:
        with open(_claude_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "zenvi" in (data.get("mcpServers") or {}):
            return True
    except Exception:
        pass
    return _claude_is_registered_via_cli()


def _claude_is_registered_via_cli() -> bool:
    """Fallback for when the config file can't be read directly: ask the CLI.
    ``claude mcp list`` prints one ``<name>: <url> (<transport>) - <status>``
    line per server; anchor on the colon so we don't false-match some other
    server whose URL/args happen to contain the substring "zenvi"."""
    try:
        result = subprocess.run(
            [_which_cli("claude") or "claude", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return "zenvi:" in (result.stdout or "")
    except Exception:
        return False


def _codex_config_path() -> str:
    return os.path.join(_resolved_home(), ".codex", "config.toml")


def _codex_is_registered() -> bool:
    path = _codex_config_path()
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return "zenvi_editor" in (data.get("mcp_servers") or {})
    except Exception:
        return False


def register_claude(port: int, token: str):
    """Register Zenvi's MCP server with the ``claude`` CLI (user scope).

    Idempotent: removes any prior ``zenvi`` registration first (ignoring
    failure ΓÇö it's fine if none existed) so re-running this after the port
    changed (e.g. a fallback-port restart) cleanly replaces the old entry
    rather than erroring on a duplicate name.

    Returns ``(ok, message)``.
    """
    try:
        claude = _which_cli("claude") or "claude"
        subprocess.run(
            [claude, "mcp", "remove", "-s", "user", "zenvi"],
            capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            [claude, "mcp", "add", "--transport", "http", "zenvi",
             "http://127.0.0.1:%d/mcp" % port,
             "--header", "Authorization: Bearer %s" % token,
             "--scope", "user"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, "Connected. Run `claude` in your terminal to use it."
        return False, (result.stderr or result.stdout or "claude mcp add failed").strip()
    except Exception as e:
        return False, str(e)


_CODEX_SECTION_HEADER = "[mcp_servers.zenvi_editor]"


def _codex_desired_section(port: int) -> str:
    return (
        '%s\n'
        'url = "http://127.0.0.1:%d/mcp"\n'
        'bearer_token_env_var = "ZENVI_MCP_TOKEN"\n'
    ) % (_CODEX_SECTION_HEADER, port)


def register_codex(port: int, token: str):
    """Write/update the ``[mcp_servers.zenvi_editor]`` table in
    ``~/.codex/config.toml`` (Codex has no CLI command for registering an
    HTTP-transport MCP server ΓÇö only stdio servers via ``codex mcp add``;
    confirmed against the current Codex CLI docs).

    Validates the file both before and after editing, and writes a
    ``.zenvi-backup`` copy first ΓÇö this mutates a config file we don't fully
    control the rest of the schema/contents of, so failing safe matters more
    than convenience here.

    The token itself is never written to the file (Codex reads it from the
    ``ZENVI_MCP_TOKEN`` env var at runtime via ``bearer_token_env_var``), so
    the returned message tells the caller to export it before running codex.

    Returns ``(ok, message)``.
    """
    path = _codex_config_path()
    try:
        import tomllib
    except ImportError:
        return False, "Python 3.11+ (tomllib) is required to edit config.toml."

    original = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except Exception as e:
            return False, "Failed to read ~/.codex/config.toml: %s" % e
        try:
            tomllib.loads(original)
        except Exception as e:
            return False, "~/.codex/config.toml has invalid TOML, not touching it: %s" % e

    new_section = _codex_desired_section(port)
    if _CODEX_SECTION_HEADER in original:
        pattern = re.compile(re.escape(_CODEX_SECTION_HEADER) + r".*?(?=\n\[|\Z)", re.DOTALL)
        updated = pattern.sub(new_section.rstrip("\n"), original, count=1)
    else:
        if original and not original.endswith("\n"):
            original += "\n"
        sep = "\n" if original else ""
        updated = original + sep + new_section

    try:
        tomllib.loads(updated)
    except Exception as e:
        return False, "Generated config would be invalid TOML, aborting: %s" % e

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if original:
            with open(path + ".zenvi-backup", "w", encoding="utf-8") as fh:
                fh.write(original)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except Exception as e:
        return False, "Failed to write ~/.codex/config.toml: %s" % e

    return True, (
        "Updated ~/.codex/config.toml. Before running codex, run:\n"
        "export ZENVI_MCP_TOKEN=%s"
    ) % token


# The one entry in ~/.cursor/mcp.json that is Zenvi's. A server the user
# named "zenvi" is theirs, not ours.
_CURSOR_MCP_NAME = "zenvi-editor"


def _cursor_mcp_path() -> str:
    return os.path.join(_resolved_home(), ".cursor", "mcp.json")


def _cursor_is_registered() -> bool:
    path = _cursor_mcp_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _CURSOR_MCP_NAME in (data.get("mcpServers") or {})
    except Exception:
        return False


# cursor-agent expands ${env:NAME} in headers, so the bearer token never has to
# sit in a file the Cursor editor shares. CursorCliRunner sets the variable.
_CURSOR_TOKEN_ENV = "ZENVI_MCP_TOKEN"


def _cursor_server_entry(port: int) -> dict:
    return {
        "url": "http://127.0.0.1:%d/mcp" % port,
        "headers": {"Authorization": "Bearer ${env:%s}" % _CURSOR_TOKEN_ENV},
    }


def _write_with_mode(path: str, text: str, mode: int) -> None:
    """Write *path* with *mode* from the start, not the umask's 0644 first."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)   # O_CREAT's mode only applies to a file it creates


def register_cursor(port: int, token: str):
    """Write or replace Zenvi's HTTP server in ``~/.cursor/mcp.json``.

    Cursor's CLI has no ``mcp add`` for an HTTP server; it reads this file,
    which the Cursor editor reads too. Only the ``zenvi-editor`` entry is
    touched: it is added, or updated in place when a restart moved the port,
    and every other server is left as it was. An entry that is already
    current is not rewritten, so the check CursorCliRunner makes before each
    turn costs one read. Invalid JSON is refused, never repaired. The entry
    names the token by environment variable (``$ZENVI_MCP_TOKEN``), like the
    Codex registration, rather than storing it.

    The file holds other servers' secrets, so the ``.zenvi-backup`` copy (of
    the file as it was before Zenvi first changed it) and the new file keep
    its permissions (0600 when it is new), and the new content is staged next
    to it and moved into place rather than written over it. A symlinked
    mcp.json (dotfiles) is updated through the link.

    Returns ``(ok, message)``.
    """
    path = os.path.realpath(_cursor_mcp_path())
    connected = (
        "Connected. Before running cursor-agent yourself, run:\n"
        "export %s=%s" % (_CURSOR_TOKEN_ENV, token)
    )
    original = ""
    data = {}
    mode = 0o600
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except Exception as e:
            return False, "Failed to read ~/.cursor/mcp.json: %s" % e
        if original.strip():
            try:
                data = json.loads(original)
            except Exception as e:
                return False, "~/.cursor/mcp.json has invalid JSON, not touching it: %s" % e
            if not isinstance(data, dict):
                return False, "~/.cursor/mcp.json is not a JSON object, not touching it."

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        return False, "~/.cursor/mcp.json mcpServers is not an object, not touching it."

    entry = _cursor_server_entry(port)
    if servers.get(_CURSOR_MCP_NAME) == entry:
        return True, connected
    servers[_CURSOR_MCP_NAME] = entry

    staged = path + ".zenvi-tmp"
    backup = path + ".zenvi-backup"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Only the first: later port moves would back up our own edit.
        if original and not os.path.exists(backup):
            _write_with_mode(backup, original, mode)
        _write_with_mode(staged, json.dumps(data, indent=2) + "\n", mode)
        os.replace(staged, path)
    except Exception as e:
        try:
            os.remove(staged)
        except OSError:
            pass
        return False, "Failed to write ~/.cursor/mcp.json: %s" % e

    return True, connected


# `cursor-agent models` prints "<id> - <name>" per model, flagging the one the
# CLI uses when no --model is given with "(current)" and Cursor's own pick
# with "(default)". Some names end in zero-width spaces.
_CURSOR_MODEL_LINE = re.compile(r"^([A-Za-z0-9][\w.\-]*) - (.+)$")
_CURSOR_MODEL_FLAGS = re.compile(
    r"\s*\(((?:current|default)(?:\s*,\s*(?:current|default))*)\)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_cursor_models(text: str) -> list:
    """Picker entries from ``cursor-agent models`` output, in the CLI's order.

    "CLI default" comes first and is preselected, tagged with the model the
    CLI says it would use. The list depends on the account and runs to
    hundreds of ids, so nothing else is featured; search reaches the rest.
    """
    rows, seen = [], set()
    current = fallback = ""
    for line in _ANSI.sub("", text or "").splitlines():
        match = _CURSOR_MODEL_LINE.match(line.replace("​", "").strip())
        if not match or match.group(1) in seen:
            continue
        mid, name = match.group(1), match.group(2)
        seen.add(mid)
        flags = set()
        marks = _CURSOR_MODEL_FLAGS.search(name)
        if marks:
            flags = {f.strip() for f in marks.group(1).split(",")}
            name = name[:marks.start()]
        name = " ".join(name.split()) or mid
        rows.append({"id": mid, "name": name, "rank": len(rows) + 1, "featured": False})
        if "current" in flags and not current:
            current = name
        if "default" in flags and not fallback:
            fallback = name
    if not rows:
        return []
    return [_cli_default_entry(current or fallback)] + rows


def probe_cursor_models(cli: str) -> list:
    """Ask ``cursor-agent models`` for this account's lineup.

    A network round trip (about 3 s): never call it on the GUI thread. ``[]``
    when the CLI is logged out, offline, or prints something unrecognised.
    """
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            [cli, "models"], capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, env=_cli_child_env(), **kwargs,
        )
    except Exception:
        log.debug("cursor-agent models failed", exc_info=True)
        return []
    if result.returncode != 0:
        log.debug("cursor-agent models exited %s", result.returncode)
        return []
    return parse_cursor_models(result.stdout or "")


# Re-read on this cadence (the same as the backend lineups) or when the CLI
# binary / version changes; CLI detection calls in every 60 s.
CURSOR_MODELS_TTL_S = 15 * 60
_cursor_models_read = {"key": None, "at": 0.0}
_cursor_models_lock = threading.Lock()


def refresh_cursor_models(version) -> bool:
    """Re-read Cursor's model list if it is due; True when the lineup changed.

    Blocking (runs the CLI). A failed read keeps the lineup already shown.
    """
    cli = _which_cursor_cli()
    if not cli:
        return False
    key = (cli, version or "")
    now = time.monotonic()
    with _cursor_models_lock:
        last = _cursor_models_read
        if last["key"] == key and now - last["at"] < CURSOR_MODELS_TTL_S:
            return False
        last["key"], last["at"] = key, now
    return set_cli_lineup(BACKEND_CURSOR, probe_cursor_models(cli))


# (workspace, server url, token) combinations already approved this session.
_cursor_approved: set = set()
_cursor_approved_lock = threading.Lock()


def _approve_cursor_mcp(cli: str, cwd: str, env: dict, url: str) -> bool:
    """Approve zenvi-editor, and only it, for workspace *cwd*; True if it took.

    ``--approve-mcps`` would approve every server the workspace declares, and
    the workspace is the user's project folder: whatever a downloaded
    project's .cursor/mcp.json names would start unattended. Approvals are
    stored per workspace and keyed on the server's resolved config (URL, and
    the header after ${env:} expansion), so this runs with the token in *env*,
    again when the port moves, and otherwise once per session. The CLI exits 0
    even for a server it cannot find, so its answer is read as well.
    Blocking: it runs the CLI.
    """
    key = (cwd, url, env.get(_CURSOR_TOKEN_ENV, ""))
    with _cursor_approved_lock:
        if key in _cursor_approved:
            return True
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            [cli, "mcp", "enable", _CURSOR_MCP_NAME], capture_output=True,
            encoding="utf-8", errors="replace", timeout=60, cwd=cwd, env=env, **kwargs,
        )
    except Exception:
        log.warning("cursor-agent mcp enable failed", exc_info=True)
        return False
    answer = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "approved" not in answer.lower():
        log.warning("cursor-agent mcp enable did not approve zenvi-editor: %s",
                    answer.strip()[:200])
        return False
    with _cursor_approved_lock:
        _cursor_approved.add(key)
    return True


class BaseAgentRunner(QObject):
    """Common signal surface + subprocess plumbing for CLI agent backends."""

    # Identical to AIChatWorker so the existing AIChatWindow slots connect 1:1.
    response_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    token_received = pyqtSignal(str)
    tool_started = pyqtSignal(str, str, str)   # call_id, tool_name, args_json
    tool_log = pyqtSignal(str, str)            # call_id, line
    tool_completed = pyqtSignal(str, bool, str)  # call_id, ok, result_text
    # Declared for signature parity with AIChatWorker so AIChatWindow can
    # connect the same slots to every backend. CLI backends never emit it ΓÇö
    # planning mode is a Zenvi-backend feature, and these agents do their own
    # planning internally.
    plan_event = pyqtSignal(str, str)          # event_type, payload_json
    # CLI-only, and additive: lets AIChatWindow persist the conversation this
    # runner will resume from, so --resume survives an app restart.  Zenvi's
    # own worker never declares it (see _make_worker's hasattr guard).
    cli_session_changed = pyqtSignal(str, str, bool, str)  # ui_sid, cli_sid, started, cwd

    CLI_NAME = ""        # executable, e.g. "claude"
    DISPLAY_NAME = ""    # human label, e.g. "Claude Code"
    BACKEND_ID = ""      # picker/backend id, e.g. BACKEND_CLAUDE
    MODELS: list = []    # built-in model-picker entries; the live lineup wins

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session_id = ""          # set by AIChatWindow._make_worker
        self._backend_session_id = ""
        self._cli_session_id = ""      # CLI-side conversation id (for resume)
        self._cli_started = False
        # The CLI stores transcripts per working directory, so a resume is only
        # valid from the folder the conversation was created in.
        self._cli_cwd = ""
        # True once the CLI reported its own conversation id (Codex mints one;
        # Claude accepts the id we hand it up front).
        self._cli_id_from_cli = False
        self._stopping = False         # shutdown flag (mirrors AIChatWorker)
        # User pressed Stop. Distinct from _stopping, which means the whole app
        # (or this tab) is going away and must stay latched: a cancelled tab has
        # to accept the next message, so this one is cleared by run_request.
        self._cancelled = False
        self._proc = None
        self._cli_path = ""
        self._model_id = ""
        self._server = None
        self._responded = False
        self._final_text = ""
        self._last_error = ""
        self._stderr_tail: list = []

    def _emit_cli_session(self) -> None:
        """Tell the window which CLI conversation this tab is attached to."""
        try:
            self.cli_session_changed.emit(
                self._session_id or "",
                self._cli_session_id or "",
                bool(self._cli_started),
                self._cli_cwd or "",
            )
        except Exception:
            log.debug("cli_session_changed emit failed", exc_info=True)

    # -- slots -------------------------------------------------------------
    @pyqtSlot()
    def clear_session(self):
        """Forget CLI continuity so the next message starts a fresh conversation."""
        self._cli_started = False
        self._cli_session_id = ""
        self._cli_id_from_cli = False
        self._cli_cwd = ""

    def _reset_cli_continuity(self):
        """Start over from a fresh conversation on the next request.

        Unlike ``clear_session`` this is internal bookkeeping, not a user
        action: it runs when a launch-time failure means the CLI never created
        the conversation we latched.
        """
        self._cli_started = False
        self._cli_id_from_cli = False
        self._cli_session_id = str(uuid.uuid4())
        self._emit_cli_session()

    def cancel(self):
        """Terminate the running subprocess (called from the GUI thread).

        Non-blocking: the worker thread's read loop hits EOF and its final
        ``wait()`` reaps the process, so we don't stall the UI here.
        """
        self._cancelled = True
        proc = self._proc
        if proc and proc.poll() is None:
            # Signal the whole process group, not just the CLI: these agents
            # spawn their own children (shells, language servers, MCP clients),
            # and terminating the parent alone leaves those running ΓÇö they keep
            # driving the editor through the MCP server after the user pressed
            # Stop. run_request starts the child in its own session so this
            # group id is ours to kill.
            if sys.platform == "win32":
                try:
                    subprocess.call(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except Exception:
                    pass
                for send in (proc.terminate, proc.kill):
                    try:
                        send()
                        return
                    except Exception:
                        continue
                return
            for send in (
                lambda: os.killpg(os.getpgid(proc.pid), signal.SIGTERM),
                proc.terminate,
                proc.kill,
            ):
                try:
                    send()
                    return
                except Exception:
                    continue

    @property
    def _aborted(self) -> bool:
        """True when nothing more should be emitted for the current request."""
        return self._stopping or self._cancelled

    # Signature must match AIChatWorker.run_request exactly: AIChatWindow
    # dispatches through QMetaObject.invokeMethod with five Q_ARG(str, ...),
    # and Qt resolves the slot by its registered signature ΓÇö a shorter one is
    # simply never found and the request silently does nothing.
    @pyqtSlot(str, str, str, str, str)
    def run_request(self, text: str, model_id: str, agent_mode: str = "agent",
                    action: str = "chat", plan_id: str = ""):
        # ``agent_mode``/``action``/``plan_id`` drive the Zenvi backend's
        # planning flow only; CLI agents plan internally, so they are accepted
        # for signature parity and otherwise ignored.
        self._cancelled = False
        self._model_id = self._coerce_model(model_id)
        self._responded = False
        self._final_text = ""
        self._last_error = ""
        self._stderr_tail = []
        if not self._cli_session_id:
            self._cli_session_id = self._session_id or str(uuid.uuid4())

        # The CLI keeps transcripts per working directory, so a conversation
        # started elsewhere (Save As into another folder) is simply not
        # reachable from here -- begin a new one rather than issue a --resume
        # that is bound to fail.
        cwd = _project_cwd()
        if self._cli_started and self._cli_cwd and cwd != self._cli_cwd:
            log.info(
                "%s: project folder changed (%s -> %s), starting a new conversation",
                self.CLI_NAME, self._cli_cwd, cwd,
            )
            self._cli_started = False
            self._cli_id_from_cli = False
            self._cli_session_id = str(uuid.uuid4())
        self._cli_cwd = cwd

        try:
            from classes.agent_mcp_server import get_mcp_server
            self._server = get_mcp_server().start()
        except Exception as e:
            log.error("MCP server start failed: %s", e, exc_info=True)
            self._emit_error("Could not start the editor tool server: %s" % e)
            return

        self._cli_path = _which_cli(self.CLI_NAME)
        if not self._cli_path:
            self._emit_error(
                "%s CLI not found. Install it and make sure '%s' is on your PATH, "
                "then try again." % (self.DISPLAY_NAME, self.CLI_NAME)
            )
            return

        ready_err = self._ensure_ready()
        if ready_err:
            self._emit_error(ready_err)
            return

        try:
            argv = self._build_argv(text)
            popen_kwargs = dict(
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Explicit UTF-8, not text=True's locale-dependent default: a
                # GUI-launched app's environment often lacks LANG/LC_ALL, which
                # can silently resolve to ASCII and crash on the CLI's normal
                # non-ASCII output (em dashes, arrows, checkmarks, etc.).
                # errors="replace" so a genuinely malformed byte degrades to
                # U+FFFD instead of killing the whole read loop.
                encoding="utf-8", errors="replace",
                bufsize=1, env=self._build_env(), cwd=cwd,
            )
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
                no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                popen_kwargs["creationflags"] = flags | no_window
            else:
                # Own process group so cancel() can signal the CLI *and* every
                # child it spawned (see cancel()).
                popen_kwargs["start_new_session"] = True
            self._proc = subprocess.Popen(argv, **popen_kwargs)
        except Exception as e:
            if not self._aborted:
                self._emit_error("Failed to launch %s: %s" % (self.DISPLAY_NAME, e))
            return

        # The CLI has now consumed this conversation id, so every later turn
        # must resume rather than try to create it again.  Latching here (not
        # after a clean turn) is what makes Stop mid-turn recoverable.
        self._cli_started = True
        self._emit_cli_session()

        try:
            for line in self._proc.stdout:
                if self._aborted:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    # Non-JSON log noise (CLI diagnostics) ΓÇö keep a short tail
                    # so we can surface something useful if the run fails.
                    self._stderr_tail.append(line)
                    self._stderr_tail = self._stderr_tail[-8:]
                    continue
                try:
                    self._handle_event(ev)
                except Exception:
                    log.debug("agent event handling failed", exc_info=True)
        except Exception as e:
            if not self._aborted:
                self._emit_error(str(e))
            return

        # Reaped outside the read loop's handler: stdout is already closed, so
        # the whole response is in hand. A CLI that lingers past the timeout
        # must not turn into "Command [...] timed out after 5 seconds" in the
        # chat and throw that response away.
        try:
            self._proc.wait(timeout=5)
        except Exception:
            log.warning("%s did not exit within 5s of closing stdout",
                        self.DISPLAY_NAME)

        if self._aborted:
            return
        if self._responded:
            return
        if self._final_text:
            self._emit_response(self._final_text)
        elif self._last_error:
            self._emit_error(self._last_error)
        elif self._proc.returncode not in (0, None):
            # Nothing came back at all and the CLI failed: it never got as far
            # as creating this conversation (not logged in, bad flag, ...), so
            # drop the resume continuity we latched at launch. Leaving it set
            # makes every later message in this tab --resume an id the CLI does
            # not have, which fails until the user clears the session.
            self._reset_cli_continuity()
            tail = "\n".join(self._stderr_tail[-4:])
            self._emit_error(
                "%s exited with code %s.%s"
                % (self.DISPLAY_NAME, self._proc.returncode,
                   ("\n" + tail) if tail else "")
            )
        else:
            self._emit_response("(No response.)")

    # -- emit helpers (respect shutdown) -----------------------------------
    def _emit_response(self, text: str):
        self._responded = True
        if not self._aborted:
            self.response_ready.emit(text or "")

    def _emit_error(self, text: str):
        if not self._aborted:
            self.error_occurred.emit(text or "Unknown error.")

    # -- model selection ---------------------------------------------------
    def _coerce_model(self, model_id: str) -> str:
        """Keep *model_id* only if it is one this backend actually offers.

        Tabs remember the model the picker last had, and that picker is shared
        with the other backends ΓÇö so a tab switched from Zenvi to Claude Code
        can arrive holding a Zenvi model id, which the CLI would reject.
        """
        offered = models_for_backend(self.BACKEND_ID)
        if not model_id or not offered or model_id == CLI_DEFAULT_MODEL_ID:
            return ""
        return model_id if any(m["id"] == model_id for m in offered) else ""

    # -- subclass hooks ----------------------------------------------------
    def _ensure_ready(self):
        """Return an error string if the backend can't run, else None."""
        return None

    def _build_env(self):
        return _cli_child_env()

    def _build_argv(self, text: str):
        raise NotImplementedError

    def _handle_event(self, ev: dict):
        raise NotImplementedError


class ClaudeCodeRunner(BaseAgentRunner):
    """Drives the Claude Code CLI (`claude -p --output-format stream-json`)."""

    CLI_NAME = "claude"
    DISPLAY_NAME = "Claude Code"
    BACKEND_ID = BACKEND_CLAUDE

    # Built-in fallback lineup, used until the backend's live list lands (see
    # ``models_for_backend``). ``rank`` orders the picker, ``featured`` decides
    # whether an entry shows before the menu's "show all" toggle, the same
    # contract as the Zenvi model list the backend serves (see setModels in
    # chat.js).
    MODELS = [
        {"id": "claude-opus-5",   "name": "Opus 5",   "provider": "anthropic",
         "rank": 10, "featured": True, "default": True,
         "tags": ["Most capable"]},
        {"id": "claude-sonnet-5", "name": "Sonnet 5", "provider": "anthropic",
         "rank": 20, "featured": True, "tags": ["Balanced"]},
        {"id": "claude-haiku-4-5", "name": "Haiku 4.5", "provider": "anthropic",
         "rank": 30, "featured": True, "tags": ["Fastest"]},
        {"id": "claude-fable-5",  "name": "Fable 5",  "provider": "anthropic",
         "rank": 40, "featured": False, "tags": ["Frontier"]},
        {"id": "claude-opus-4-8", "name": "Opus 4.8", "provider": "anthropic",
         "rank": 50, "featured": False},
        {"id": "claude-opus-4-7", "name": "Opus 4.7", "provider": "anthropic",
         "rank": 60, "featured": False},
        {"id": "claude-opus-4-6", "name": "Opus 4.6", "provider": "anthropic",
         "rank": 70, "featured": False},
        {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6", "provider": "anthropic",
         "rank": 80, "featured": False},
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._open_blocks: dict = {}   # stream_event block index -> {"kind","call_id"}
        self._think_seq = 0

    def _build_argv(self, text: str):
        cfg = _write_claude_mcp_config(self._server)
        argv = [
            self._cli_path or self.CLI_NAME, "-p", text,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", cfg, "--strict-mcp-config",
            # The agent is driving the editor on the user's behalf from inside
            # the app ΓÇö there is no terminal to answer a permission prompt, so
            # a prompt would just hang the turn until it times out.
            "--dangerously-skip-permissions",
            "--append-system-prompt", _agent_bash_prompt(),
        ]
        argv += _add_dir_args()
        if self._model_id:
            argv += ["--model", self._model_id]
        if self._cli_started and self._cli_session_id:
            argv += ["--resume", self._cli_session_id]
        else:
            argv += ["--session-id", self._cli_session_id]
        return argv

    def _handle_event(self, ev: dict):
        etype = ev.get("type")
        if etype == "stream_event":
            self._handle_stream_event(ev.get("event") or {})
            return
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name") or "tool"
                    self.tool_started.emit(
                        block.get("id") or "", _strip_mcp_prefix(name),
                        json.dumps(block.get("input") or {}, default=str))
            return
        if etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    text = _content_to_text(block.get("content"))
                    ok = not block.get("is_error")
                    self.tool_completed.emit(block.get("tool_use_id") or "", ok, text)
            return
        if etype == "result":
            if ev.get("is_error"):
                self._last_error = ev.get("result") or "The agent reported an error."
            else:
                self._emit_response(ev.get("result") or self._final_text)
            return

    def _handle_stream_event(self, event: dict):
        etype = event.get("type")
        if etype == "content_block_start":
            idx = event.get("index")
            cb = event.get("content_block") or {}
            if cb.get("type") == "thinking":
                self._think_seq += 1
                call_id = "think_%d" % self._think_seq
                self._open_blocks[idx] = {"kind": "thinking", "call_id": call_id}
                self.tool_started.emit(call_id, "thinking", "{}")
            else:
                self._open_blocks[idx] = {"kind": cb.get("type")}
            return
        if etype == "content_block_delta":
            idx = event.get("index")
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                txt = delta.get("text") or ""
                self._final_text += txt
                self.token_received.emit(txt)
            elif dtype == "thinking_delta":
                blk = self._open_blocks.get(idx)
                if blk and blk.get("kind") == "thinking":
                    self.tool_log.emit(blk["call_id"], delta.get("thinking") or "")
            return
        if etype == "content_block_stop":
            blk = self._open_blocks.pop(event.get("index"), None)
            if blk and blk.get("kind") == "thinking":
                self.tool_completed.emit(blk["call_id"], True, "")
            return
        if etype == "message_start":
            # A fresh assistant turn streams into the same bubble; drop stale
            # block bookkeeping but keep accumulated final text.
            self._open_blocks.clear()


class CodexRunner(BaseAgentRunner):
    """Drives the Codex CLI (`codex exec --json`)."""

    CLI_NAME = "codex"
    DISPLAY_NAME = "Codex"
    BACKEND_ID = BACKEND_CODEX
    # No built-in lineup: we do not track OpenAI's models here. The backend's
    # live list (``set_live_lineups``) fills the picker; until it lands the
    # picker stays hidden and the CLI uses whatever its own config selects.
    MODELS: list = []

    _TOOL_ITEM_TYPES = {
        "command_execution", "mcp_tool_call", "tool_call", "function_call",
        "local_shell_call", "web_search",
    }
    _MSG_ITEM_TYPES = {"assistant_message", "agent_message", "message"}

    def _build_env(self):
        extra = {}
        if self._server is not None and self._server.token:
            extra["ZENVI_MCP_TOKEN"] = self._server.token
        return _cli_child_env(extra)

    def _build_argv(self, text: str):
        url = self._server.url() if self._server else ""
        common = [
            "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "-c", 'mcp_servers.zenvi_editor.url="%s"' % url,
            "-c", 'mcp_servers.zenvi_editor.bearer_token_env_var="ZENVI_MCP_TOKEN"',
        ]
        if self._model_id:
            common += ["--model", self._model_id]
        common += _add_dir_args()
        # Unlike Claude, Codex will not take an id we invent -- it mints its own
        # and reports it as ``thread.started``.  Resuming a seeded placeholder
        # would just fail, so wait until we have heard a real one.
        cli = self._cli_path or self.CLI_NAME
        if self._cli_started and self._cli_id_from_cli and self._cli_session_id:
            return [cli, "exec", "resume", self._cli_session_id, *common, text]
        return [cli, "exec", *common, text]

    def _handle_event(self, ev: dict):
        etype = ev.get("type")
        if etype == "thread.started":
            thread_id = ev.get("thread_id") or ""
            if thread_id:
                self._cli_session_id = thread_id
                self._cli_id_from_cli = True
                # Persist it now: this is the only place the id is knowable.
                self._emit_cli_session()
            return
        if etype in ("item.started", "item.updated", "item.completed"):
            self._handle_item(etype, ev.get("item") or {})
            return
        if etype == "turn.completed":
            self._emit_response(self._final_text)
            return
        if etype == "turn.failed":
            self._last_error = (ev.get("error") or {}).get("message") or "The agent reported an error."
            return
        if etype == "error":
            # Many of these are transient "Reconnecting..." notices; remember the
            # latest as a fallback only.
            self._last_error = ev.get("message") or self._last_error

    def _handle_item(self, etype: str, item: dict):
        itype = item.get("type")
        call_id = item.get("id") or ""
        if itype in self._TOOL_ITEM_TYPES:
            if etype == "item.started":
                name = item.get("tool") or item.get("name") or item.get("command") or itype
                args = item.get("arguments") or item.get("input") or {}
                self.tool_started.emit(call_id, _strip_mcp_prefix(str(name)),
                                       json.dumps(args, default=str) if isinstance(args, dict) else "{}")
            elif etype == "item.completed":
                out = item.get("output") or item.get("result") or item.get("text") or ""
                ok = (item.get("status") or "completed") not in ("failed", "error")
                self.tool_completed.emit(call_id, ok, str(out))
            return
        if itype in self._MSG_ITEM_TYPES:
            if etype == "item.completed":
                txt = item.get("text") or item.get("message") or ""
                if txt:
                    # Appended, not replaced (matching the Claude runner): a
                    # turn can complete more than one assistant message, and
                    # all of them stream into the same bubble -- so the text
                    # turn.completed persists has to be all of them too.
                    if self._final_text:
                        self._final_text += "\n\n"
                    self._final_text += txt
                    self.token_received.emit(txt)
            return
        if itype == "reasoning" and etype == "item.completed":
            txt = item.get("text") or ""
            if txt:
                rid = "think_%s" % (call_id or "0")
                self.tool_started.emit(rid, "thinking", "{}")
                self.tool_log.emit(rid, txt)
                self.tool_completed.emit(rid, True, "")


class CursorCliRunner(BaseAgentRunner):
    """Drives the Cursor agent CLI (`cursor-agent -p --output-format stream-json`)."""

    CLI_NAME = "cursor-agent"
    DISPLAY_NAME = "Cursor CLI"
    BACKEND_ID = BACKEND_CURSOR
    # The models depend on the Cursor account, so the picker shows what
    # `cursor-agent models` lists (refresh_cursor_models). Until it has, the
    # only choice is to leave the model to the CLI's own config.
    MODELS = [_cli_default_entry()]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._think_seq = 0
        self._mcp_approved = False
        self._begin_turn()

    def _begin_turn(self):
        """Drop the previous turn's stream state (each run opens with ``init``)."""
        self._think_id = ""
        self._think_open = False
        # Prose between two tool starts: the unit the chat freezes into its own
        # bubble when a tool begins (AIChatWindow._on_tool_started).
        self._segments = []
        self._segment = ""
        # Text of the message streaming now, which the CLI then repeats whole.
        self._message = ""

    def _ensure_ready(self):
        if self._server is None:
            return "Could not start the editor tool server."
        ok, message = register_cursor(self._server.port, self._server.token)
        if not ok:
            return message
        self._mcp_approved = _approve_cursor_mcp(
            self._cli_path or self.CLI_NAME, self._cli_cwd or _project_cwd(),
            self._build_env(), self._server.url(),
        )
        return None

    def _build_env(self):
        # register_cursor's entry sends "Bearer ${env:ZENVI_MCP_TOKEN}".
        extra = {}
        if self._server is not None and self._server.token:
            extra[_CURSOR_TOKEN_ENV] = self._server.token
        return _cli_child_env(extra)

    def _build_argv(self, text: str):
        argv = [
            self._cli_path or self.CLI_NAME, "-p",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force", "--trust",
            "--workspace", self._cli_cwd or _project_cwd(),
        ]
        if not self._mcp_approved:
            # Approving zenvi-editor alone failed (an older CLI?). Without
            # this the turn has no editor tools at all.
            argv.append("--approve-mcps")
        if self._model_id:
            argv += ["--model", self._model_id]
        argv += _add_dir_args()
        # Cursor mints the conversation id and reports it in ``init``, so only
        # an id heard from the CLI is resumed, never the placeholder we seed.
        if self._cli_started and self._cli_id_from_cli and self._cli_session_id:
            argv += ["--resume", self._cli_session_id]
        argv.append(text)
        return argv

    def _handle_event(self, ev: dict):
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            self._begin_turn()
            session_id = ev.get("session_id") or ""
            if session_id:
                self._cli_session_id = session_id
                self._cli_id_from_cli = True
                self._emit_cli_session()
            return
        if etype == "assistant":
            self._handle_assistant(ev)
            return
        if etype == "thinking":
            self._handle_thinking(ev)
            return
        if etype == "tool_call":
            self._handle_tool_call(ev)
            return
        if etype == "result":
            self._close_thinking()
            if ev.get("is_error"):
                self._last_error = ev.get("result") or "The agent reported an error."
            else:
                # Not ``result`` itself: it glues the turn's messages together
                # with no separator, so the chat could not tell which of them it
                # has already shown (AIChatWindow._final_segment_text).
                self._emit_response(self._final_text or ev.get("result") or "")
            return
        if etype == "error":
            self._last_error = ev.get("message") or self._last_error

    def _handle_assistant(self, ev: dict):
        text = "".join(
            block.get("text") or ""
            for block in (ev.get("message") or {}).get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            return
        # --stream-partial-output streams a message as deltas and then sends it
        # again whole. Deltas carry a timestamp and no model_call_id; the repeat
        # has a model_call_id or no timestamp. Only a repeat of exactly what
        # streamed is dropped, so nothing is lost if the CLI stops repeating.
        partial = "timestamp_ms" in ev and "model_call_id" not in ev
        if not partial and self._message and text.strip() == self._message.strip():
            self._message = ""
            return
        self._message += text
        self._segment += text
        self._final_text = "\n\n".join(self._segments + [self._segment])
        self.token_received.emit(text)

    def _start_block(self, call_id: str, name: str, args: dict):
        """Emit ``tool_started``, closing the prose segment the way the chat does."""
        if self._segment.strip():
            self._segments.append(self._segment)
        self._segment = ""
        self.tool_started.emit(call_id, name, json.dumps(args, default=str))

    def _close_thinking(self):
        if self._think_open:
            self.tool_completed.emit(self._think_id, True, "")
            self._think_open = False

    def _handle_thinking(self, ev: dict):
        subtype = ev.get("subtype")
        if subtype == "delta":
            if not self._think_open:
                self._think_seq += 1
                self._think_id = "think_%d" % self._think_seq
                self._think_open = True
                self._start_block(self._think_id, "thinking", {})
            self.tool_log.emit(self._think_id, ev.get("text") or "")
            return
        if subtype == "completed":
            self._close_thinking()

    def _handle_tool_call(self, ev: dict):
        subtype = ev.get("subtype")
        call_id = ev.get("call_id") or ""
        kind, payload = _cursor_tool_payload(ev.get("tool_call"))
        if subtype == "started":
            self._close_thinking()
            # A tool call ends the message that was streaming.
            self._message = ""
            name, args = _cursor_tool_start(kind, payload)
            self._start_block(call_id, name, args)
            return
        if subtype == "completed":
            ok, text = _cursor_tool_result(payload.get("result"))
            self.tool_completed.emit(call_id, ok, text)


# Cursor's own tools, keyed by their ``<kind>ToolCall`` field. Spelled out so
# the chat reads them as work on the user's files: bare "read", "edit", "glob"
# and "grep" are the Zenvi Assistant harness's motion-graphics tools as far as
# humanize_tool_name is concerned.
_CURSOR_TOOL_NAMES = {
    "shell": "run_shell_command",
    "read": "read_file",
    "edit": "edit_file",
    "write": "write_file",
    "delete": "delete_file",
    "grep": "search_files",
    "glob": "find_files",
    "ls": "list_directory",
    "getMcpTools": "look_up_tools",
}

# Arguments that say what a Cursor tool is working on. The rest are CLI
# bookkeeping (shell parse trees, call ids, sandbox flags).
_CURSOR_ARG_KEYS = (
    "command", "path", "targetDirectory", "globPattern", "pattern", "query",
    "searchTerm", "url", "server", "toolName",
)


def _cursor_tool_payload(tool_call):
    """``(kind, payload)`` of a ``tool_call`` field, e.g. ``("mcp", {...})``."""
    if not isinstance(tool_call, dict):
        return "", {}
    for key, value in tool_call.items():
        if isinstance(key, str) and key.endswith("ToolCall") and isinstance(value, dict):
            return key[: -len("ToolCall")], value
    tool = tool_call.get("tool")   # {"tool": {"case": "...ToolCall", "value": {...}}}
    if isinstance(tool, dict) and isinstance(tool.get("value"), dict):
        case = str(tool.get("case") or "")
        return (case[: -len("ToolCall")] if case.endswith("ToolCall") else case), tool["value"]
    return "", tool_call


def _cursor_tool_start(kind: str, payload: dict):
    """The tool name the chat labels, and the arguments worth showing."""
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    if kind == "mcp":
        # args.name is "<server>-<tool>"; toolName is the tool alone.
        name = args.get("toolName") or ""
        if not name:
            name = str(args.get("name") or "")
            server = args.get("providerIdentifier") or args.get("serverIdentifier") or ""
            if server and name.startswith(server + "-"):
                name = name[len(server) + 1:]
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        return _strip_mcp_prefix(str(name)) or "mcp_tool", inner
    if kind:
        name = _CURSOR_TOOL_NAMES.get(kind) or re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()
        return name, {k: args[k] for k in _CURSOR_ARG_KEYS if args.get(k) not in (None, "")}
    name = payload.get("name") or "tool"
    raw = payload.get("arguments") or payload.get("args") or {}
    return _strip_mcp_prefix(str(name)), raw if isinstance(raw, dict) else {}


def _cursor_tool_result(result):
    """``(ok, text)`` for a finished Cursor tool call.

    The CLI wraps a result in one key: ``success`` (an MCP tool that raised
    is still a ``success``, with ``isError``), or ``error`` / ``spawnError`` /
    ``rejected`` and the like when the call itself failed. Flags such as
    ``isBackground`` can sit next to it.
    """
    if result is None:
        return True, ""
    if not isinstance(result, dict):
        return True, str(result)
    if "success" in result:
        body = result.get("success")
        if not isinstance(body, dict):
            return True, "" if body is None else str(body)
        ok = body.get("isError") is not True and body.get("exitCode") in (None, 0)
        if "content" in body:
            return ok, _cursor_content_text(body.get("content"))
        for key in ("interleavedOutput", "stdout", "output", "text"):
            if body.get(key):
                return ok, str(body[key])
        return ok, json.dumps(body, default=str)
    for kind, body in result.items():
        if isinstance(body, dict):
            message = body.get("error") or body.get("message") or body.get("reason") or ""
            return False, str(message or kind)
        if isinstance(body, str) and body:
            return False, body
    return True, ""


def _cursor_content_text(content) -> str:
    """A result's ``content``: a string, or MCP items with the text one level
    deeper than usual (``{"text": {"text": "..."}}``)."""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, dict):
                text = text.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)
    return _content_to_text(content)


# ---------------------------------------------------------------------------
# MCP config helpers
# ---------------------------------------------------------------------------

def _write_claude_mcp_config(server) -> str:
    """Write the Claude `--mcp-config` JSON pointing at the in-app MCP server."""
    cfg = {
        "mcpServers": {
            "zenvi-editor": {
                "type": "http",
                "url": server.url(),
                "headers": {"Authorization": "Bearer %s" % server.token},
            }
        }
    }
    path = os.path.abspath(os.path.join(_agent_mcp_dir(), "claude_mcp.json"))
    # Created 0600 in one step rather than chmod'ed afterwards: a plain open()
    # applies the umask first (usually 0644), leaving the bearer token this
    # file carries world-readable for the window in between.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh)
    return path


def _content_to_text(content) -> str:
    """Flatten an MCP/Claude tool_result `content` field into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
            elif isinstance(blk, dict) and blk.get("text"):
                parts.append(blk.get("text"))
        return "\n".join(p for p in parts if p)
    return str(content)


def cleanup_agent_mcp_configs():
    """Remove written MCP config files (called on shutdown)."""
    try:
        import glob
        for p in glob.glob(os.path.join(_agent_mcp_dir(), "*.json")):
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
