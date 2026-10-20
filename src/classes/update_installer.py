"""
 @file
 @brief Installs an update staged by the AutoUpdater, after Zenvi has quit.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

How an update is installed:
  Zenvi never replaces its own running installation. apply_pending_update()
  checks the staged package and starts a small helper -- /bin/sh on macOS and
  Linux, PowerShell on Windows -- that waits for this process to exit (and
  for any other Zenvi session running from the same install), installs the
  update with a rollback, and reopens Zenvi. The helper reopens Zenvi in
  every case, the new version or (when installing failed) the old one, so an
  update never leaves the user with nothing to click.

  Replacing the app from inside it is what used to crash: the old process was
  still tearing down while its own bundle was renamed and deleted, a relaunch
  from inside an AppImage ran off a mount that vanished with its parent, and
  overwriting a running AppImage fails with "Text file busy".

  Each attempt is recorded in update_status.json. After MAX_INSTALL_ATTEMPTS
  failed attempts at one version, launching Zenvi stops trying (the update
  panel offers the manual download instead); Restart to Update still tries.

NOTE: This module intentionally avoids importing Qt or any heavy
dependency so it can run quickly at the very start of the process.
"""

import hashlib
import json
import os
import platform
import posixpath
import shutil
import subprocess
import sys
import time
import uuid


# ---------------------------------------------------------------------------
# Paths  (must match info.UPDATE_PATH / auto_updater staging)
# ---------------------------------------------------------------------------

def _resolve_staging_dir():
    """Must match info.UPDATE_PATH; fallback if info is not importable."""
    try:
        from classes import info
        return info.UPDATE_PATH
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".openshot_qt", "updates")


UPDATE_STAGING_DIR = _resolve_staging_dir()
UPDATE_MANIFEST = os.path.join(UPDATE_STAGING_DIR, "update_manifest.json")
UPDATE_LOG = os.path.join(UPDATE_STAGING_DIR, "install.log")
# Outcome of the last install attempt, written by us and by the helper.
UPDATE_STATUS = os.path.join(UPDATE_STAGING_DIR, "update_status.json")

# Launching Zenvi stops installing a version after this many failed attempts.
MAX_INSTALL_ATTEMPTS = 2

# How long the helper waits for Zenvi to quit before it ends the process.
APP_EXIT_TIMEOUT = 120
# How long it waits for other Zenvi sessions (e.g. a headless one) to finish
# before postponing the update.
OTHER_SESSIONS_TIMEOUT = 60

_HELPER_SH_NAME = "zenvi_update_helper.sh"
_UPDATE_HELPER_SCRIPT_NAME = "zenvi_update_helper.ps1"

# Bundle ids of apps this updater may replace (macOS).
_MAC_BUNDLE_ID = "org.zenvi.Zenvi"


# ---------------------------------------------------------------------------
# Version comparison (shared with auto_updater)
# ---------------------------------------------------------------------------

def parse_version(version_str):
    """Parse '3.4.1' or 'v3.4.1' into a comparable tuple.

    Build metadata (+foo) and pre-release suffixes (-rc1) are stripped so
    ``1.1.0-rc1`` still compares as ``(1, 1, 0)`` rather than ``(0,)``.
    """
    try:
        clean = (version_str or "").strip().lstrip("v")
        for sep in ("+", "-"):
            if sep in clean:
                clean = clean.split(sep, 1)[0]
        parts = [int(x) for x in clean.split(".") if x != ""]
        return tuple(parts) if parts else (0,)
    except (ValueError, AttributeError):
        return (0,)


def is_version_newer(remote_version, local_version):
    """Return True when remote_version is strictly newer than local_version."""
    return parse_version(remote_version) > parse_version(local_version)


def _running_version():
    try:
        from classes import info
        return info.VERSION
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Logging (minimal — no dependency on classes.logger)
# ---------------------------------------------------------------------------

def _log(msg):
    line = f"[ZenviUpdater] {msg}"
    try:
        print(line)
    except (UnicodeEncodeError, AttributeError, OSError, ValueError):
        # Windows consoles are often cp1252, and frozen GUI builds have no
        # stdout at all; never let logging abort an install.
        pass
    try:
        os.makedirs(os.path.dirname(UPDATE_LOG), exist_ok=True)
        with open(UPDATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Manifest and status
# ---------------------------------------------------------------------------

def read_manifest():
    """Read and return the update manifest dict, or None."""
    try:
        with open(UPDATE_MANIFEST, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return manifest if isinstance(manifest, dict) else None
    except Exception:
        return None


def _read_manifest():
    return read_manifest()


def write_manifest(manifest):
    """Write the manifest atomically: a crash mid-write leaves the old one."""
    os.makedirs(os.path.dirname(UPDATE_MANIFEST), exist_ok=True)
    tmp = UPDATE_MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, UPDATE_MANIFEST)


def read_status():
    """The last install attempt: {"version", "attempts", "ok", "reason", "time"} or {}."""
    try:
        with open(UPDATE_STATUS, "r", encoding="utf-8-sig") as fh:
            status = json.load(fh)
        return status if isinstance(status, dict) else {}
    except Exception:
        return {}


def _write_status(status):
    try:
        os.makedirs(os.path.dirname(UPDATE_STATUS), exist_ok=True)
        tmp = UPDATE_STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=2)
        os.replace(tmp, UPDATE_STATUS)
    except OSError as exc:
        _log(f"Could not record the update status: {exc}")


# A launch right after a failed attempt (often the reopened old version)
# doesn't try again for this long.
RETRY_LAUNCH_AFTER = 600
# An attempt still marked "installing" is taken as running for this long
# when its helper's pid can't be checked (Windows).
INSTALL_STALE_AFTER = 900
_LOCK_DIR_NAME = ".install-lock"


def install_in_progress():
    """True while an installer helper is (likely) still working."""
    status = read_status()
    if status.get("ok") is not None:
        return False
    try:
        age = time.time() - float(status.get("time") or 0)
    except (TypeError, ValueError):
        return False
    if age > INSTALL_STALE_AFTER:
        return False
    try:
        with open(os.path.join(UPDATE_STAGING_DIR, _LOCK_DIR_NAME, "pid"), encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        # No helper has taken the lock (yet): only a just-started one counts.
        return age < 30
    return _pid_alive(pid)


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def install_attempts(version):
    """How many times installing *version* was tried and did not finish."""
    status = read_status()
    if status.get("version") != version or status.get("ok") is True:
        return 0
    try:
        return int(status.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def last_failure(version):
    """The reason the last attempt at *version* failed, or ""."""
    status = read_status()
    if status.get("version") == version and status.get("ok") is False:
        return str(status.get("reason") or "The update could not be installed")
    return ""


def _record_attempt(version):
    attempts = install_attempts(version) + 1
    _write_status({"version": version, "attempts": attempts, "ok": None,
                   "reason": "installing", "time": int(time.time())})
    return attempts


def _record_failure(version, reason, attempts=None):
    if attempts is None:
        attempts = max(1, install_attempts(version))
    _write_status({"version": version, "attempts": attempts, "ok": False,
                   "reason": reason, "time": int(time.time())})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def has_pending_update(for_launch=False):
    """Return True if a verified-looking update package is staged and newer
    than the running version.

    for_launch: also require that launching hasn't already failed to install
    this version MAX_INSTALL_ATTEMPTS times.
    """
    manifest = read_manifest()
    if not manifest:
        return False
    filepath = manifest.get("filepath", "")
    if not (filepath and os.path.isfile(filepath)):
        return False
    staged_ver = manifest.get("version", "")
    running = _running_version()
    if staged_ver and running and not is_version_newer(staged_ver, running):
        return False
    if for_launch:
        if install_attempts(staged_ver) >= MAX_INSTALL_ATTEMPTS:
            return False
        status = read_status()
        try:
            recent = time.time() - float(status.get("time") or 0) < RETRY_LAUNCH_AFTER
        except (TypeError, ValueError):
            recent = False
        if status.get("version") == staged_ver and status.get("ok") is False and recent:
            # The helper just reopened this version after a failed or
            # postponed attempt: don't go straight back into another one.
            return False
    return True


def apply_pending_update(wait_pid=None, user_requested=False):
    """Hand a staged update to the installer helper.

    Returns True once the helper is running: the caller must then exit
    promptly (the helper waits for *wait_pid*, this process by default,
    before touching the installation). Returns False when nothing was
    started; the app keeps running as it is.
    """
    manifest = read_manifest()
    if manifest is None:
        return False

    version = manifest.get("version", "")
    filepath = manifest.get("filepath", "")
    filename = manifest.get("filename", "") or os.path.basename(filepath)
    running = _running_version()
    if version and running and not is_version_newer(version, running):
        _log(f"Staged {version} is not newer than {running}; discarding it")
        discard_staged_update(manifest)
        return False
    if not user_requested and install_attempts(version) >= MAX_INSTALL_ATTEMPTS:
        _log(f"Installing {version} already failed {MAX_INSTALL_ATTEMPTS} times; "
             "waiting for Restart to Update or a manual install")
        return False

    _log(f"Applying staged update  version={version}  file={filename}")

    if not _verify_integrity(manifest):
        _log("Integrity check FAILED — discarding staged update")
        _cleanup(manifest)
        return False

    if wait_pid is None:
        wait_pid = os.getpid()

    system = platform.system().lower()
    attempts = _record_attempt(version)
    try:
        if system == "darwin":
            ok = _apply_macos(filepath, filename, version, wait_pid, attempts)
        elif system == "linux":
            ok = _apply_linux(filepath, filename, version, wait_pid, attempts)
        elif system == "windows":
            ok = _apply_windows(filepath, filename, version=version,
                                wait_pid=wait_pid, attempts=attempts)
        else:
            _log(f"Unsupported platform: {system}")
            ok = False
    except Exception as exc:
        _log(f"Installation error: {exc}")
        ok = False

    if ok:
        _log("Update handed to the installer helper; Zenvi exits so it can install")
    else:
        _record_failure(version, "The installer could not be started", attempts)
        _log("Update could not be started — keeping staged files for retry")
    return ok


def _show_update_notice(system, version):
    """Post a plain native OS notification while the update applies. This
    runs before PyQt is loaded, so it's the system notification center —
    no custom window, no dependencies, nothing to build or theme."""
    message = f"Updating to version {version}…" if version else "Installing update…"
    try:
        if system == "darwin":
            safe = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "Zenvi"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "linux":
            subprocess.Popen(
                ["notify-send", "Zenvi", message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "windows":
            safe = message.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(8000, 'Zenvi', '%s', "
                "[System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Seconds 8; $n.Dispose()"
            ) % safe
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-WindowStyle", "Hidden", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helper process environment
# ---------------------------------------------------------------------------

# The AppImage runtime describes the old mount in these; the reopened
# AppImage sets its own. DYLD_ variables only ever come from launch-mac.
_ENV_DROP = ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "PYTHONHOME")
_ENV_DROP_PREFIXES = ("DYLD_",)


def _install_dirs():
    """Directories that belong to the running installation."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.realpath(sys.executable)))
    for var in ("APPDIR",):
        if os.environ.get(var):
            dirs.append(os.environ[var])
    bundle = _running_bundle()
    if bundle:
        dirs.append(bundle)
    return [os.path.normcase(os.path.abspath(d)) for d in dirs if d]


def _clean_env(base=None, install_dirs=None):
    """A copy of the environment with nothing pointing into the installation.

    The launchers prepend the install's own folders to LD_LIBRARY_PATH,
    PYTHONPATH, QT_PLUGIN_PATH, XDG_DATA_DIRS...; the helper, and the Zenvi
    it reopens, must not load anything from the copy being replaced. Only
    those entries go: the user's own settings (a snap browser's
    XDG_DATA_DIRS, their Qt theme, their PATH) stay.
    """
    env = dict(os.environ if base is None else base)
    dirs = _install_dirs() if install_dirs is None else install_dirs

    def inside(part):
        if "/.mount_" in part or "/AppTranslocation/" in part:
            return True
        norm = os.path.normcase(os.path.abspath(part))
        return any(norm == d or norm.startswith(d + os.sep) for d in dirs)

    for key in list(env):
        upper = key.upper()
        if upper in _ENV_DROP or upper.startswith(_ENV_DROP_PREFIXES):
            del env[key]
            continue
        value = env[key]
        if not value or not any(sep in value for sep in ("/", "\\")):
            continue
        parts = value.split(os.pathsep)
        kept = [part for part in parts if not (part and inside(part))]
        if len(kept) == len(parts):
            continue
        if any(kept) or upper == "PATH":
            env[key] = os.pathsep.join(kept)
        else:
            del env[key]
    if os.name != "nt":
        parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
        for system_dir in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            if system_dir not in parts:
                parts.append(system_dir)
        env["PATH"] = os.pathsep.join(parts)
    return env


def _helper_path(suffix):
    """A script file of its own for each helper: a second one must never
    overwrite a script the first is still reading."""
    return os.path.join(UPDATE_STAGING_DIR, "zenvi_update_helper-%d-%s%s" % (
        os.getpid(), uuid.uuid4().hex[:12], suffix))


def _spawn_posix_helper(script, args):
    """Write *script* to the staging dir and run it with /bin/sh, detached
    from this process (its own session, so it outlives our exit)."""
    path = _helper_path(".sh")
    try:
        os.makedirs(UPDATE_STAGING_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
    except OSError as exc:
        _log(f"Failed to write the update helper: {exc}")
        return False
    try:
        subprocess.Popen(
            ["/bin/sh", path] + [str(a) for a in args],
            cwd="/",
            env=_clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"Failed to launch the update helper: {exc}")
        return False
    return True


# Shared by the macOS and Linux helpers: logging, the status file, and
# waiting for Zenvi to quit. Arguments are positional so no path is ever
# parsed as shell syntax.
_SH_COMMON = r'''
log() {
    printf '%s  [ZenviUpdater] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
}

json_str() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\n\r'
}

status() {  # status true|false "reason" [postponed]
    attempts="$ATTEMPTS"
    postponed=false
    if [ "$3" = postponed ]; then
        # Postponing isn't a failed attempt.
        attempts=$((ATTEMPTS > 0 ? ATTEMPTS - 1 : 0))
        postponed=true
    fi
    printf '{"version": "%s", "attempts": %s, "ok": %s, "postponed": %s, "reason": "%s", "time": %s}\n' \
        "$(json_str "$VERSION")" "$attempts" "$1" "$postponed" "$(json_str "$2")" "$(date +%s)" \
        > "$STATUS.tmp" 2>/dev/null && mv -f "$STATUS.tmp" "$STATUS" 2>/dev/null
}

alive() {  # running, and not a zombie its parent hasn't reaped
    kill -0 "$1" 2>/dev/null || return 1
    case "$(ps -o stat= -p "$1" 2>/dev/null)" in
        Z*) return 1 ;;
    esac
    return 0
}

LOCK="$(dirname "$STATUS")/.install-lock"
take_lock() {  # one installer helper at a time
    if ! mkdir "$LOCK" 2>/dev/null; then
        holder=$(cat "$LOCK/pid" 2>/dev/null)
        if [ -n "$holder" ] && alive "$holder"; then
            return 1
        fi
        rm -rf "$LOCK"
        mkdir "$LOCK" 2>/dev/null || return 1
    fi
    echo $$ > "$LOCK/pid"
    trap 'rm -rf "$LOCK"' EXIT
    return 0
}

if ! take_lock; then
    log "Another update helper is already installing; this one stops"
    rm -f "$0"
    exit 0
fi

wait_for_exit() {  # Zenvi's pid: wait, then end it if it hangs (it was asked to quit)
    n=0
    while alive "$1"; do
        n=$((n + 1))
        if [ "$n" -ge $((APP_EXIT_TIMEOUT * 5)) ]; then
            log "Zenvi (pid $1) did not quit in ${APP_EXIT_TIMEOUT} s; ending it"
            kill -TERM "$1" 2>/dev/null
            sleep 5
            kill -KILL "$1" 2>/dev/null
            sleep 1
            break
        fi
        sleep 0.2
    done
}

wait_for_others() {  # until other_sessions finds nothing; 1 if they outlast the timeout
    n=0
    while [ -n "$(other_sessions)" ]; do
        n=$((n + 1))
        if [ "$n" -ge $((OTHER_TIMEOUT * 2)) ]; then
            return 1
        fi
        sleep 0.5
    done
    return 0
}

'''


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _apply_linux(filepath, filename, version="", wait_pid=None, attempts=1):
    if filename.endswith(".AppImage"):
        return _apply_appimage(filepath, version, wait_pid, attempts)
    if filename.endswith(".deb"):
        return _apply_deb(filepath, version, wait_pid, attempts)
    _log(f"Unknown Linux package type: {filename}")
    return False


_APPIMAGE_HELPER = r'''#!/bin/sh
# Zenvi update helper (AppImage): replaces the AppImage once Zenvi has quit,
# then starts it again.
PID="$1"; NEW="$2"; TARGET="$3"; MANIFEST="$4"; LOG="$5"; STATUS="$6"
VERSION="$7"; ATTEMPTS="$8"; APP_EXIT_TIMEOUT="$9"; shift 9
OTHER_TIMEOUT="$1"; RELAUNCH="$2"
''' + _SH_COMMON + r'''
other_sessions() {  # Zenvis still running from the AppImage being replaced
    for exe in /proc/[0-9]*/exe; do
        [ "$(readlink "$exe" 2>/dev/null)" = "$TARGET" ] && echo "${exe#/proc/}"
    done
}

reopen() {
    if [ "$RELAUNCH" = 1 ] && [ -x "$TARGET" ]; then
        if command -v setsid >/dev/null 2>&1; then
            setsid "$TARGET" >/dev/null 2>&1 < /dev/null &
        else
            nohup "$TARGET" >/dev/null 2>&1 < /dev/null &
        fi
        log "Reopened $TARGET"
    fi
}

fail() {
    log "Update to $VERSION failed: $1"
    rm -f "$TMP" 2>/dev/null
    status false "$1"
    reopen
    rm -f "$0"
    exit 1
}

log "Update helper started for $VERSION; waiting for Zenvi (pid $PID) to quit"
wait_for_exit "$PID"
if ! wait_for_others; then
    log "Another Zenvi session is still running from $TARGET; update postponed"
    status false "Another Zenvi session was still running. Close it, then use Restart to Update." postponed
    reopen
    rm -f "$0"
    exit 0
fi

DIR=$(dirname "$TARGET")
TMP="$DIR/.$(basename "$TARGET").zenvi-update"
mkdir -p "$DIR" 2>/dev/null
# Copy next to the target, then rename over it: a rename never hits
# "Text file busy" and leaves no half-written AppImage behind.
cp "$NEW" "$TMP" 2>>"$LOG" || fail "could not copy the new AppImage into $DIR"
chmod 755 "$TMP" 2>>"$LOG" || fail "could not make the new AppImage executable"
mv -f "$TMP" "$TARGET" 2>>"$LOG" || fail "could not replace $TARGET"

rm -f "$MANIFEST" "$NEW"
status true "installed"
log "Installed $VERSION into $TARGET"
reopen
rm -f "$0"
exit 0
'''


def _apply_appimage(filepath, version="", wait_pid=None, attempts=1, relaunch=True):
    """Replace the AppImage Zenvi runs from (after it quits) and start it again."""
    target = os.environ.get("APPIMAGE") or _find_current_appimage()
    if not target:
        target = os.path.expanduser("~/Applications/Zenvi.AppImage")
        _log(f"No existing AppImage detected — installing to {target}")
    target = os.path.realpath(target)
    _log(f"Replacing AppImage  {target}")
    _show_update_notice("linux", version)
    return _spawn_posix_helper(_APPIMAGE_HELPER, [
        wait_pid or os.getpid(), filepath, target, UPDATE_MANIFEST, UPDATE_LOG,
        UPDATE_STATUS, version, attempts, APP_EXIT_TIMEOUT,
        OTHER_SESSIONS_TIMEOUT, 1 if relaunch else 0,
    ])


_DEB_HELPER = r'''#!/bin/sh
# Zenvi update helper (.deb): installs the package once Zenvi has quit
# (asking for an administrator password), then starts Zenvi again.
PID="$1"; DEB="$2"; APP_EXE="$3"; MANIFEST="$4"; LOG="$5"; STATUS="$6"
VERSION="$7"; ATTEMPTS="$8"; APP_EXIT_TIMEOUT="$9"; shift 9
OTHER_TIMEOUT="$1"; RELAUNCH="$2"; LAUNCHER="$3"
''' + _SH_COMMON + r'''
other_sessions() {  # Zenvis still running from the installed package
    for exe in /proc/[0-9]*/exe; do
        [ "$(readlink "$exe" 2>/dev/null)" = "$APP_EXE" ] && echo "${exe#/proc/}"
    done
}

reopen() {
    if [ "$RELAUNCH" = 1 ] && [ -x "$LAUNCHER" ]; then
        if command -v setsid >/dev/null 2>&1; then
            setsid "$LAUNCHER" >/dev/null 2>&1 < /dev/null &
        else
            nohup "$LAUNCHER" >/dev/null 2>&1 < /dev/null &
        fi
        log "Reopened $LAUNCHER"
    fi
}

fail() {
    log "Update to $VERSION failed: $1"
    status false "$1"
    reopen
    rm -f "$0"
    exit 1
}

log "Update helper started for $VERSION; waiting for Zenvi (pid $PID) to quit"
wait_for_exit "$PID"
if ! wait_for_others; then
    log "Another Zenvi session is still running ($APP_EXE); update postponed"
    status false "Another Zenvi session was still running. Close it, then use Restart to Update." postponed
    reopen
    rm -f "$0"
    exit 0
fi
command -v pkexec >/dev/null 2>&1 || fail "installing a .deb needs pkexec (PolicyKit)"
# apt-get resolves any new dependencies; dpkg alone would leave the package
# half-configured if one were missing.
if ! pkexec /bin/sh -c 'if command -v apt-get >/dev/null 2>&1; then apt-get install -y --allow-downgrades "$1"; else dpkg -i "$1"; fi' sh "$DEB" >>"$LOG" 2>&1; then
    fail "the package manager did not install the update (or the password prompt was cancelled)"
fi

rm -f "$MANIFEST" "$DEB"
status true "installed"
log "Installed $VERSION"
reopen
rm -f "$0"
exit 0
'''


def _deb_launcher():
    """The command the .deb puts on PATH (it sets the library paths first)."""
    found = shutil.which("zenvi")
    if found:
        return found
    return "/usr/bin/zenvi"


def _apply_deb(filepath, version="", wait_pid=None, attempts=1, relaunch=True):
    """Install a .deb package (asks for an administrator password) after Zenvi quits."""
    _log(f"Installing deb: {filepath}")
    app_exe = "/opt/zenvi/zenvi"
    if getattr(sys, "frozen", False):
        app_exe = os.path.realpath(sys.executable)
    _show_update_notice("linux", version)
    return _spawn_posix_helper(_DEB_HELPER, [
        wait_pid or os.getpid(), filepath, app_exe, UPDATE_MANIFEST, UPDATE_LOG,
        UPDATE_STATUS, version, attempts, APP_EXIT_TIMEOUT,
        OTHER_SESSIONS_TIMEOUT, 1 if relaunch else 0, _deb_launcher(),
    ])


def _find_current_appimage():
    """Best-effort search for an existing Zenvi AppImage on the system."""
    # /proc/self/exe on Linux
    try:
        exe = os.readlink("/proc/self/exe")
        if exe.endswith(".AppImage"):
            return exe
    except OSError:
        pass

    search_dirs = [
        os.path.expanduser("~/Applications"),
        os.path.expanduser("~/Desktop"),
        "/usr/local/bin",
        "/opt",
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for entry in os.listdir(d):
                low = entry.lower()
                if "zenvi" in low and low.endswith(".appimage"):
                    return os.path.join(d, entry)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

_MACOS_HELPER = r'''#!/bin/sh
# Zenvi update helper (macOS): installs the app from a staged disk image
# once Zenvi has quit, then opens Zenvi again.
PID="$1"; DMG="$2"; TARGET="$3"; MANIFEST="$4"; LOG="$5"; STATUS="$6"
VERSION="$7"; ATTEMPTS="$8"; APP_EXIT_TIMEOUT="$9"; shift 9
OTHER_TIMEOUT="$1"; OPEN="$2"; BUNDLE_ID="$3"; RUNNING="$4"
''' + _SH_COMMON + r'''
NEW="$TARGET.zenvi-update-new"
OLD="$TARGET.zenvi-update-old"
MNT=""
STAGE=""

other_sessions() {  # Zenvis still running from the bundle being replaced
    # A prefix match on the command line, not a regex: paths may hold ( ) + .
    /bin/ps -axo pid=,command= 2>/dev/null | while read -r pid cmd; do
        case "$cmd" in
            "$TARGET/Contents/MacOS/zenvi"|"$TARGET/Contents/MacOS/zenvi "*|\
            "$TARGET/Contents/MacOS/launch-mac"|"$TARGET/Contents/MacOS/launch-mac "*)
                echo "$pid" ;;
        esac
    done
}

reopen() {  # the installed Zenvi, else the copy that asked for the update
    [ -n "$OPEN" ] || return
    for app in "$TARGET" "$RUNNING"; do
        if [ -n "$app" ] && [ -d "$app" ]; then
            "$OPEN" "$app" >>"$LOG" 2>&1 || "$OPEN" -n "$app" >>"$LOG" 2>&1
            log "Reopened $app"
            return
        fi
    done
}

detach() {
    if [ -n "$MNT" ]; then
        /usr/bin/hdiutil detach "$MNT" -quiet >/dev/null 2>&1 \
            || /usr/bin/hdiutil detach "$MNT" -force -quiet >/dev/null 2>&1
        rmdir "$MNT" 2>/dev/null
        MNT=""
    fi
}

fail() {
    log "Update to $VERSION failed: $1"
    detach
    rm -rf "$NEW" 2>/dev/null
    [ -n "$STAGE" ] && rm -rf "$STAGE" 2>/dev/null
    if [ ! -d "$TARGET" ] && [ -d "$OLD" ]; then
        mv "$OLD" "$TARGET" 2>>"$LOG" && log "Put the previous version back"
    fi
    status false "$1"
    reopen
    rm -f "$0"
    exit 1
}

as_admin() {  # run "$@" with an administrator password prompt
    cmd=""
    for a in "$@"; do
        q=$(printf '%s' "$a" | sed "s/'/'\\\\''/g")
        cmd="$cmd '$q'"
    done
    /usr/bin/osascript - "$cmd" >>"$LOG" 2>&1 <<'OSA'
on run argv
    do shell script (item 1 of argv) with prompt "Zenvi needs your permission to install its update." with administrator privileges
end run
OSA
}

log "Update helper started for $VERSION; waiting for Zenvi (pid $PID) to quit"
wait_for_exit "$PID"
if ! wait_for_others; then
    log "Another Zenvi session is still running from $TARGET; update postponed"
    status false "Another Zenvi session was still running. Close it, then use Restart to Update." postponed
    reopen
    rm -f "$0"
    exit 0
fi

MNT=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/zenvi_update.XXXXXX") || fail "could not create a mount point"
/usr/bin/hdiutil attach "$DMG" -mountpoint "$MNT" -nobrowse -noautoopen -readonly -quiet >>"$LOG" 2>&1 \
    || { rmdir "$MNT" 2>/dev/null; MNT=""; fail "could not open the update disk image"; }

APP=""
for candidate in "$MNT"/*.app; do
    if [ -d "$candidate" ] && [ "$(/usr/bin/defaults read "$candidate/Contents/Info" CFBundleIdentifier 2>/dev/null)" = "$BUNDLE_ID" ]; then
        APP="$candidate"
        break
    fi
done
[ -n "$APP" ] || fail "the disk image holds no Zenvi app"

PARENT=$(dirname "$TARGET")
if [ -w "$PARENT" ] && { [ ! -e "$TARGET" ] || [ -w "$TARGET" ]; }; then
    ADMIN=0
    STAGE_NEW="$NEW"
else
    # Not ours to write (e.g. /Applications for a standard user): stage in a
    # temp folder, then ask for an administrator password for the swap.
    ADMIN=1
    STAGE=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/zenvi_update_app.XXXXXX") || fail "could not create a staging folder"
    STAGE_NEW="$STAGE/$(basename "$TARGET")"
fi

rm -rf "$STAGE_NEW" 2>/dev/null
# ditto keeps symlinks, permissions and extended attributes, so the code
# signature survives the copy.
/usr/bin/ditto "$APP" "$STAGE_NEW" 2>>"$LOG" || fail "could not copy the new version"
detach
/usr/bin/xattr -dr com.apple.quarantine "$STAGE_NEW" 2>/dev/null
EXE=$(/usr/bin/defaults read "$STAGE_NEW/Contents/Info" CFBundleExecutable 2>/dev/null)
[ -n "$EXE" ] && [ -x "$STAGE_NEW/Contents/MacOS/$EXE" ] || fail "the new version is incomplete"

if [ "$ADMIN" = 0 ]; then
    rm -rf "$OLD" 2>/dev/null
    if [ -e "$TARGET" ]; then
        mv "$TARGET" "$OLD" 2>>"$LOG" || fail "could not move the current version aside"
    fi
    mv "$STAGE_NEW" "$TARGET" 2>>"$LOG" || fail "could not move the new version into place"
else
    # One password prompt: swap, then remove the old version, in one go.
    as_admin /bin/sh -c 'rm -rf "$3" && { [ ! -e "$2" ] || mv "$2" "$3"; } && mv "$1" "$2" || { [ -d "$3" ] && [ ! -e "$2" ] && mv "$3" "$2"; exit 1; }; rm -rf "$3"' \
        sh "$STAGE_NEW" "$TARGET" "$OLD" \
        || fail "the update needs an administrator to install it (the password prompt was cancelled or refused)"
    rm -rf "$STAGE" 2>/dev/null
fi

rm -f "$MANIFEST" "$DMG"
status true "installed"
log "Installed $VERSION into $TARGET"
reopen
# The old version goes last, so Zenvi is back on screen without waiting on it.
[ -e "$OLD" ] && rm -rf "$OLD" 2>/dev/null
rm -f "$0"
exit 0
'''


def _running_bundle():
    """The .app bundle this (frozen) process runs from, or None."""
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    path = os.path.realpath(sys.executable)
    while path and path != os.path.dirname(path):
        if path.endswith(".app"):
            return path
        path = os.path.dirname(path)
    return None


def _macos_install_target():
    """The bundle the update replaces: the one Zenvi runs from, unless macOS
    runs it from a read-only place (the disk image, App Translocation)."""
    # macOS paths, also when a test runs this on another platform.
    apps_dir = os.environ.get("ZENVI_UPDATE_APPLICATIONS_DIR")
    if apps_dir:
        return posixpath.join(apps_dir, "Zenvi.app")
    bundle = _running_bundle()
    if bundle and "/AppTranslocation/" not in bundle and not bundle.startswith("/Volumes/"):
        return bundle
    if bundle and bundle.startswith("/Volumes/") and os.access(posixpath.dirname(bundle), os.W_OK):
        # A writable external drive, not a mounted disk image.
        return bundle
    return posixpath.join("/Applications", posixpath.basename(bundle) if bundle else "Zenvi.app")


def _apply_macos(filepath, filename, version="", wait_pid=None, attempts=1, open_cmd="/usr/bin/open"):
    """Install the app from a staged .dmg (after Zenvi quits) and open it again."""
    if not filename.endswith(".dmg"):
        _log(f"Unknown macOS package type: {filename}")
        return False
    target = _macos_install_target()
    _log(f"Installing {filename} into {target} once Zenvi quits")
    _show_update_notice("darwin", version)
    return _spawn_posix_helper(_MACOS_HELPER, [
        wait_pid or os.getpid(), filepath, target, UPDATE_MANIFEST, UPDATE_LOG,
        UPDATE_STATUS, version, attempts, APP_EXIT_TIMEOUT,
        OTHER_SESSIONS_TIMEOUT, open_cmd or "", _MAC_BUNDLE_ID, _running_bundle() or "",
    ])


def cleanup_leftover_bundles():
    """Remove copies of the app that an interrupted update left beside it:
    ours (.zenvi-update-new/-old) and older updaters' (.new/.bak).

    Only directories next to the running bundle whose Info.plist names Zenvi's
    bundle id are touched. Returns the paths removed.
    """
    bundle = _running_bundle()
    if not bundle:
        return []
    removed = []
    for suffix in (".zenvi-update-new", ".zenvi-update-old", ".new", ".bak"):
        leftover = bundle + suffix
        plist = os.path.join(leftover, "Contents", "Info.plist")
        try:
            if not os.path.isdir(leftover) or os.path.islink(leftover):
                continue
            with open(plist, "rb") as fh:
                if _MAC_BUNDLE_ID.encode() not in fh.read():
                    continue
            shutil.rmtree(leftover)
            removed.append(leftover)
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# Inno Setup silent flags:
#   /SILENT                — no prompts, but Setup's progress window shows:
#                            the install takes minutes, and with nothing on
#                            screen people launch Zenvi again mid-install
#   /SUPPRESSMSGBOXES      — suppress any message boxes
#   /NORESTART             — don't auto-reboot the machine
#   /CLOSEAPPLICATIONS     — use Restart Manager to close processes holding
#                            files under {app} open, so Setup can replace them
#   /NORESTARTAPPLICATIONS — don't let Inno relaunch whatever it closed; the
#                            external updater script does its own relaunch
#                            below, and letting both happen races two
#                            instances against each other
#   /SP-                   — disable "This will install..." prompt
#
# Deliberately no /CURRENTUSER: forcing non-admin mode would fight an
# existing per-machine install under {autopf} instead of updating it in
# place. Leave install scope exactly as Setup would otherwise choose.
_INNO_SILENT_ARGS = (
    "/SILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/NORESTARTAPPLICATIONS",
    "/SP-",
)

# CREATE_NO_WINDOW avoids a flashed console; CREATE_NEW_PROCESS_GROUP
# separates the helper from this process's process group so it isn't
# affected by however this process exits. Deliberately NOT DETACHED_PROCESS:
# powershell.exe's console host silently fails to run anything when spawned
# with no console at all (verified empirically) — CREATE_NO_WINDOW already
# gives it a hidden console, which is what actually matters here.
_HELPER_CREATIONFLAGS = 0x08000000 | 0x00000200
# Leave any job object Zenvi runs in (an installer's, a launcher's), so the
# helper isn't killed together with Zenvi. Refused when the job forbids it.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _ps_str(value):
    """Render *value* as a single-quoted PowerShell string literal (no
    interpolation, so paths containing $ or backticks can't be misread as
    PowerShell syntax)."""
    return "'" + str(value).replace("'", "''") + "'"


def _build_update_helper_script(filepath, manifest_path, relaunch_target, log_path,
                                parent_pid=None, version="", attempts=1,
                                status_path=None):
    """Return PowerShell source for a detached external updater.

    Why this can't just run inline in this process: Setup is about to
    overwrite {app}\\zenvi.exe (and its DLLs), and /CLOSEAPPLICATIONS uses
    Windows Restart Manager to find and close *any* running process that
    currently has those exact files open for execution. This process IS
    {app}\\zenvi.exe — running from the very file Setup wants to replace —
    so if it blocked here waiting on Setup, Restart Manager could
    legitimately close it mid-wait, orphaning the wait and leaving nobody to
    verify success or relaunch the app. PowerShell holds no handle to
    anything under {app}, so it's invisible to that scan; it can safely wait
    for Setup, then finish the job.

    The app is reopened whatever Setup's outcome: the new version on
    success, the old one (still installed) on failure.
    """
    inno_arg_list = ",".join(_ps_str(a) for a in _INNO_SILENT_ARGS)
    # Only real Inno Setup .exe installers take silent-mode ArgumentList flags.
    # Unit-test stubs use a .bat stand-in; passing the Inno flags can hang
    # Start-Process -Wait on some Windows runners.
    if str(filepath).lower().endswith(".exe"):
        start_process = (
            f"    $p = Start-Process -FilePath {_ps_str(filepath)} "
            f"-ArgumentList {inno_arg_list} -Wait -PassThru -ErrorAction Stop\n"
        )
    else:
        start_process = (
            f"    $p = Start-Process -FilePath {_ps_str(filepath)} "
            f"-Wait -PassThru -ErrorAction Stop\n"
        )
    relaunch_block = ""
    if relaunch_target:
        relaunch_block = (
            "function Reopen {\n"
            f"    if (Test-Path {_ps_str(relaunch_target)}) {{\n"
            f"        Start-Process -FilePath {_ps_str(relaunch_target)}\n"
            f"        Log 'Relaunched app'\n"
            f"    }}\n"
            "}\n"
        )
    else:
        relaunch_block = "function Reopen { }\n"
    expected_exe = _ps_str(relaunch_target) if relaunch_target else "$null"
    wait_parent_block = ""
    if parent_pid:
        # Not Wait-Process: Zenvi may exit before PowerShell starts, and its
        # pid be reused by an unrelated program, which must not be waited on
        # (or ended). A pid whose executable isn't Zenvi's is not Zenvi.
        wait_parent_block = (
            f"Log 'Waiting for parent process {int(parent_pid)} to exit'\n"
            f"$deadline = (Get-Date).AddSeconds({APP_EXIT_TIMEOUT})\n"
            "while ($true) {\n"
            f"    $p = Get-Process -Id {int(parent_pid)} -ErrorAction SilentlyContinue\n"
            "    if (-not $p) { break }\n"
            "    if ($ZenviExe -and $p.Path -and ($p.Path -ne $ZenviExe)) { break }\n"
            "    if ((Get-Date) -gt $deadline) {\n"
            f"        Log 'Zenvi did not quit in {APP_EXIT_TIMEOUT} s; ending it'\n"
            f"        Stop-Process -Id {int(parent_pid)} -Force -ErrorAction SilentlyContinue\n"
            "        Start-Sleep -Seconds 2\n"
            "        break\n"
            "    }\n"
            "    Start-Sleep -Milliseconds 300\n"
            "}\n"
            "Start-Sleep -Milliseconds 400\n"
        )
    other_sessions_block = ""
    if relaunch_target:
        # Another Zenvi from this install (a headless session) would be closed
        # by Setup mid-work: wait for it, else postpone.
        other_sessions_block = (
            f"$deadline = (Get-Date).AddSeconds({OTHER_SESSIONS_TIMEOUT})\n"
            "while (Get-Process -Name zenvi -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Path -eq $ZenviExe }) {\n"
            "    if ((Get-Date) -gt $deadline) {\n"
            "        Log 'Another Zenvi session is still running; update postponed'\n"
            "        Status $false 'Another Zenvi session was still running. Close it, then use Restart to Update.' $true\n"
            "        Reopen\n"
            "        Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue\n"
            "        exit 0\n"
            "    }\n"
            "    Start-Sleep -Milliseconds 500\n"
            "}\n"
        )
    status_block = ""
    if status_path:
        status_block = (
            "function Status($ok, $reason, $postponed = $false) {\n"
            # Postponing isn't a failed attempt.
            f"    $attempts = {int(attempts)}\n"
            "    if ($postponed -and $attempts -gt 0) { $attempts = $attempts - 1 }\n"
            f"    $s = [ordered]@{{ version = {_ps_str(version)}; attempts = $attempts; "
            "ok = $ok; postponed = [bool]$postponed; reason = $reason; "
            "time = [int][double]::Parse((Get-Date -UFormat %s)) }\n"
            f"    $s | ConvertTo-Json | Set-Content -Path {_ps_str(status_path)} "
            "-Encoding UTF8 -ErrorAction SilentlyContinue\n"
            "}\n"
        )
    else:
        status_block = "function Status($ok, $reason, $postponed = $false) { }\n"
    lock_block = ""
    if status_path:
        lock_dir = os.path.join(os.path.dirname(status_path), _LOCK_DIR_NAME)
        lock_block = (
            # One installer helper at a time; the mutex goes with this process.
            "$mutex = New-Object System.Threading.Mutex($false, 'Local\\ZenviUpdateHelper')\n"
            "try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }\n"
            "if (-not $owned) {\n"
            "    Log 'Another update helper is already installing; this one stops'\n"
            "    Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue\n"
            "    exit 0\n"
            "}\n"
            f"New-Item -ItemType Directory -Force -Path {_ps_str(lock_dir)} | Out-Null\n"
            f"Set-Content -Path {_ps_str(os.path.join(lock_dir, 'pid'))} -Value $PID -ErrorAction SilentlyContinue\n"
        )

    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$ZenviExe = {expected_exe}\n"
        "function Log($msg) {\n"
        f"    $line = ('{{0}}  [ZenviUpdater] {{1}}' -f "
        "(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)\n"
        f"    Add-Content -Path {_ps_str(log_path)} -Value $line "
        "-ErrorAction SilentlyContinue\n"
        "}\n"
        f"{status_block}"
        f"{relaunch_block}"
        "\n"
        "Log 'External updater started'\n"
        f"{lock_block}"
        f"{wait_parent_block}"
        f"{other_sessions_block}"
        "try {\n"
        f"{start_process}"
        "    $code = $p.ExitCode\n"
        "} catch {\n"
        "    Log \"Failed to launch installer: $_\"\n"
        "    Status $false 'The installer could not run (was the administrator prompt declined?)'\n"
        "    Reopen\n"
        "    Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue\n"
        "    exit 1\n"
        "}\n"
        "\n"
        "if ($code -eq 0) {\n"
        "    Log 'Installer completed successfully (exit code 0)'\n"
        f"    Remove-Item -Path {_ps_str(manifest_path)} -Force "
        "-ErrorAction SilentlyContinue\n"
        f"    Remove-Item -Path {_ps_str(filepath)} -Force "
        "-ErrorAction SilentlyContinue\n"
        "    Status $true 'installed'\n"
        "} else {\n"
        "    Log \"Installer did not succeed (exit code=$code) - "
        "leaving staged files for retry\"\n"
        "    Status $false \"The installer stopped with exit code $code\"\n"
        "}\n"
        "Reopen\n"
        "\n"
        "Remove-Item -Path $MyInvocation.MyCommand.Path -Force "
        "-ErrorAction SilentlyContinue\n"
    )


_AUTO_PARENT_PID = object()


def _spawn_external_updater(filepath, relaunch_target, parent_pid=_AUTO_PARENT_PID,
                            version="", attempts=1):
    """Write the helper script to the update staging dir and launch it fully
    detached. Returns True once the helper process has been started — at
    that point the staged installer/manifest are no longer this process's
    responsibility to clean up; the helper does that itself once Setup
    actually finishes.

    parent_pid defaults to this process (so Setup waits for zenvi.exe to
    exit). Pass parent_pid=None to skip the wait (unit tests that keep the
    caller alive).
    """
    if parent_pid is _AUTO_PARENT_PID:
        parent_pid = os.getpid()
    script_path = _helper_path(".ps1")
    script = _build_update_helper_script(
        filepath, UPDATE_MANIFEST, relaunch_target, UPDATE_LOG,
        parent_pid=parent_pid, version=version, attempts=attempts,
        status_path=UPDATE_STATUS)

    try:
        os.makedirs(UPDATE_STAGING_DIR, exist_ok=True)
        # With a BOM: Windows PowerShell 5.1 reads a .ps1 without one as the
        # ANSI code page, which garbles paths like C:\\Users\\José.
        with open(script_path, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
    except OSError as exc:
        _log(f"Failed to write updater helper script: {exc}")
        return False

    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-File", script_path]
    env = _clean_env() if os.name == "nt" else None
    last_exc = None
    for flags in (_HELPER_CREATIONFLAGS | _CREATE_BREAKAWAY_FROM_JOB, _HELPER_CREATIONFLAGS):
        try:
            subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=env, cwd=UPDATE_STAGING_DIR)
            return True
        except OSError as exc:
            # A job that forbids breaking away refuses the first form.
            last_exc = exc
            continue
        except Exception as exc:
            last_exc = exc
            break
    _log(f"Failed to launch external updater: {last_exc}")
    return False


def _apply_windows(filepath, filename, version="", wait_pid=_AUTO_PARENT_PID, attempts=1):
    """Hand the staged Inno Setup installer off to a detached external
    updater process and return immediately, so this process — itself a
    running image of {app}\\zenvi.exe, one of the files Setup is about to
    replace — exits cleanly under its own control instead of risking being
    closed mid-wait by /CLOSEAPPLICATIONS. See _build_update_helper_script
    for why the wait has to happen outside this process. The external
    updater records the outcome and reopens the app in every case.
    """
    if not filename.endswith(".exe"):
        _log(f"Unknown Windows package type: {filename}")
        return False

    relaunch_target = None
    if getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
        relaunch_target = sys.executable

    _log(f"Handing off to external updater: {filepath}")
    _show_update_notice("windows", version)
    if not _spawn_external_updater(filepath, relaunch_target, parent_pid=wait_pid,
                                   version=version, attempts=attempts):
        return False

    _log("External updater launched — exiting so /CLOSEAPPLICATIONS cannot "
         "target this process mid-install")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def discard_staged_update(manifest=None):
    """Remove staged installer + manifest without applying (failed/cancelled)."""
    if manifest is None:
        manifest = read_manifest()
    _cleanup(manifest or {})


def _verify_integrity(manifest):
    """SHA-256 verification of the staged file."""
    filepath = manifest.get("filepath", "")
    expected = manifest.get("sha256", "")

    if not os.path.isfile(filepath):
        _log(f"Staged file missing: {filepath}")
        return False

    if not expected:
        _log("No SHA-256 in manifest — refusing to install")
        return False

    sha = hashlib.sha256()
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)

    actual = sha.hexdigest()
    if actual != expected:
        _log(f"SHA-256 mismatch  expected={expected}  actual={actual}")
        return False
    return True


def _cleanup(manifest):
    """Remove staged update files after successful (or discarded) install."""
    # Delete the manifest first so future launches don't re-trigger even if
    # the installer .exe is still locked (e.g. running in background on Windows).
    try:
        if os.path.exists(UPDATE_MANIFEST):
            os.unlink(UPDATE_MANIFEST)
            _log("Manifest cleaned up")
    except Exception as exc:
        _log(f"Manifest cleanup error: {exc}")
    try:
        fp = manifest.get("filepath", "")
        if fp and os.path.exists(fp):
            os.unlink(fp)
            _log("Installer file cleaned up")
    except Exception as exc:
        _log(f"Cleanup error: {exc}")


def cleanup_stale_staging():
    """Delete staged packages that will never be installed: anything the
    manifest doesn't point at (older downloads, interrupted .part files) and
    a staged version that isn't newer than the running one.

    Run off the GUI thread. Returns the paths removed.
    """
    removed = []
    manifest = read_manifest() or {}
    staged = manifest.get("filepath", "")
    running = _running_version()
    if staged and running and manifest.get("version") and \
            not is_version_newer(manifest.get("version"), running):
        _cleanup(manifest)
        removed.append(staged)
        staged = ""
    keep = {os.path.normcase(os.path.abspath(p)) for p in (
        UPDATE_MANIFEST, UPDATE_LOG, UPDATE_STATUS, staged,
        os.path.join(UPDATE_STAGING_DIR, _HELPER_SH_NAME),
        os.path.join(UPDATE_STAGING_DIR, _UPDATE_HELPER_SCRIPT_NAME),
    ) if p}
    try:
        entries = os.listdir(UPDATE_STAGING_DIR)
    except OSError:
        return removed
    for entry in entries:
        path = os.path.join(UPDATE_STAGING_DIR, entry)
        if os.path.normcase(os.path.abspath(path)) in keep or not os.path.isfile(path):
            continue
        if entry.startswith("zenvi_update_helper-"):
            # Each helper deletes its own script; one left a day later is
            # from a helper that was killed.
            try:
                if time.time() - os.path.getmtime(path) < 86400:
                    continue
            except OSError:
                continue
        elif not (entry.startswith("Zenvi") or entry.endswith((".part", ".tmp"))):
            continue
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            continue
    return removed
