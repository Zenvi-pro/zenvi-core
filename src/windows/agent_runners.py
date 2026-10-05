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
BACKEND_OPENCODE = "opencode"
BACKEND_HERMES = "hermes"


# Models offered in the chat model picker per backend, in menu order. ``id`` is
# passed straight through to the CLI's ``--model`` flag; Claude Code accepts a
# full model name or a latest-alias ("opus", "sonnet"), and we use full names so
# the picker keeps meaning the same model after a new release ships.
#
# Where the lineup comes from, first hit wins: what the installed CLI lists
# about itself (``set_cli_lineup``, every runner has a ``list_models``), then
# the Zenvi backend's ``GET /models/cli`` (``set_live_lineups``), then each
# runner's built-in ``MODELS``. The last two only show while the CLI has not
# answered (not signed in, still starting).
#
# A backend with an empty list hides the model pill. Every runner that can
# list its models carries a "CLI default" entry until the CLI has answered.
_live_lineups: dict = {}
_live_lineups_lock = threading.Lock()

# ``efforts`` lists the reasoning-effort levels a model takes, as its CLI
# names them; the chat shows an effort picker beside the model pill for it.
_PICKER_KEYS = ("id", "name", "provider", "featured", "rank", "tags", "default", "efforts")


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


def _cli_default_entry(uses: str = "", efforts=None) -> dict:
    """"CLI default", tagged with the model the CLI says it would use."""
    entry = {"id": CLI_DEFAULT_MODEL_ID, "name": "CLI default", "rank": 0,
             "featured": True, "default": True}
    if uses:
        entry["tags"] = [uses]
    if efforts:
        entry["efforts"] = list(efforts)
    return entry


def _effort_levels(values) -> list:
    """Effort names out of a CLI's listing: non-empty strings, no repeats."""
    out = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


# Lineups a CLI reported about itself (``cursor-agent models``,
# ``opencode models``, ``codex debug models``, Claude Code's ``initialize``,
# Hermes' ACP session): the user's own install knows which ids it accepts, so
# this beats the backend's lineup.
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
    with _live_lineups_lock:
        listed = [dict(m) for m in _cli_lineups.get(backend, [])]
    if listed:
        return listed
    live = live_lineup_for(backend)
    if live:
        return live
    runner = CLI_RUNNERS.get(backend)
    return [dict(m) for m in runner.MODELS] if runner else []


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
        # OpenCode's official install script.
        dirs.append(os.path.join(home, ".opencode", "bin"))
    appdata = os.environ.get("APPDATA") or (
        os.path.join(home, "AppData", "Roaming") if home else ""
    )
    if appdata:
        dirs.append(os.path.join(appdata, "npm"))
    # nvm-windows keeps npm globals in its node folder, not %APPDATA%/npm.
    if os.environ.get("NVM_SYMLINK"):
        dirs.append(os.environ["NVM_SYMLINK"])
    local = os.environ.get("LOCALAPPDATA") or (
        os.path.join(home, "AppData", "Local") if home else ""
    )
    if local:
        dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    return dirs


def _expand_path_value(value: str, variables: dict) -> list:
    """Folders of a registry ``Path`` value, with its ``%VAR%`` parts filled in."""
    def fill(match):
        name = match.group(1)
        found = variables.get(name.upper()) or os.environ.get(name)
        return found if found else match.group(0)

    out = []
    for part in (value or "").split(";"):
        part = re.sub(r"%([^%;]+)%", fill, part.strip())
        if part and "%" not in part:
            out.append(part)
    return out


@functools.lru_cache(maxsize=1)
def _windows_path_dirs() -> tuple:
    """The user's and machine's PATH as Windows stores them.

    run-zenvi-core.sh starts the editor with ``env -i`` and an MSYS shell does
    not inherit the Windows PATH, so node / npm / nvm folders (where OpenCode
    and Codex's npm builds live) are missing from ``os.environ`` although
    every other program on the machine sees them.
    """
    if sys.platform != "win32":
        return ()
    try:
        import winreg
    except Exception:
        return ()
    keys = ((winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"))
    variables, paths = {}, []
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                index = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if not isinstance(value, str):
                        continue
                    if name.upper() == "PATH":
                        paths.append(value)
                    else:
                        variables.setdefault(name.upper(), value)
        except OSError:
            continue
    out = []
    for value in paths:
        for folder in _expand_path_value(value, variables):
            if folder not in out:
                out.append(folder)
    return tuple(out)


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


def _agent_import_prompt() -> str:
    """Shared import steering for Claude (append) and Codex (message prefix)."""
    return (
        "Importing local media (import_files_tool):\n"
        "- To put files into Project Files you MUST call import_files_tool. "
        "list_files_tool only lists media already in the project — it never "
        "imports from disk.\n"
        "- If the user named a folder (Downloads, Desktop, a path, or "
        "\"folder X in Downloads\"), call import_files_tool with that folder "
        "and dry_run=true IMMEDIATELY. Do NOT ask for individual file paths "
        "when they named a folder. Do NOT use Glob, Read, Bash, or "
        "list_files_tool to preflight — Zenvi resolves the path.\n"
        "- Bare names work: folder=\"Downloads\" or folder=\"Desktop\". "
        "For “all videos”, pass media_types=video (skips images/audio).\n"
        "- Windows: prefer C:/Users/.../folder (forward slashes). Git Bash "
        "/c/Users/... also works. Never invent /mnt/c/... mounts.\n"
        "- For vague asks inside Desktop/Downloads/Movies/Videos/Documents/"
        "Pictures only, you may Glob those folders, then call "
        "import_files_tool with the path you found (dry_run=true first).\n"
        "- For folders or bulk asks: dry_run=true → show preview → ask the "
        "user → dry_run=false. If the tool returns multiple candidates or "
        "not found, ask the user — do not guess."
    )


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
        'for name in "${(@k)files}"; do ...; done.\n'
        + _agent_import_prompt()
    )


def _claude_code_system_prompt():
    """Append-system-prompt for Claude Code: editor fast-path + bash recipes."""
    return (
        "Zenvi editor (zenvi-editor MCP):\n"
        "- Drive the open project with zenvi-editor MCP tools for editor actions.\n"
        "- YouTube / youtu.be / similar URL: ONE call to ingest_web_video_tool "
        "with url + intent. Add/place on timeline → intent=timeline. "
        "Colour/look/grade → intent=reference (Imports only). "
        "Recreate → intent=recreate.\n"
        "- Do NOT ToolSearch, list_files, get_timeline_state_tool, "
        "import_files_tool, or import_video_url_and_add_to_timeline_tool "
        "before ingest for a bare YouTube/web video URL.\n"
        "- If status=running, call ingest_web_video_tool(job_id=...) again — "
        "each call waits until done or ~45s. A client timeout is not failure; "
        "keep calling the same job_id. Do not abandon it for Import.\n"
        "- On ok=false / status=failed: quote the short reason once and stop. "
        "Do not start a second download of the same URL in that turn.\n"
        "- After a successful ingest+place: one short sentence. "
        "Do NOT call watch_clip_window_tool unless the user asked to check "
        "the picture.\n"
        "\n"
        + _agent_bash_prompt()
    )


# Stable sentinel returned by ClaudeCodeRunner._ensure_ready when signed out.
CLI_AUTH_REQUIRED = "CLI_AUTH_REQUIRED"


def is_cli_auth_error(text: str) -> bool:
    """True when CLI output means Anthropic/Claude login is missing or expired."""
    low = str(text or "").strip().lower()
    if not low:
        return False
    if low.strip() == CLI_AUTH_REQUIRED.lower():
        return True
    needles = (
        "oauth session expired",
        "failed to authenticate",
        "not logged in",
        "please run /login",
        "please run claude auth login",
        "authentication_error",
        "could not be refreshed",
        "auth login",
    )
    return any(n in low for n in needles)


def claude_auth_status() -> dict | None:
    """Run ``claude auth status``; return parsed JSON or None on failure."""
    cli = _which_cli("claude")
    if not cli:
        return None
    try:
        result = subprocess.run(
            [cli, "auth", "status"],
            capture_output=True, text=True, timeout=8,
            env=_cli_child_env(),
        )
    except Exception:
        return None
    raw = (result.stdout or result.stderr or "").strip()
    if not raw:
        return None
    # Prefer a JSON object line; some versions wrap prose around it.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def claude_is_logged_in() -> bool | None:
    """True/False from auth status; None if the probe could not run."""
    status = claude_auth_status()
    if not isinstance(status, dict):
        return None
    if "loggedIn" in status:
        return bool(status.get("loggedIn"))
    # Older shapes
    if "logged_in" in status:
        return bool(status.get("logged_in"))
    return None


def start_claude_auth_login() -> tuple[bool, str]:
    """Launch ``claude auth login`` (opens the browser). Returns (ok, message)."""
    cli = _which_cli("claude")
    if not cli:
        return False, "Claude Code CLI not found on PATH."
    try:
        proc = subprocess.Popen(
            [cli, "auth", "login"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=_cli_child_env(),
            start_new_session=True,
        )
    except Exception as e:
        return False, "Could not start Claude sign-in: %s" % e
    # Poll until logged in or the login process exits / times out (~5 min).
    deadline = time.time() + 300
    while time.time() < deadline:
        logged = claude_is_logged_in()
        if logged is True:
            try:
                proc.terminate()
            except Exception:
                pass
            return True, "Signed in to Claude."
        if proc.poll() is not None:
            if claude_is_logged_in() is True:
                return True, "Signed in to Claude."
            err = ""
            try:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            except Exception:
                pass
            return False, err or (
                "Sign-in did not finish. Complete it in the browser, then try again."
            )
        time.sleep(1.5)
    try:
        proc.terminate()
    except Exception:
        pass
    if claude_is_logged_in() is True:
        return True, "Signed in to Claude."
    return False, "Sign-in timed out. Finish in the browser, then try again."


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
    # What the CLI itself starts (node behind an npm shim, git, ffmpeg) is
    # looked up on PATH, which a stripped launch lost (see _windows_path_dirs).
    have = [d for d in (env.get("PATH") or "").split(os.pathsep) if d]
    missing = [d for d in _windows_path_dirs() if d not in have]
    if missing:
        env["PATH"] = os.pathsep.join(have + missing)
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
        # Launchers first: npm puts an extension-less sh script beside
        # ``<name>.cmd`` and Windows cannot run it.
        names = [binary_name + ".exe", binary_name + ".cmd", binary_name + ".bat", binary_name]
        for name in names[:3]:
            found = shutil.which(name)
            if found:
                return found
    for directory in list(_cli_install_dirs()) + list(_windows_path_dirs()):
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

    For ``claude``, also reports ``logged_in`` (True/False/None).
    """
    cli = _which_cli(binary_name)
    if not cli:
        out = {"installed": False, "version": None, "registered": False}
        if binary_name == "claude":
            out["logged_in"] = None
        return out
    version = None
    try:
        result = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=10
        )
        version = (result.stdout or result.stderr or "").strip() or None
    except Exception:
        version = None
    out = {
        "installed": True,
        "version": version,
        "registered": _is_registered(binary_name),
    }
    if binary_name == "claude":
        out["logged_in"] = claude_is_logged_in()
    return out


def _is_registered(binary_name: str) -> bool:
    if binary_name == "claude":
        return _claude_is_registered()
    if binary_name == "codex":
        return _codex_is_registered()
    if binary_name == "cursor-agent":
        return _cursor_is_registered()
    if binary_name == "opencode":
        return _opencode_is_registered()
    if binary_name == "hermes":
        return _hermes_is_registered()
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
    return _claude_entry() is not None or _claude_is_registered_via_cli()


def _claude_entry() -> dict | None:
    """Return the ``zenvi`` mcpServers entry from ``~/.claude.json``, or None."""
    try:
        with open(_claude_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = (data.get("mcpServers") or {}).get("zenvi")
        return entry if isinstance(entry, dict) else None
    except Exception:
        return None


def _claude_registration_matches(port: int, token: str) -> bool:
    """True when Claude already points at this live MCP URL + bearer token."""
    entry = _claude_entry()
    if not entry:
        return False
    want_url = "http://127.0.0.1:%d/mcp" % port
    url = str(entry.get("url") or entry.get("serverUrl") or "").rstrip("/")
    if url != want_url.rstrip("/"):
        return False
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        return False
    auth = str(headers.get("Authorization") or headers.get("authorization") or "")
    return auth.strip() == ("Bearer %s" % token)


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


def ensure_claude_registered(port: int, token: str):
    """Auto-wire Claude Code to the live Zenvi MCP when the CLI is installed.

    Skips ``mcp remove``/``add`` when the existing user-scope entry already
    matches this port + token (Palmier-style: app up ⇒ Claude can call tools).

    Returns ``(ok, message, changed)``.
    """
    if not _which_cli("claude"):
        return False, "Claude Code CLI not found on PATH.", False
    if _claude_registration_matches(port, token):
        return True, "Claude Code already connected to Zenvi MCP.", False
    ok, message = register_claude(port, token)
    return ok, message, bool(ok)


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


def _opencode_config_dir() -> str:
    """OpenCode's global config folder (it honours ``XDG_CONFIG_HOME`` on every OS)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_resolved_home(), ".config")
    return os.path.join(base, "opencode")


def _opencode_is_registered() -> bool:
    """OpenCode merges ``opencode.json`` and ``opencode.jsonc``; the latter may
    carry comments, so look for the server key rather than parse JSON."""
    for name in ("opencode.json", "opencode.jsonc"):
        try:
            with open(os.path.join(_opencode_config_dir(), name), "r", encoding="utf-8") as fh:
                if re.search(r'"zenvi_editor"\s*:', fh.read()):
                    return True
        except Exception:
            continue
    return False


def _opencode_server_entry(url: str) -> dict:
    # ``{env:...}`` is OpenCode's config substitution: the token stays out of
    # the file and is read from the environment at launch (as Codex does).
    return {
        "type": "remote",
        "url": url,
        "headers": {"Authorization": "Bearer {env:ZENVI_MCP_TOKEN}"},
        "enabled": True,
    }


def register_opencode(port: int, token: str):
    """Upsert ``mcp.zenvi_editor`` in OpenCode's global ``opencode.json`` (Connect).

    Zenvi's own turns do not need this: they pass a scoped ``OPENCODE_CONFIG``
    (see OpenCodeRunner). This is for running ``opencode`` yourself. OpenCode
    has no command that adds a remote MCP server non-interactively, and it
    merges ``opencode.json`` with the user's ``opencode.jsonc``, so only the
    plain-JSON file is edited and every other key and server is left as it
    was. Like register_cursor: a file that does not parse is refused, a
    current entry is not rewritten, and the file (which usually holds
    provider API keys) keeps its permissions, is replaced atomically, and
    gets a one-time ``.zenvi-backup``.

    Returns ``(ok, message)``.
    """
    path = os.path.realpath(os.path.join(_opencode_config_dir(), "opencode.json"))
    done = (
        "Updated %s. Before running opencode, run:\n"
        "export ZENVI_MCP_TOKEN=%s"
    ) % (path, token)
    original = ""
    data = {}
    mode = 0o600
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read()
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except Exception as e:
            return False, "Failed to read %s: %s" % (path, e)
        try:
            data = json.loads(original) if original.strip() else {}
        except Exception as e:
            return False, "%s is not valid JSON, not touching it: %s" % (path, e)
        if not isinstance(data, dict):
            return False, "%s is not a JSON object, not touching it." % path

    mcp = data.get("mcp")
    if mcp is None:
        mcp = {}
        data["mcp"] = mcp
    if not isinstance(mcp, dict):
        return False, "%s: \"mcp\" is not an object, not touching it." % path
    entry = _opencode_server_entry("http://127.0.0.1:%d/mcp" % port)
    if mcp.get("zenvi_editor") == entry:
        return True, done
    mcp["zenvi_editor"] = entry

    staged = path + ".zenvi-tmp"
    backup = path + ".zenvi-backup"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if original and not os.path.exists(backup):
            _write_with_mode(backup, original, mode)
        _write_with_mode(staged, json.dumps(data, indent=2) + "\n", mode)
        os.replace(staged, path)
    except Exception as e:
        try:
            os.remove(staged)
        except OSError:
            pass
        return False, "Failed to write %s: %s" % (path, e)
    return True, done


def _hermes_config_path() -> str:
    """Hermes' config.yaml: ``$HERMES_HOME``, else ``%LOCALAPPDATA%\\hermes`` on
    Windows and ``~/.hermes`` elsewhere (hermes_constants.get_hermes_home)."""
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if not home and sys.platform == "win32":
        local = (os.environ.get("LOCALAPPDATA") or "").strip() or os.path.join(
            _resolved_home(), "AppData", "Local")
        home = os.path.join(local, "hermes")
    if not home:
        home = os.path.join(_resolved_home(), ".hermes")
    return os.path.join(home, "config.yaml")


def _hermes_is_registered() -> bool:
    """Look for ``zenvi_editor`` under the top-level ``mcp_servers`` block of
    Hermes' ``config.yaml`` (no YAML parser here, and none is needed)."""
    try:
        with open(_hermes_config_path(), "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return False
    block = re.search(r"(?m)^mcp_servers:[^\n]*\n((?:[ \t]+[^\n]*\n|[ \t]*\n)*)", text + "\n")
    return bool(block and re.search(r"(?m)^[ \t]+zenvi_editor:", block.group(1)))


def register_hermes(port: int, token: str):
    """Upsert ``mcp_servers.zenvi_editor`` in Hermes' ``config.yaml`` (Connect).

    Zenvi's own turns do not need this: HermesRunner hands the server to each
    ACP session. This is for running ``hermes`` yourself. ``hermes mcp add`` is
    interactive, but ``hermes config set`` edits a dotted key in place and
    keeps every other server, so Hermes does the YAML editing itself; running
    it again after a port change just overwrites the url. The header uses
    Hermes' ``${VAR}`` expansion, so the token never lands in the file (as
    with Codex).

    Returns ``(ok, message)``.
    """
    hermes = _which_cli("hermes") or "hermes"
    settings = (
        ("mcp_servers.zenvi_editor.url", "http://127.0.0.1:%d/mcp" % port),
        ("mcp_servers.zenvi_editor.headers.Authorization", "Bearer ${ZENVI_MCP_TOKEN}"),
    )
    try:
        for key, value in settings:
            result = subprocess.run(
                [hermes, "config", "set", key, value],
                # Explicit UTF-8: a GUI-launched app often has no LANG, and
                # text=True then decodes Hermes' "✓ Set ..." as ASCII and fails.
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=30, stdin=subprocess.DEVNULL,
                # The home HermesRunner and _hermes_is_registered use.
                env=_cli_child_env(),
            )
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or "hermes config set failed").strip()
    except Exception as e:
        return False, str(e)
    return True, (
        "Updated %s. Before running hermes, run:\n"
        "export ZENVI_MCP_TOKEN=%s"
    ) % (_hermes_config_path(), token)


# `cursor-agent models` prints "<id> - <name>" per model, flagging the one the
# CLI uses when no --model is given with "(current)" and Cursor's own pick
# with "(default)". Some names end in zero-width spaces.
_CURSOR_MODEL_LINE = re.compile(r"^([A-Za-z0-9][\w.\-]*) - (.+)$")
_CURSOR_MODEL_FLAGS = re.compile(
    r"\s*\(((?:current|default)(?:\s*,\s*(?:current|default))*)\)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# How many of a CLI's own models the picker shows before "search N more".
_FEATURED_FROM_CLI = 8


def parse_cursor_models(text: str) -> list:
    """Picker entries from ``cursor-agent models`` output, in the CLI's order.

    "CLI default" comes first and is preselected, tagged with the model the
    CLI says it would use. The list depends on the account and runs to
    hundreds of ids, so the menu opens on the CLI's first few and search
    reaches the rest.
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
        rows.append({"id": mid, "name": name, "rank": len(rows) + 1,
                     "featured": len(rows) < _FEATURED_FROM_CLI})
        if "current" in flags and not current:
            current = name
        if "default" in flags and not fallback:
            fallback = name
    if not rows:
        return []
    return [_cli_default_entry(current or fallback)] + rows


def _models_command_output(argv, attempts: int = 2) -> str:
    """stdout of a CLI's list-models command, or "" when it failed.

    A CLI's first start after boot can fail or stall (it opens its own
    database, refreshes a login), so one failed run is retried.
    """
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    name = " ".join(argv[1:])
    for attempt in range(attempts):
        if attempt:
            time.sleep(2)
        try:
            result = subprocess.run(
                argv, capture_output=True, encoding="utf-8", errors="replace",
                timeout=45, env=_cli_child_env(), **kwargs,
            )
        except Exception:
            log.warning("%s failed (attempt %d)", name, attempt + 1, exc_info=True)
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
        log.warning("%s exited %s (attempt %d): %s", name, result.returncode,
                    attempt + 1, (result.stderr or "").strip()[-200:])
    return ""


def probe_cursor_models(cli: str) -> list:
    """Ask ``cursor-agent models`` for this account's lineup (a network call)."""
    return parse_cursor_models(_models_command_output([cli, "models"]))


def parse_claude_models(text: str) -> list:
    """Picker entries from Claude Code's answer to a stream-json ``initialize``.

    The CLI lists what this account may pick (``/model``): ``value`` is what
    ``--model`` takes, ``description`` leads with the model's name. Its
    "default" entry becomes "CLI default", tagged with what that resolves to.
    """
    models = None
    for line in (text or "").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "control_response":
            reply = ev.get("response") or {}
            models = (reply.get("response") or reply).get("models")
            break
    rows, seen, default_name, default_efforts = [], set(), "", []
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("value"), str) or not m["value"]:
            continue
        mid = m["value"]
        name = (str(m.get("description") or "").split("\u00b7")[0].strip()
                or m.get("displayName") or mid)
        efforts = _effort_levels(m.get("supportedEffortLevels")) if m.get("supportsEffort") else []
        if mid == "default":
            default_name, default_efforts = name, efforts
            continue
        if mid in seen:
            continue
        seen.add(mid)
        row = {"id": mid, "name": name, "provider": "anthropic",
               "rank": len(rows) + 1, "featured": True}
        if efforts:
            row["efforts"] = efforts
        rows.append(row)
    return [_cli_default_entry(default_name, default_efforts)] + rows if rows else []


def _acp_style_probe(argv, requests, done, timeout: float = 60.0) -> str:
    """Run a CLI that talks JSON lines over stdio, send *requests* (the next
    one each time *done* says "more"), and return what it printed.

    *done(line_dict)* returns True to stop, a dict to send next, else None.
    """
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1, env=_cli_child_env(), **kwargs)
    except Exception:
        log.warning("%s failed to start", " ".join(argv[1:]), exc_info=True)
        return ""
    killer = threading.Timer(timeout, proc.kill)
    killer.daemon = True
    killer.start()
    out = []
    try:
        for msg in requests:
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            out.append(line)
            try:
                ev = json.loads(line)
            except Exception:
                continue
            step = done(ev) if isinstance(ev, dict) else None
            if step is True:
                break
            if isinstance(step, dict):
                proc.stdin.write(json.dumps(step) + "\n")
                proc.stdin.flush()
    except Exception:
        log.debug("%s probe failed", " ".join(argv[1:]), exc_info=True)
    finally:
        killer.cancel()
        try:
            proc.kill()
        except Exception:
            pass
    return "".join(out)


def probe_claude_models(cli: str) -> list:
    """Ask the installed Claude Code which models it offers (no turn is run)."""
    text = _acp_style_probe(
        [cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose"],
        [{"type": "control_request", "request_id": "zenvi-models",
          "request": {"subtype": "initialize"}}],
        lambda ev: ev.get("type") == "control_response" or None)
    return parse_claude_models(text)


def parse_hermes_models(state) -> list:
    """Picker entries from the ``models`` block of Hermes' ACP session answer."""
    if not isinstance(state, dict):
        return []
    rows, seen, current = [], set(), ""
    for m in state.get("availableModels") or []:
        mid = m.get("modelId") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        name = m.get("name") or mid
        row = {"id": mid, "name": name, "rank": len(rows) + 1, "featured": len(rows) < 12}
        provider = re.match(r"Provider: ([^\u2022]+)", str(m.get("description") or ""))
        if provider:
            row["provider"] = provider.group(1).strip()
        if mid == state.get("currentModelId"):
            current = name
        rows.append(row)
    return [_cli_default_entry(current)] + rows if rows else []


def probe_hermes_models(cli: str) -> list:
    """Open an ACP session just to read which models Hermes' provider offers.

    ponytail: this leaves one empty session in Hermes' history per read, so it
    runs once per CLI version (LIST_MODELS_ONCE); turns refresh the list too.
    """
    found = {}

    def done(ev):
        if ev.get("id") == 1 and "result" in ev:
            return {"jsonrpc": "2.0", "id": 2, "method": "session/new",
                    "params": {"cwd": _project_cwd(), "mcpServers": []}}
        if ev.get("id") in (1, 2):
            found.update((ev.get("result") or {}).get("models") or {})
            return True
        return None

    _acp_style_probe(
        [cli, "acp", "--accept-hooks"],
        [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": 1, "clientCapabilities": {}}}],
        done, timeout=90.0)
    return parse_hermes_models(found)


def parse_codex_models(text: str) -> list:
    """Picker entries from ``codex debug models``: the visible models, by priority.

    "CLI default" (whatever ~/.codex/config.toml picks) leads and is preselected.
    """
    try:
        models = json.loads(text or "")["models"]
    except Exception:
        return []
    shown = [m for m in models if isinstance(m, dict) and m.get("visibility") == "list"
             and isinstance(m.get("slug"), str) and m["slug"]]
    shown.sort(key=lambda m: m["priority"] if isinstance(m.get("priority"), (int, float)) else 1e9)
    rows, seen = [], set()
    for m in shown:
        if m["slug"] in seen:
            continue
        seen.add(m["slug"])
        row = {"id": m["slug"], "name": m.get("display_name") or m["slug"],
               "rank": len(rows) + 1, "featured": len(rows) < _FEATURED_FROM_CLI}
        levels = m.get("supported_reasoning_levels")
        efforts = _effort_levels([l.get("effort") for l in levels if isinstance(l, dict)]
                                 if isinstance(levels, list) else [])
        if efforts:
            row["efforts"] = efforts
        rows.append(row)
    if not rows:
        return []
    # "CLI default" is whichever of these config.toml names, which is not
    # ours to read: offer only the levels every listed model takes.
    shared = [e for e in rows[0].get("efforts", [])
              if all(e in r.get("efforts", []) for r in rows)]
    return [_cli_default_entry(efforts=shared)] + rows


def probe_codex_models(cli: str) -> list:
    """Ask ``codex debug models`` for the catalogue this install ships."""
    return parse_codex_models(_models_command_output([cli, "debug", "models"]))


def _opencode_models_cache() -> str:
    """OpenCode's copy of the models.dev catalogue (XDG cache, on Windows too)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(_resolved_home(), ".cache")
    return os.path.join(base, "opencode", "models.json")


def _opencode_efforts(path: str) -> dict:
    """``{"provider/model": [levels]}`` from OpenCode's model catalogue cache.

    ``opencode models`` prints ids only; the levels a model takes (its
    ``#variant``) are in the catalogue it downloaded.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            catalog = json.load(fh)
    except Exception:
        return {}
    out = {}
    for provider, entry in catalog.items() if isinstance(catalog, dict) else []:
        models = entry.get("models") if isinstance(entry, dict) else None
        for mid, model in models.items() if isinstance(models, dict) else []:
            options = model.get("reasoning_options") if isinstance(model, dict) else None
            for option in options if isinstance(options, list) else []:
                if isinstance(option, dict) and option.get("type") == "effort":
                    levels = _effort_levels(option.get("values"))
                    if levels:
                        out["%s/%s" % (provider, mid)] = levels
    return out


def parse_opencode_models(text: str, efforts=None) -> list:
    """Picker entries from ``opencode models``: one ``provider/model`` per line.

    The list follows the providers the user signed in to. It marks no default,
    so "CLI default" (whatever opencode.json picks) leads and is preselected.
    """
    rows, seen = [], set()
    for line in _ANSI.sub("", text or "").splitlines():
        mid = line.strip()
        if "/" not in mid or " " in mid or mid in seen:
            continue
        seen.add(mid)
        provider, _, name = mid.partition("/")
        row = {"id": mid, "name": name, "provider": provider,
               "rank": len(rows) + 1, "featured": len(rows) < _FEATURED_FROM_CLI}
        if (efforts or {}).get(mid):
            row["efforts"] = list(efforts[mid])
        rows.append(row)
    return [_cli_default_entry()] + rows if rows else []


def probe_opencode_models(cli: str) -> list:
    """Ask ``opencode models`` which models the signed-in providers offer."""
    return parse_opencode_models(_models_command_output([cli, "models"]),
                                 _opencode_efforts(_opencode_models_cache()))


# A CLI's model list is re-read on this cadence (the same as the backend
# lineups) or when its binary / version changes. A read that failed (logged
# out, offline) is retried on the next CLI detection, which runs every 60 s.
CLI_MODELS_TTL_S = 15 * 60
CLI_MODELS_RETRY_S = 55
_cli_models_read: dict = {}     # backend -> {"key", "at", "ok"}
_cli_models_lock = threading.Lock()


def refresh_cli_models(backend: str, version) -> bool:
    """Re-read *backend*'s model list if it is due; True when the lineup changed.

    For runners with a ``list_models`` hook. Blocking (runs the CLI); a failed
    read keeps the lineup already shown.
    """
    runner = CLI_RUNNERS.get(backend)
    if runner is None or runner.list_models is None:
        return False
    cli = _which_cli(runner.CLI_NAME)
    if not cli:
        return False
    key = (cli, version or "")
    now = time.monotonic()
    with _cli_models_lock:
        last = _cli_models_read.setdefault(backend, {"key": None, "at": 0.0, "ok": False})
        wait = CLI_MODELS_TTL_S if last["ok"] else CLI_MODELS_RETRY_S
        if last["key"] == key and (now - last["at"] < wait
                                   or (last["ok"] and runner.LIST_MODELS_ONCE)):
            return False
        last["key"], last["at"] = key, now
    rows = runner.list_models(cli)
    with _cli_models_lock:
        _cli_models_read[backend]["ok"] = bool(rows)
    return set_cli_lineup(backend, rows)


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
    # Claude Code only today: Anthropic OAuth missing/expired. Chat shows a
    # Sign-in card instead of a raw CLI dump. Payload is a short reason.
    auth_required = pyqtSignal(str)
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
    # Stop whatever the CLI left in its process group once it exits. Off by
    # default; see CursorCliRunner.
    REAP_ON_EXIT = False
    # No stdin: there is no one to type into it, and ``opencode run`` blocks
    # reading an inherited one. A CLI that talks over stdin (HermesRunner's
    # ACP) sets PIPE and starts the conversation in _after_launch.
    STDIN = subprocess.DEVNULL

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
        self._effort = ""              # reasoning effort for this turn ("" = CLI's own)
        self._pending_effort = ""
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

    def _reap_process_group(self):
        """SIGTERM anything still in the finished CLI's process group.

        run_request starts the CLI in its own session, so the group is ours.
        POSIX only: on Windows the tree cannot be found once its root exits.
        """
        proc = self._proc
        if proc is None or sys.platform == "win32":
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass   # nothing left in the group

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
        # Left by AIChatWindow just before this call, for this turn only (the
        # slot's signature is shared with AIChatWorker, which has no effort).
        self._effort, self._pending_effort = self._coerce_effort(self._pending_effort), ""
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
            if ready_err == CLI_AUTH_REQUIRED or is_cli_auth_error(ready_err):
                self._emit_auth_required(ready_err)
            else:
                self._emit_error(ready_err)
            return

        try:
            argv = self._build_argv(text)
            popen_kwargs = dict(
                stdin=self.STDIN,
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
        self._after_launch(text)

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
        if self.REAP_ON_EXIT:
            self._reap_process_group()

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

    def _emit_auth_required(self, text: str = ""):
        self._responded = True
        if not self._aborted:
            self.auth_required.emit(
                text or "Claude Code needs you to sign in again."
            )

    def _emit_error(self, text: str):
        if self._aborted:
            return
        blob = text or "Unknown error."
        # Claude OAuth recovery only — Codex/Cursor "authenticate" errors
        # must not open the Claude Sign-in card.
        if self.BACKEND_ID == BACKEND_CLAUDE and (
            is_cli_auth_error(blob)
            or any(is_cli_auth_error(line) for line in self._stderr_tail)
        ):
            self._emit_auth_required(blob)
            return
        self.error_occurred.emit(blob)

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

    def _coerce_effort(self, effort) -> str:
        """Keep *effort* only if the model this turn runs on lists it.

        The picker is shared across models and backends like the model pill
        is, and a CLI rejects a level its model does not take.
        """
        if not effort or not isinstance(effort, str):
            return ""
        wanted = self._model_id or CLI_DEFAULT_MODEL_ID
        for m in models_for_backend(self.BACKEND_ID):
            if m["id"] == wanted:
                return effort if effort in (m.get("efforts") or []) else ""
        return ""

    # -- subclass hooks ----------------------------------------------------
    @staticmethod
    def register(port: int, token: str):
        """Give this CLI Zenvi's MCP server (Connect). Returns ``(ok, message)``."""
        raise NotImplementedError

    # ``list_models(cli) -> picker entries`` for a CLI that can list its own
    # models (see refresh_cli_models); None when it cannot.
    list_models = None
    # Read the list once per CLI version instead of every 15 minutes.
    LIST_MODELS_ONCE = False

    def _ensure_ready(self):
        """Return an error string if the backend can't run, else None."""
        return None

    def _build_env(self):
        return _cli_child_env()

    def _build_argv(self, text: str):
        raise NotImplementedError

    def _after_launch(self, text: str):
        """Called once the CLI is running (a stdin protocol starts here)."""

    def _send_prompt_on_stdin(self, prompt: str):
        """Write *prompt* to the CLI's stdin and close it.

        argv is no place for a prompt on Windows: a ``.cmd`` launcher runs
        through cmd.exe, which cuts an argument at its first newline, and any
        local process can read another's command line.
        """
        proc = self._proc

        def _send():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except Exception:
                # The CLI already exited; the read loop reports its output.
                log.debug("%s stdin write failed", self.CLI_NAME, exc_info=True)

        # Not on this thread: a prompt larger than the pipe buffer would block
        # here while the CLI blocks writing the stdout nobody reads yet.
        threading.Thread(target=_send, name="cli-stdin", daemon=True).start()

    def _handle_event(self, ev: dict):
        raise NotImplementedError


class ClaudeCodeRunner(BaseAgentRunner):
    """Drives the Claude Code CLI (`claude -p --output-format stream-json`)."""

    CLI_NAME = "claude"
    DISPLAY_NAME = "Claude Code"
    BACKEND_ID = BACKEND_CLAUDE
    register = staticmethod(register_claude)
    list_models = staticmethod(probe_claude_models)
    STDIN = subprocess.PIPE

    # Built-in fallback lineup, used until the installed CLI has listed its
    # own (see ``models_for_backend``). ``rank`` orders the picker, ``featured`` decides
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

    def _ensure_ready(self):
        logged = claude_is_logged_in()
        if logged is False:
            return CLI_AUTH_REQUIRED
        return None

    def _build_argv(self, text: str):
        cfg = _write_claude_mcp_config(self._server)
        argv = [
            # No prompt argument: `-p` reads it from stdin (_after_launch).
            self._cli_path or self.CLI_NAME, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", cfg, "--strict-mcp-config",
            # The agent is driving the editor on the user's behalf from inside
            # the app ΓÇö there is no terminal to answer a permission prompt, so
            # a prompt would just hang the turn until it times out.
            "--dangerously-skip-permissions",
            # From a file, like the prompt on stdin: npm installs Claude Code as
            # claude.cmd, and cmd.exe cuts a command line at its first newline.
            "--append-system-prompt-file", _write_claude_system_prompt(),
        ]
        argv += _add_dir_args()
        if self._model_id:
            argv += ["--model", self._model_id]
        if self._effort:
            argv += ["--effort", self._effort]
        if self._cli_started and self._cli_session_id:
            argv += ["--resume", self._cli_session_id]
        else:
            argv += ["--session-id", self._cli_session_id]
        return argv

    def _after_launch(self, text: str):
        self._send_prompt_on_stdin(text or "")

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
    register = staticmethod(register_codex)
    # No built-in lineup: the installed Codex lists its own (refresh_cli_models).
    # Until it has, the picker offers only "CLI default".
    MODELS = [_cli_default_entry()]
    list_models = staticmethod(probe_codex_models)
    # The prompt goes in through stdin ("-"), not argv.
    STDIN = subprocess.PIPE

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
        if self._effort:
            common += ["-c", 'model_reasoning_effort="%s"' % self._effort]
        # Unlike Claude, Codex will not take an id we invent -- it mints its own
        # and reports it as ``thread.started``.  Resuming a seeded placeholder
        # would just fail, so wait until we have heard a real one.
        cli = self._cli_path or self.CLI_NAME
        # Codex has no --append-system-prompt; prefix import steering so it
        # does not Glob /mnt/c the way Claude did before the Claude prompt fix.
        # The prompt goes in through stdin ("-"), see _send_prompt_on_stdin.
        self._stdin_prompt = _agent_import_prompt() + "\n\n" + (text or "")
        if self._cli_started and self._cli_id_from_cli and self._cli_session_id:
            # `exec resume` has no --add-dir (it exits 2); the thread keeps
            # the folders its first turn was given.
            return [cli, "exec", "resume", self._cli_session_id, *common, "-"]
        return [cli, "exec", *common, *_add_dir_args(), "-"]

    def _after_launch(self, text: str):
        self._send_prompt_on_stdin(self._stdin_prompt)

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
    register = staticmethod(register_cursor)
    list_models = staticmethod(probe_cursor_models)
    # The models depend on the Cursor account, so the picker shows what
    # `cursor-agent models` lists (refresh_cli_models). Until it has, the
    # only choice is to leave the model to the CLI's own config.
    MODELS = [_cli_default_entry()]
    # cursor-agent exits without stopping the stdio MCP servers and the worker
    # it started, so every finished turn would leave them running.
    REAP_ON_EXIT = True
    # On Windows the CLI is a .cmd, which would cut the prompt at a newline.
    STDIN = subprocess.PIPE

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
        # No prompt argument: `-p` then reads it from stdin (_after_launch).
        return argv

    def _after_launch(self, text: str):
        self._send_prompt_on_stdin(text or "")

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


# Agent CLIs' own file and shell tools, renamed so the chat reads them as work
# on the user's files: under bare names like "read" or "bash"
# humanize_tool_name labels them as the Zenvi Assistant harness's
# motion-graphics steps (that harness runs on OpenCode). OpenCode and Hermes.
_PLAIN_TOOL_NAMES = {
    "bash": "run_shell_command",
    "read": "read_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "patch": "edit_file",
    "write": "write_file",
    "glob": "find_files",
    "grep": "search_files",
    "list": "list_directory",
}


class OpenCodeRunner(BaseAgentRunner):
    """Drives SST OpenCode (`opencode run --format json`)."""

    CLI_NAME = "opencode"
    DISPLAY_NAME = "OpenCode"
    BACKEND_ID = BACKEND_OPENCODE
    register = staticmethod(register_opencode)
    list_models = staticmethod(probe_opencode_models)
    # The models follow the user's signed-in providers, so the picker shows
    # what `opencode models` lists; until then only "CLI default".
    MODELS = [_cli_default_entry()]

    # OpenCode names MCP tools ``<server>_<tool>``.
    _MCP_PREFIX = "zenvi_editor_"

    def _build_env(self):
        extra = {}
        if self._server is not None:
            # Merged over the user's own config, so their other MCP servers
            # stay available -- the equivalent of Claude's --mcp-config.
            extra["OPENCODE_CONFIG"] = _write_opencode_mcp_config(self._server)
            if self._server.token:
                extra["ZENVI_MCP_TOKEN"] = self._server.token
        return _cli_child_env(extra)

    def _build_argv(self, text: str):
        argv = [
            _opencode_native(self._cli_path or self.CLI_NAME), "run", "--format", "json",
            # No terminal to answer a permission prompt (see Claude's
            # --dangerously-skip-permissions).
            "--auto", "--thinking",
        ]
        if self._model_id:
            # OpenCode calls an effort level a variant: provider/model#variant.
            argv += ["--model", self._model_id + ("#" + self._effort if self._effort else "")]
        # Like Codex, OpenCode mints its own session id ("ses_..."), and
        # --session with any other id fails with "Session not found".
        if self._cli_started and self._cli_id_from_cli and self._cli_session_id:
            argv += ["--session", self._cli_session_id]
        argv.append(text)
        return argv

    def _handle_event(self, ev: dict):
        sid = ev.get("sessionID") or ""
        if sid and not self._cli_id_from_cli:
            self._cli_session_id = sid
            self._cli_id_from_cli = True
            self._emit_cli_session()
        etype = ev.get("type")
        part = ev.get("part") or {}
        if etype == "text":
            # Parts arrive whole. The separator is streamed too, so the chat's
            # copy of the turn matches the joined reply (_final_segment_text).
            txt = part.get("text") or ""
            if txt:
                if self._final_text:
                    self._final_text += "\n\n"
                    self.token_received.emit("\n\n")
                self._final_text += txt
                self.token_received.emit(txt)
            return
        if etype == "reasoning":
            txt = part.get("text") or ""
            if txt:
                rid = "think_%s" % (part.get("id") or "0")
                self.tool_started.emit(rid, "thinking", "{}")
                self.tool_log.emit(rid, txt)
                self.tool_completed.emit(rid, True, "")
            return
        if etype == "tool_use":
            # Emitted once, when the call has already finished.
            state = part.get("state") or {}
            call_id = part.get("callID") or part.get("id") or ""
            name = part.get("tool") or "tool"
            if name.startswith(self._MCP_PREFIX):
                name = name[len(self._MCP_PREFIX):]
            else:
                name = _PLAIN_TOOL_NAMES.get(name, name)
            args = state.get("input")
            self.tool_started.emit(call_id, name,
                                   json.dumps(args if isinstance(args, dict) else {}, default=str))
            ok = state.get("status") != "error"
            out = state.get("output") if ok else state.get("error")
            self.tool_completed.emit(call_id, ok, str(out or ""))
            return
        if etype == "error":
            err = ev.get("error") or {}
            self._last_error = ((err.get("data") or {}).get("message")
                                or err.get("name") or "The agent reported an error.")


def _opencode_native(cli: str) -> str:
    """The binary behind npm's ``opencode.cmd`` shim, when there is one.

    A ``.cmd`` runs through cmd.exe, which re-parses the prompt argument and
    mangles quotes, ``&`` and ``%`` in it; the npm package ships a native exe.
    """
    if cli.lower().endswith(".cmd"):
        folder = os.path.dirname(cli)
        candidates = []
        try:
            with open(cli, "r", encoding="utf-8", errors="replace") as fh:
                # What the shim itself starts: an update can move the package
                # (opencode-ai -> @opencode/cli) and leave the old exe behind.
                named = re.search(r'"%dp0%[\\/]+([^"]+\.exe)"', fh.read())
            if named:
                candidates.append(os.path.join(folder, *re.split(r"[\\/]+", named.group(1))))
        except OSError:
            pass
        candidates.append(os.path.join(folder, "node_modules", "opencode-ai",
                                       "bin", "opencode.exe"))
        for native in candidates:
            if os.path.isfile(native):
                return native
    return cli


def _write_opencode_mcp_config(server) -> str:
    """Write the scoped ``OPENCODE_CONFIG`` file pointing at the in-app MCP server.

    It names the token by environment variable only, so it holds no secret.
    """
    cfg = {"mcp": {"zenvi_editor": _opencode_server_entry(server.url())}}
    path = os.path.abspath(os.path.join(_agent_mcp_dir(), "opencode_mcp.json"))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


class HermesRunner(BaseAgentRunner):
    """Drives Nous Research Hermes Agent over ACP (``hermes acp``).

    Hermes has no streaming-JSON print mode (``-z`` prints only the final
    text), so this speaks the Agent Client Protocol instead: newline-delimited
    JSON-RPC on stdin/stdout. One process per turn -- initialize, open or
    reload the session with the editor's MCP server, prompt, then close stdin
    so Hermes exits and the base read loop ends.
    """

    CLI_NAME = "hermes"
    DISPLAY_NAME = "Hermes"
    BACKEND_ID = BACKEND_HERMES
    register = staticmethod(register_hermes)
    # Hermes lists its models only inside an ACP session (probe_hermes_models,
    # and every turn's session answer); until then its own config's choice.
    MODELS = [_cli_default_entry()]
    list_models = staticmethod(probe_hermes_models)
    LIST_MODELS_ONCE = True
    STDIN = subprocess.PIPE

    def __init__(self, parent=None):
        super().__init__(parent)
        self._think_seq = 0
        self._reset_protocol()

    def _reset_protocol(self):
        self._rpc_id = 0
        self._pending: dict = {}      # request id -> method
        self._prompt = ""
        self._prompting = False
        self._think_id = ""
        # Prose between two tool starts, the unit the chat freezes into a
        # bubble when a tool begins (see CursorCliRunner).
        self._segments: list = []
        self._segment = ""

    def _build_env(self):
        # hermes acp also loads config.yaml, where Connect wrote
        # ``Bearer ${ZENVI_MCP_TOKEN}`` (see register_hermes).
        extra = {}
        if self._server is not None and self._server.token:
            extra["ZENVI_MCP_TOKEN"] = self._server.token
        return _cli_child_env(extra)

    def _build_argv(self, text: str):
        # The prompt travels over ACP (see _after_launch), never through argv.
        return [self._cli_path or self.CLI_NAME, "acp", "--accept-hooks"]

    def _after_launch(self, text: str):
        self._reset_protocol()
        self._prompt = text
        self._request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    # -- JSON-RPC plumbing -------------------------------------------------
    def _write(self, msg: dict):
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except Exception:
            # Hermes already exited (e.g. missing ACP extras); its output and
            # exit code are reported by the base read loop.
            log.debug("hermes stdin write failed", exc_info=True)

    def _request(self, method: str, params: dict):
        self._rpc_id += 1
        self._pending[self._rpc_id] = method
        self._write({"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params})

    def _finish(self):
        try:
            self._proc.stdin.close()
        except Exception:
            pass

    def _mcp_servers(self) -> list:
        # Passed per session, so the token only ever travels over the pipe.
        return [{
            "type": "http", "name": "zenvi_editor", "url": self._server.url(),
            "headers": [{"name": "Authorization", "value": "Bearer %s" % self._server.token}],
        }]

    def _new_session(self):
        self._request("session/new", {"cwd": self._cli_cwd, "mcpServers": self._mcp_servers()})

    def _begin_prompt(self, session_id: str, result: dict):
        """The session is open: note its models, pick ours, then prompt."""
        set_cli_lineup(self.BACKEND_ID, parse_hermes_models(result.get("models")))
        if self._model_id:
            self._request("session/set_model",
                          {"sessionId": session_id, "modelId": self._model_id})
        else:
            self._send_prompt(session_id)

    def _send_prompt(self, session_id: str):
        self._prompting = True
        self._request("session/prompt", {
            "sessionId": session_id, "prompt": [{"type": "text", "text": self._prompt}]})

    # -- events ------------------------------------------------------------
    def _handle_event(self, ev: dict):
        method = ev.get("method")
        if method:
            if "id" in ev:
                self._answer(ev)
            elif method == "session/update" and self._prompting:
                # Updates before the prompt are session/load replaying history
                # the chat already shows.
                self._handle_update((ev.get("params") or {}).get("update") or {})
            return

        kind = self._pending.pop(ev.get("id"), None)
        if kind is None:
            return
        if "error" in ev and kind == "session/load":
            # The stored conversation is gone: start a new one instead.
            self._new_session()
            return
        if "error" in ev and kind == "session/set_model":
            # Hermes no longer offers that model: its own default answers.
            self._send_prompt(self._cli_session_id)
            return
        if "error" in ev:
            err = ev.get("error") or {}
            data = err.get("data")
            self._last_error = ((data.get("details") if isinstance(data, dict) else "")
                                or err.get("message") or "Hermes reported an error.")
            self._finish()
            return
        result = ev.get("result") or {}
        if kind == "initialize":
            # Like Codex, Hermes mints its own ids; only reload one it gave us.
            if self._cli_started and self._cli_id_from_cli and self._cli_session_id:
                self._request("session/load", {
                    "sessionId": self._cli_session_id, "cwd": self._cli_cwd,
                    "mcpServers": self._mcp_servers()})
            else:
                self._new_session()
        elif kind == "session/load":
            # An unknown id comes back as an empty result: start over.
            if result:
                self._begin_prompt(self._cli_session_id, result)
            else:
                self._new_session()
        elif kind == "session/new":
            session_id = result.get("sessionId") or ""
            if not session_id:
                self._last_error = "Hermes did not start a session."
                self._finish()
                return
            self._cli_session_id = session_id
            self._cli_id_from_cli = True
            self._emit_cli_session()
            self._begin_prompt(session_id, result)
        elif kind == "session/set_model":
            self._send_prompt(self._cli_session_id)
        elif kind == "session/prompt":
            self._prompting = False
            self._close_thinking()
            if result.get("stopReason") == "refusal" and not self._final_text:
                self._last_error = "Hermes declined to answer."
            else:
                self._emit_response(self._final_text)
            self._finish()

    def _answer(self, ev: dict):
        """Reply to a request Hermes makes of us, so the turn never waits on it."""
        if ev.get("method") == "session/request_permission":
            # No one is there to click a permission dialog (see Claude's
            # --dangerously-skip-permissions).
            options = (ev.get("params") or {}).get("options") or []
            allow = next((o for o in options
                          if str(o.get("kind") or "").startswith("allow")), None)
            outcome = ({"outcome": "selected", "optionId": allow.get("optionId")}
                       if allow else {"outcome": "cancelled"})
            self._write({"jsonrpc": "2.0", "id": ev.get("id"), "result": {"outcome": outcome}})
            return
        self._write({"jsonrpc": "2.0", "id": ev.get("id"),
                     "error": {"code": -32601, "message": "Method not found"}})

    def _start_block(self, call_id: str, name: str, args: dict):
        if self._segment.strip():
            self._segments.append(self._segment)
        self._segment = ""
        self.tool_started.emit(call_id, name, json.dumps(args, default=str))

    def _close_thinking(self):
        if self._think_id:
            self.tool_completed.emit(self._think_id, True, "")
            self._think_id = ""

    def _handle_update(self, update: dict):
        kind = update.get("sessionUpdate")
        if kind == "agent_thought_chunk":
            txt = (update.get("content") or {}).get("text") or ""
            if txt:
                if not self._think_id:
                    self._think_seq += 1
                    self._think_id = "think_%d" % self._think_seq
                    self._start_block(self._think_id, "thinking", {})
                self.tool_log.emit(self._think_id, txt)
            return
        if kind == "agent_message_chunk":
            self._close_thinking()
            txt = (update.get("content") or {}).get("text") or ""
            if txt:
                self._segment += txt
                # Joined at tool boundaries, as the chat commits the prose, so
                # the end-of-turn reply renders only what is new (#200).
                self._final_text = "\n\n".join(self._segments + [self._segment])
                self.token_received.emit(txt)
            return
        if kind == "tool_call":
            self._close_thinking()
            args = update.get("rawInput")
            self._start_block(update.get("toolCallId") or "",
                              _hermes_tool_name(update.get("title")),
                              args if isinstance(args, dict) else {})
            return
        if kind == "tool_call_update":
            status = update.get("status")
            if status in ("completed", "failed"):
                ok, text = _hermes_tool_result(update)
                self.tool_completed.emit(update.get("toolCallId") or "", ok, text)


def _hermes_tool_name(title) -> str:
    """The tool name in a Hermes tool_call title.

    Editor tools come as ``mcp__zenvi_editor__<tool>``; Hermes' own as
    ``<tool>: <what it is doing>`` ("python: import json").
    """
    name = str(title or "tool").split(":", 1)[0].strip() or "tool"
    if name.startswith("mcp_zenvi_editor_"):          # older Hermes releases
        return name[len("mcp_zenvi_editor_"):]
    if name.startswith("mcp__"):
        return _strip_mcp_prefix(name)
    return _PLAIN_TOOL_NAMES.get(name, name)


def _hermes_tool_result(update: dict):
    """``(ok, text)`` of a finished Hermes tool call.

    Hermes wraps an MCP tool's answer in an ``<untrusted_tool_result>`` block
    with a warning paragraph, and reports a tool that raised as "completed"
    with ``{"error": ...}`` inside; only that tells the two apart.
    """
    ok = update.get("status") == "completed"
    parts = []
    for item in update.get("content") or []:
        if isinstance(item, dict):
            inner = item.get("content")
            if isinstance(inner, dict) and inner.get("text"):
                parts.append(inner["text"])
    text = "\n".join(parts) or str(update.get("rawOutput") or "")
    match = re.search(r"<untrusted_tool_result[^>]*>\n(.*?)\n?</untrusted_tool_result>",
                      text, re.S)
    if not match:
        return ok, text
    body = match.group(1).split("\n\n", 1)[-1].strip()   # drop the warning
    try:
        payload = json.loads(body)
    except Exception:
        return ok, body
    if isinstance(payload, dict) and payload.get("error"):
        return False, str(payload["error"])
    if isinstance(payload, dict) and "result" in payload:
        result = payload["result"]
        return ok, result if isinstance(result, str) else json.dumps(result, default=str)
    return ok, body


# Every CLI chat backend, in the order the agent picker lists them. The chat
# window builds its backend list, worker factory, CLI detection and Connect from
# this, so adding a CLI is its runner class plus one entry here.
CLI_RUNNERS = {
    runner.BACKEND_ID: runner
    for runner in (ClaudeCodeRunner, CodexRunner, CursorCliRunner, OpenCodeRunner, HermesRunner)
}


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


def _write_claude_system_prompt() -> str:
    """Write Claude's appended system prompt to a file and return its path."""
    path = os.path.abspath(os.path.join(_agent_mcp_dir(), "claude_system_prompt.txt"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_claude_code_system_prompt())
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
