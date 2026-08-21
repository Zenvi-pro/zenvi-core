"""
 @file
 @brief Pre-launch update installer.  Called from launch.py *before* any
        heavy imports (PyQt5, openshot, etc.) to apply a previously downloaded
        update that was staged by the AutoUpdater background thread.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 NOTE: This module intentionally avoids importing PyQt5 or any heavy
 dependency so it can run quickly at the very start of the process.
"""

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time


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


# ---------------------------------------------------------------------------
# Version comparison (shared with auto_updater)
# ---------------------------------------------------------------------------

def parse_version(version_str):
    """Parse '3.4.1' or 'v3.4.1' into a comparable tuple."""
    try:
        clean = (version_str or "").strip().lstrip("v")
        return tuple(int(x) for x in clean.split("."))
    except (ValueError, AttributeError):
        return (0,)


def is_version_newer(remote_version, local_version):
    """Return True when remote_version is strictly newer than local_version."""
    return parse_version(remote_version) > parse_version(local_version)


# ---------------------------------------------------------------------------
# Logging (minimal — no dependency on classes.logger)
# ---------------------------------------------------------------------------

def _log(msg):
    line = f"[ZenviUpdater] {msg}"
    print(line)
    try:
        with open(UPDATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API — called from launch.py
# ---------------------------------------------------------------------------

def has_pending_update():
    """Return True if a verified update package is staged and ready to install."""
    if not os.path.exists(UPDATE_MANIFEST):
        return False
    try:
        with open(UPDATE_MANIFEST, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        filepath = manifest.get("filepath", "")
        if not (bool(filepath) and os.path.isfile(filepath)):
            return False
        # Skip update if staged version is not newer than the running version
        staged_ver = manifest.get("version", "")
        if staged_ver:
            try:
                from classes import info as _info
                if not is_version_newer(staged_ver, _info.VERSION):
                    return False
            except Exception:
                pass
        return True
    except Exception:
        return False


def apply_pending_update():
    """Try to install a staged update.

    Returns
    -------
    bool
        True  — update applied; the caller should restart the process.
        False — nothing was applied (missing, corrupt, or unsupported).
    """
    manifest = _read_manifest()
    if manifest is None:
        return False

    _log(f"Applying staged update  version={manifest.get('version')}  "
         f"file={manifest.get('filename')}")

    if not _verify_integrity(manifest):
        _log("Integrity check FAILED — discarding staged update")
        _cleanup(manifest)
        return False

    system = platform.system().lower()
    filepath = manifest.get("filepath", "")
    filename = manifest.get("filename", "")

    _show_update_notice(system, manifest.get("version", ""))

    try:
        if system == "linux":
            ok = _apply_linux(filepath, filename)
        elif system == "darwin":
            ok = _apply_macos(filepath, filename)
        elif system == "windows":
            ok = _apply_windows(filepath, filename)
        else:
            _log(f"Unsupported platform: {system}")
            ok = False
    except Exception as exc:
        _log(f"Installation error: {exc}")
        ok = False

    if ok:
        _log("Update applied successfully")
        _cleanup(manifest)
    else:
        _log("Update could not be applied — keeping staged files for retry")

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
    except Exception:
        pass


def _relaunch(cmd):
    """Best-effort launch of the freshly-installed app so the user isn't left
    staring at nothing after the update silently applies and this process
    exits. Never raises — a failure here just means the user has to open the
    app again themselves, same as before this helper existed."""
    try:
        subprocess.Popen(cmd)
        _log(f"Relaunched: {' '.join(cmd)}")
    except Exception as exc:
        _log(f"Failed to relaunch after update: {exc}")


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _apply_linux(filepath, filename):
    if filename.endswith(".AppImage"):
        return _apply_appimage(filepath)
    if filename.endswith(".deb"):
        return _apply_deb(filepath)
    _log(f"Unknown Linux package type: {filename}")
    return False


def _apply_appimage(filepath):
    """Replace the running AppImage binary with the new one."""
    current = os.environ.get("APPIMAGE") or _find_current_appimage()

    if not current:
        # Fallback: place in ~/Applications/
        current = os.path.expanduser("~/Applications/Zenvi.AppImage")
        os.makedirs(os.path.dirname(current), exist_ok=True)
        _log(f"No existing AppImage detected — installing to {current}")

    _log(f"Replacing AppImage  {current}")

    backup = current + ".bak"
    try:
        if os.path.exists(current):
            shutil.copy2(current, backup)

        shutil.copy2(filepath, current)
        # Ensure executable
        st = os.stat(current)
        os.chmod(current, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Remove backup on success
        if os.path.exists(backup):
            os.unlink(backup)

        _log("AppImage replaced successfully")
        _relaunch([current])
        return True

    except Exception as exc:
        _log(f"AppImage replacement failed: {exc}")
        if os.path.exists(backup):
            _log("Restoring backup")
            shutil.move(backup, current)
        return False


def _apply_deb(filepath):
    """Install a .deb package (requires elevated privileges)."""
    _log(f"Installing deb: {filepath}")

    # Try pkexec (graphical polkit prompt — zero terminal friction)
    for tool in ("pkexec", "sudo"):
        try:
            result = subprocess.run(
                [tool, "dpkg", "-i", filepath],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                _log(f"deb installed via {tool}")
                return True
            _log(f"{tool} dpkg returned {result.returncode}: {result.stderr.strip()}")
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            _log(f"{tool} dpkg timed out")
            continue

    _log("Could not install .deb (no pkexec/sudo available or permission denied)")
    return False


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

def _apply_macos(filepath, filename):
    """Mount a .dmg, copy the .app bundle to /Applications."""
    if not filename.endswith(".dmg"):
        _log(f"Unknown macOS package type: {filename}")
        return False

    mount_point = tempfile.mkdtemp(prefix="zenvi_update_")
    _log(f"Mounting DMG at {mount_point}")

    try:
        # Mount
        res = subprocess.run(
            ["hdiutil", "attach", filepath,
             "-mountpoint", mount_point,
             "-nobrowse", "-quiet"],
            capture_output=True, text=True, timeout=120,
        )
        if res.returncode != 0:
            _log(f"hdiutil attach failed: {res.stderr.strip()}")
            return False

        # Locate .app bundle
        app_bundle = None
        for entry in os.listdir(mount_point):
            if entry.endswith(".app"):
                app_bundle = os.path.join(mount_point, entry)
                break

        if not app_bundle:
            _log("No .app bundle found inside DMG")
            return False

        dest = os.path.join("/Applications", os.path.basename(app_bundle))

        # Remove old and copy new
        if os.path.exists(dest):
            _log(f"Removing old installation at {dest}")
            shutil.rmtree(dest)

        _log(f"Copying {app_bundle} → {dest}")
        shutil.copytree(app_bundle, dest)

        _log("macOS update installed")
        _relaunch(["open", "-n", dest])
        return True

    except Exception as exc:
        _log(f"macOS install error: {exc}")
        return False

    finally:
        # Always unmount
        try:
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet"],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
        try:
            os.rmdir(mount_point)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# Generous for a silent install of a large frozen build; this call runs in the
# pre-QApplication bootstrap process (see launch.py) so blocking here is
# invisible — no GUI exists yet to freeze.
_INNO_TIMEOUT_SECS = 120


def _run_and_wait(cmd, timeout):
    """Launch *cmd*, block until it exits or *timeout* elapses.

    Returns (ok, returncode): ok is True only for a confirmed zero exit code.
    Factored out of _apply_windows so the wait/timeout/returncode logic is
    testable with any stub executable — no real Inno installer required.
    """
    try:
        proc = subprocess.Popen(cmd)
    except Exception as exc:
        _log(f"Failed to launch {cmd[0]}: {exc}")
        return False, None

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"Process did not finish within {timeout}s (pid={proc.pid}) — "
             f"leaving it running in the background")
        return False, None

    return (rc == 0), rc


def _apply_windows(filepath, filename):
    """Run the staged Inno Setup installer in fully silent mode, BLOCKING
    until it finishes, and verify it actually succeeded before reporting
    success. Only reports success on a confirmed zero exit code; relaunches
    the app itself afterward, since Inno's own postinstall launch is skipped
    in silent mode (windows-installer.iss [Run] has Flags: ... skipifsilent).
    """
    if not filename.endswith(".exe"):
        _log(f"Unknown Windows package type: {filename}")
        return False

    _log(f"Launching silent installer: {filepath}")
    # Inno Setup silent flags:
    #   /VERYSILENT           — no user prompts at all
    #   /SUPPRESSMSGBOXES     — suppress any message boxes
    #   /NORESTART            — don't auto-reboot the machine
    #   /CLOSEAPPLICATIONS    — close running Zenvi instances (requires the
    #                           AppMutex directive in windows-installer.iss;
    #                           otherwise this flag is a documented no-op)
    #   /NORESTARTAPPLICATIONS — don't let Inno relaunch whatever it closed;
    #                           we do our own relaunch below, and letting both
    #                           happen races two instances against each other
    #   /SP-                  — disable "This will install..." prompt
    ok, rc = _run_and_wait(
        [filepath,
         "/VERYSILENT",
         "/SUPPRESSMSGBOXES",
         "/NORESTART",
         "/CLOSEAPPLICATIONS",
         "/NORESTARTAPPLICATIONS",
         "/CURRENTUSER",
         "/SP-"],
        timeout=_INNO_TIMEOUT_SECS,
    )
    if not ok:
        _log(f"Windows installer did not succeed (exit code={rc}) — "
             f"keeping staged files for retry")
        return False

    _log("Windows installer completed successfully (exit code 0)")

    if getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
        _relaunch([sys.executable])
    else:
        _log("Not a frozen build (or exe missing) — skipping post-install relaunch")

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_manifest():
    """Read and return the update manifest dict, or None."""
    try:
        with open(UPDATE_MANIFEST, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def discard_staged_update(manifest=None):
    """Remove staged installer + manifest without applying (failed/cancelled)."""
    if manifest is None:
        manifest = read_manifest()
    _cleanup(manifest or {})


def _read_manifest():
    return read_manifest()


def _verify_integrity(manifest):
    """SHA-256 verification of the staged file."""
    filepath = manifest.get("filepath", "")
    expected = manifest.get("sha256", "")

    if not os.path.isfile(filepath):
        _log(f"Staged file missing: {filepath}")
        return False

    if not expected:
        _log("No SHA-256 in manifest — skipping integrity check")
        return True  # can't verify, proceed anyway

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
