#!/usr/bin/env python3
"""End-to-end auto-update test against real installers (CI only).

Installs a Zenvi, gives it an update the way the auto-updater would have
downloaded it, opens it, and checks that the updated Zenvi comes up, stays up,
and is still signed in.

Scenarios:
  old-to-new  Install the previous release, stage the new build, open the
              previous release. Its own (already shipped) updater installs the
              new build: this is the update every existing user is about to get.
  new-to-new  Install the new build and stage it again under a higher version,
              so the new build's own installer helper runs: the update every
              later release will get.

It resets ~/.openshot_qt and replaces the installed Zenvi, so it refuses to
run unless CI=true.

Usage:
  e2e_update_test.py --scenario old-to-new --previous <installer> --previous-version 1.2.1 \\
                     --new <installer> --new-version 1.2.2
  e2e_update_test.py --scenario new-to-new --new <installer> --new-version 1.2.2
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

SYSTEM = platform.system()
HOME = os.environ.get("USERPROFILE") if SYSTEM == "Windows" else os.path.expanduser("~")
USER_DIR = os.path.join(HOME, ".openshot_qt")
UPDATES_DIR = os.path.join(USER_DIR, "updates")
LOG = os.path.join(USER_DIR, "openshot-qt.log")
AUTH_FILE = os.path.join(USER_DIR, "zenvi_auth.json")

MAC_APP = "/Applications/Zenvi.app"
LINUX_APPIMAGE = os.path.join(HOME, "Applications", "Zenvi.AppImage")
WINDOWS_APP_ID = "{4BB0DCDC-BC24-49EC-8937-72956C33A470}_is1"

# A bumped version for staging the new build again (new-to-new).
NEWER_VERSION = "99.0.0"

STARTUP_TIMEOUT = 420     # first launch of a large app on a CI machine
STABLE_FOR = 45           # how long the updated Zenvi must stay up


class Failure(Exception):
    pass


def say(msg):
    print(f"[e2e] {msg}", flush=True)


# ── Profile ──────────────────────────────────────────────────────────────────

def reset_profile():
    shutil.rmtree(USER_DIR, ignore_errors=True)
    os.makedirs(UPDATES_DIR, exist_ok=True)


def write_session():
    """A signed-in session whose token is valid for years, so no Zenvi
    version needs the network (or a browser) to accept it."""
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    claims = {"sub": "00000000-0000-4000-8000-000000000e2e", "email": "e2e@zenvi.invalid",
              "aud": "authenticated", "exp": int(time.time()) + 10 * 365 * 86400}
    session = {
        "access_token": f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.e2e",
        "refresh_token": "e2e-refresh-token",
        "user_id": claims["sub"],
        "user_email": claims["email"],
    }
    with open(AUTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(session, fh)
    return session


def stage(package, version):
    """Leave *package* where the auto-updater puts a finished download."""
    dest = os.path.join(UPDATES_DIR, os.path.basename(package))
    shutil.copy2(package, dest)
    sha = hashlib.sha256()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    manifest = {"version": version, "filename": os.path.basename(dest), "filepath": dest,
                "sha256": sha.hexdigest(), "size": os.path.getsize(dest),
                "platform": SYSTEM.lower(), "downloaded_at": time.time()}
    with open(os.path.join(UPDATES_DIR, "update_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    say(f"staged {os.path.basename(dest)} as version {version}")


def read_log(offset=0):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return ""


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


# ── Installing and running Zenvi, per platform ───────────────────────────────

def install(package):
    say(f"installing {os.path.basename(package)}")
    if SYSTEM == "Darwin":
        mount = tempfile.mkdtemp(prefix="zenvi_e2e_")
        subprocess.run(["hdiutil", "attach", package, "-mountpoint", mount, "-nobrowse",
                        "-noautoopen", "-readonly", "-quiet"], check=True, timeout=600)
        try:
            shutil.rmtree(MAC_APP, ignore_errors=True)
            subprocess.run(["ditto", os.path.join(mount, "Zenvi.app"), MAC_APP], check=True, timeout=900)
        finally:
            subprocess.run(["hdiutil", "detach", mount, "-quiet", "-force"], timeout=120)
    elif SYSTEM == "Windows":
        # /NORESTARTAPPLICATIONS: otherwise the installer opens Zenvi after a
        # silent install (for 1.0.188's updater), before the test is ready.
        subprocess.run([package, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                        "/NORESTARTAPPLICATIONS", "/SP-"], check=True, timeout=1200)
    else:
        os.makedirs(os.path.dirname(LINUX_APPIMAGE), exist_ok=True)
        shutil.copy2(package, LINUX_APPIMAGE)
        os.chmod(LINUX_APPIMAGE, 0o755)


def windows_exe():
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" + WINDOWS_APP_ID)
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            exe = os.path.join(location, "zenvi.exe")
            if os.path.isfile(exe):
                return exe
        except OSError:
            continue
    for guess in (r"C:\Program Files\Zenvi\zenvi.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Zenvi\zenvi.exe")):
        if os.path.isfile(guess):
            return guess
    raise Failure("Zenvi is not installed (no zenvi.exe found)")


def launch():
    say("opening Zenvi")
    if SYSTEM == "Darwin":
        subprocess.run(["open", MAC_APP], check=True, timeout=60)
    elif SYSTEM == "Windows":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([windows_exe()], creationflags=flags, close_fds=True)
    else:
        subprocess.Popen([LINUX_APPIMAGE], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def zenvi_pids():
    if SYSTEM == "Windows":
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zenvi.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        return [line.split('","')[1] for line in out.splitlines() if line.startswith('"zenvi.exe"')]
    pattern = f"{MAC_APP}/Contents/MacOS/zenvi" if SYSTEM == "Darwin" else "Zenvi.AppImage|/usr/bin/zenvi|mount_Zenvi"
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return out.split()


def quit_zenvi():
    say("closing Zenvi")
    if SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", 'tell application "Zenvi" to quit'], timeout=60)
    elif SYSTEM == "Windows":
        subprocess.run(["taskkill", "/IM", "zenvi.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-TERM", "-f", "Zenvi.AppImage"], capture_output=True)
    deadline = time.time() + 60
    while zenvi_pids() and time.time() < deadline:
        time.sleep(1)
    if zenvi_pids():
        if SYSTEM == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "zenvi.exe"], capture_output=True)
        else:
            subprocess.run(["pkill", "-KILL", "-f", "Zenvi.AppImage|Zenvi.app/Contents/MacOS"],
                           capture_output=True)
        time.sleep(3)


def crash_reports_since(started):
    """Native crash reports written since *started* (macOS and Windows)."""
    found = []
    if SYSTEM == "Darwin":
        for folder in (os.path.expanduser("~/Library/Logs/DiagnosticReports"),
                       "/Library/Logs/DiagnosticReports"):
            for path in glob.glob(os.path.join(folder, "*")):
                name = os.path.basename(path).lower()
                if ("zenvi" in name or "python" in name or "launch-mac" in name) and \
                        os.path.getmtime(path) >= started:
                    found.append(path)
    elif SYSTEM == "Windows":
        for path in glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps\zenvi.exe*")):
            if os.path.getmtime(path) >= started:
                found.append(path)
    fault_log = os.path.join(USER_DIR, "faulthandler.log")
    if os.path.exists(fault_log) and os.path.getsize(fault_log) and os.path.getmtime(fault_log) >= started:
        found.append(fault_log)
    return found


# ── Checks ───────────────────────────────────────────────────────────────────

def wait_until_ready(version, offset, started):
    """Wait for a Zenvi *version* session to log that its window is up."""
    marker = f"Zenvi {version} is ready"
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        text = read_log(offset)
        if marker in text:
            say(f"Zenvi {version} is up")
            return
        if "Displaying" in text and "startup messages" in text:
            raise Failure("Zenvi showed startup error messages:\n" + text[-4000:])
        crashes = crash_reports_since(started)
        if crashes:
            raise Failure(f"Zenvi crashed: {crashes}")
        time.sleep(2)
    raise Failure(f"Zenvi {version} did not come up within {STARTUP_TIMEOUT} s. Log tail:\n"
                  + read_log(offset)[-4000:])


def check_stays_up(offset, started):
    deadline = time.time() + STABLE_FOR
    while time.time() < deadline:
        if not zenvi_pids():
            raise Failure("Zenvi quit by itself after updating. Log tail:\n" + read_log(offset)[-4000:])
        crashes = crash_reports_since(started)
        if crashes:
            raise Failure(f"Zenvi crashed after updating: {crashes}")
        time.sleep(3)
    say(f"Zenvi stayed up for {STABLE_FOR} s")


def check_still_signed_in(session, offset):
    try:
        with open(AUTH_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Failure(f"the sign-in was lost in the update: {exc}")
    if stored.get("refresh_token") != session["refresh_token"]:
        raise Failure("the sign-in was replaced during the update")
    if "Opening auth URL" in read_log(offset):
        raise Failure("Zenvi opened the browser sign-in after updating")
    say("still signed in, and no browser sign-in was opened")


def check_update_finished():
    manifest = os.path.join(UPDATES_DIR, "update_manifest.json")
    if os.path.exists(manifest):
        raise Failure("the staged update is still pending after the update")


# ── Scenarios ────────────────────────────────────────────────────────────────

def old_to_new(args):
    reset_profile()
    install(args.previous)
    session = write_session()
    stage(args.new, args.new_version)
    started, offset = time.time(), log_size()
    launch()
    wait_until_ready(args.new_version, offset, started)
    check_stays_up(offset, started)
    check_still_signed_in(session, offset)
    check_update_finished()
    quit_zenvi()


def new_to_new(args):
    reset_profile()
    install(args.new)
    session = write_session()
    stage(args.new, NEWER_VERSION)
    started, offset = time.time(), log_size()
    launch()
    deadline = time.time() + STARTUP_TIMEOUT
    status_path = os.path.join(UPDATES_DIR, "update_status.json")
    while time.time() < deadline:
        try:
            with open(status_path, encoding="utf-8-sig") as fh:
                status = json.load(fh)
        except (OSError, ValueError):
            status = {}
        if status.get("ok") is True:
            say("the installer helper reports the update installed")
            break
        if status.get("ok") is False:
            raise Failure(f"the installer helper failed: {status.get('reason')}\n"
                          + read_text(os.path.join(UPDATES_DIR, "install.log")))
        time.sleep(2)
    else:
        raise Failure("the installer helper never finished:\n"
                      + read_text(os.path.join(UPDATES_DIR, "install.log")))
    wait_until_ready(args.new_version, offset, started)
    check_stays_up(offset, started)
    check_still_signed_in(session, offset)
    check_update_finished()
    quit_zenvi()


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()[-6000:]
    except OSError:
        return f"({path} not found)"


def collect(dest):
    """Copy logs and crash reports into *dest* for the CI artifact."""
    os.makedirs(dest, exist_ok=True)
    for path in (LOG, os.path.join(UPDATES_DIR, "install.log"),
                 os.path.join(UPDATES_DIR, "update_status.json"),
                 os.path.join(USER_DIR, "faulthandler.log")):
        if os.path.exists(path):
            shutil.copy2(path, dest)
    for path in crash_reports_since(0):
        shutil.copy2(path, dest)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zenvi auto-update end-to-end test")
    parser.add_argument("--scenario", choices=("old-to-new", "new-to-new"), required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--previous")
    parser.add_argument("--previous-version")
    parser.add_argument("--artifacts", default="e2e-artifacts")
    args = parser.parse_args(argv)
    if os.environ.get("CI") != "true":
        print("This test resets ~/.openshot_qt and replaces the installed Zenvi; it only runs on CI.")
        return 2
    if args.scenario == "old-to-new" and not (args.previous and args.previous_version):
        parser.error("old-to-new needs --previous and --previous-version")

    try:
        (old_to_new if args.scenario == "old-to-new" else new_to_new)(args)
    except Failure as exc:
        print(f"::error::{args.scenario}: {exc}", flush=True)
        collect(os.path.join(args.artifacts, args.scenario))
        quit_zenvi()
        return 1
    collect(os.path.join(args.artifacts, args.scenario))
    say(f"{args.scenario}: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
