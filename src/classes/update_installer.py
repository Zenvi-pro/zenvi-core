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
        if system == "windows":
            # _apply_windows only confirms hand-off to the external updater,
            # not that Setup itself succeeded — that process still needs the
            # staged manifest/installer and cleans them up itself once it
            # knows the real outcome.
            _log("Update handed off to external updater")
        else:
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

# Inno Setup silent flags:
#   /VERYSILENT            — no user prompts at all
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
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/NORESTARTAPPLICATIONS",
    "/SP-",
)

_UPDATE_HELPER_SCRIPT_NAME = "zenvi_update_helper.ps1"

# CREATE_NO_WINDOW avoids a flashed console; CREATE_NEW_PROCESS_GROUP
# separates the helper from this process's process group so it isn't
# affected by however this process exits. Deliberately NOT DETACHED_PROCESS:
# powershell.exe's console host silently fails to run anything when spawned
# with no console at all (verified empirically) — CREATE_NO_WINDOW already
# gives it a hidden console, which is what actually matters here.
_HELPER_CREATIONFLAGS = 0x08000000 | 0x00000200


def _ps_str(value):
    """Render *value* as a single-quoted PowerShell string literal (no
    interpolation, so paths containing $ or backticks can't be misread as
    PowerShell syntax)."""
    return "'" + str(value).replace("'", "''") + "'"


def _build_update_helper_script(filepath, manifest_path, relaunch_target, log_path):
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
    """
    inno_arg_list = ",".join(_ps_str(a) for a in _INNO_SILENT_ARGS)
    relaunch_block = ""
    if relaunch_target:
        relaunch_block = (
            f"    if (Test-Path {_ps_str(relaunch_target)}) {{\n"
            f"        Start-Process -FilePath {_ps_str(relaunch_target)}\n"
            f"        Log 'Relaunched app'\n"
            f"    }}\n"
        )

    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "function Log($msg) {\n"
        f"    $line = ('{{0}}  [ZenviUpdater] {{1}}' -f "
        "(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)\n"
        f"    Add-Content -Path {_ps_str(log_path)} -Value $line "
        "-ErrorAction SilentlyContinue\n"
        "}\n"
        "\n"
        "Log 'External updater started'\n"
        "try {\n"
        f"    $p = Start-Process -FilePath {_ps_str(filepath)} "
        f"-ArgumentList {inno_arg_list} -Wait -PassThru -ErrorAction Stop\n"
        "    $code = $p.ExitCode\n"
        "} catch {\n"
        "    Log \"Failed to launch installer: $_\"\n"
        "    exit 1\n"
        "}\n"
        "\n"
        "if ($code -eq 0) {\n"
        "    Log 'Installer completed successfully (exit code 0)'\n"
        f"    Remove-Item -Path {_ps_str(manifest_path)} -Force "
        "-ErrorAction SilentlyContinue\n"
        f"    Remove-Item -Path {_ps_str(filepath)} -Force "
        "-ErrorAction SilentlyContinue\n"
        f"{relaunch_block}"
        "} else {\n"
        "    Log \"Installer did not succeed (exit code=$code) - "
        "leaving staged files for retry\"\n"
        "}\n"
        "\n"
        "Remove-Item -Path $MyInvocation.MyCommand.Path -Force "
        "-ErrorAction SilentlyContinue\n"
    )


def _spawn_external_updater(filepath, relaunch_target):
    """Write the helper script to the update staging dir and launch it fully
    detached. Returns True once the helper process has been started — at
    that point the staged installer/manifest are no longer this process's
    responsibility to clean up; the helper does that itself once Setup
    actually finishes."""
    script_path = os.path.join(UPDATE_STAGING_DIR, _UPDATE_HELPER_SCRIPT_NAME)
    script = _build_update_helper_script(
        filepath, UPDATE_MANIFEST, relaunch_target, UPDATE_LOG)

    try:
        os.makedirs(UPDATE_STAGING_DIR, exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
    except OSError as exc:
        _log(f"Failed to write updater helper script: {exc}")
        return False

    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", script_path],
            creationflags=_HELPER_CREATIONFLAGS,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"Failed to launch external updater: {exc}")
        return False

    return True


def _apply_windows(filepath, filename):
    """Hand the staged Inno Setup installer off to a detached external
    updater process and return immediately, so this process — itself a
    running image of {app}\\zenvi.exe, one of the files Setup is about to
    replace — exits cleanly under its own control instead of risking being
    closed mid-wait by /CLOSEAPPLICATIONS. See _build_update_helper_script
    for why the wait has to happen outside this process. The external
    updater verifies a confirmed zero exit code before cleaning up the
    staged files, and relaunches the app itself afterward since Inno's own
    postinstall launch is skipped in silent mode (windows-installer.iss
    [Run] has Flags: ... skipifsilent).
    """
    if not filename.endswith(".exe"):
        _log(f"Unknown Windows package type: {filename}")
        return False

    relaunch_target = None
    if getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
        relaunch_target = sys.executable

    _log(f"Handing off to external updater: {filepath}")
    if not _spawn_external_updater(filepath, relaunch_target):
        return False

    _log("External updater launched — exiting so /CLOSEAPPLICATIONS cannot "
         "target this process mid-install")
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
