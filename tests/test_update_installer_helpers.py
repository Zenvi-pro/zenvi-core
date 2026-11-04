"""The update helpers install only after Zenvi quits, roll back, and always reopen Zenvi.

These run the real /bin/sh helpers against stand-ins: a fake Zenvi.app inside
a real disk image (macOS), a fake AppImage, and a sleeping process playing
the Zenvi that asked for the update. Replacing the app from inside the
running app is what used to crash or strand users after an auto-update.
"""

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import textwrap
import time

import pytest

from classes import update_installer as ui

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell helpers")
needs_hdiutil = pytest.mark.skipif(shutil.which("hdiutil") is None, reason="macOS disk images")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    stage = tmp_path / "updates"
    stage.mkdir()
    monkeypatch.setattr(ui, "UPDATE_STAGING_DIR", str(stage))
    monkeypatch.setattr(ui, "UPDATE_MANIFEST", str(stage / "update_manifest.json"))
    monkeypatch.setattr(ui, "UPDATE_LOG", str(stage / "install.log"))
    monkeypatch.setattr(ui, "UPDATE_STATUS", str(stage / "update_status.json"))
    monkeypatch.setattr(ui, "APP_EXIT_TIMEOUT", 20)
    monkeypatch.setattr(ui, "OTHER_SESSIONS_TIMEOUT", 1)
    monkeypatch.setattr(ui, "_show_update_notice", lambda *a, **k: None)
    return stage


def _write_manifest(stage, package, version="9.9.9"):
    data = package.read_bytes()
    manifest = {"version": version, "filename": package.name, "filepath": str(package),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    (stage / "update_manifest.json").write_text(json.dumps(manifest))
    return manifest


def _make_app(path, version, bundle_id=ui._MAC_BUNDLE_ID):
    macos = path / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "launch-mac",
                       "CFBundleShortVersionString": version}, fh)
    exe = macos / "launch-mac"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    # A symlink like the real bundle's Contents/MacOS/lib -> ../Resources/lib
    (path / "Contents" / "Resources" / "lib").mkdir(parents=True)
    os.symlink("../Resources/lib", macos / "lib")
    return path


def _version_of(app):
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        return plistlib.load(fh)["CFBundleShortVersionString"]


def _make_dmg(tmp_path, app_version, bundle_id=ui._MAC_BUNDLE_ID):
    src = tmp_path / "dmg-src"
    src.mkdir()
    _make_app(src / "Zenvi.app", app_version, bundle_id)
    dmg = tmp_path / "Zenvi-v9.9.9-arm64.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", str(src),
                    "-volname", "Zenvi", str(dmg)], check=True, timeout=120)
    return dmg


def _fake_opener(tmp_path):
    """Stands in for /usr/bin/open: records what it was asked to open."""
    marker = tmp_path / "opened.txt"
    opener = tmp_path / "fake-open"
    opener.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\n')
    opener.chmod(0o755)
    return opener, marker


def _zenvi_stand_in(seconds):
    """The Zenvi process that asked for the update (exits after *seconds*)."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _wait_for(predicate, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _status(stage):
    try:
        return json.loads((stage / "update_status.json").read_text())
    except (OSError, ValueError):
        return {}


def _helper_done(stage):
    # Each helper deletes its own script, and the lock, as its last steps.
    return (_status(stage).get("ok") in (True, False)
            and not any(p.name.startswith("zenvi_update_helper-") for p in stage.iterdir())
            and not (stage / ".install-lock").exists())


# ── macOS ────────────────────────────────────────────────────────────────────

@posix_only
@needs_hdiutil
def test_macos_update_waits_for_zenvi_to_quit_then_swaps_and_reopens(tmp_path, staging, monkeypatch):
    apps = tmp_path / "Applications"
    apps.mkdir()
    _make_app(apps / "Zenvi.app", "1.0.0")
    dmg = _make_dmg(tmp_path, "9.9.9")
    _write_manifest(staging, dmg)
    monkeypatch.setenv("ZENVI_UPDATE_APPLICATIONS_DIR", str(apps))
    opener, opened = _fake_opener(tmp_path)

    zenvi = _zenvi_stand_in(2)
    attempts = ui._record_attempt("9.9.9")
    assert ui._apply_macos(str(dmg), dmg.name, "9.9.9", zenvi.pid, attempts, open_cmd=str(opener))

    # Nothing is touched while Zenvi is still running.
    time.sleep(1)
    assert _version_of(apps / "Zenvi.app") == "1.0.0"
    zenvi.wait()

    assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
    assert _status(staging)["ok"] is True
    assert _version_of(apps / "Zenvi.app") == "9.9.9"
    assert os.path.islink(apps / "Zenvi.app" / "Contents" / "MacOS" / "lib")
    assert sorted(p.name for p in apps.iterdir()) == ["Zenvi.app"]
    assert opened.read_text().strip() == str(apps / "Zenvi.app")
    assert not dmg.exists() and not (staging / "update_manifest.json").exists()
    assert not any(p.name.startswith("zenvi_update") for p in tmp_path.iterdir() if p.is_dir())


@posix_only
@needs_hdiutil
def test_macos_update_keeps_the_old_app_when_the_image_has_no_zenvi(tmp_path, staging, monkeypatch):
    apps = tmp_path / "Applications"
    apps.mkdir()
    _make_app(apps / "Zenvi.app", "1.0.0")
    dmg = _make_dmg(tmp_path, "9.9.9", bundle_id="com.example.other")
    _write_manifest(staging, dmg)
    monkeypatch.setenv("ZENVI_UPDATE_APPLICATIONS_DIR", str(apps))
    opener, opened = _fake_opener(tmp_path)

    zenvi = _zenvi_stand_in(0.5)
    assert ui._apply_macos(str(dmg), dmg.name, "9.9.9", zenvi.pid, 1, open_cmd=str(opener))
    zenvi.wait()

    assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
    status = _status(staging)
    assert status["ok"] is False and "no Zenvi app" in status["reason"]
    assert _version_of(apps / "Zenvi.app") == "1.0.0"
    # The user gets their (old) Zenvi back, and the update stays for a retry.
    assert opened.read_text().strip() == str(apps / "Zenvi.app")
    assert dmg.exists()


@posix_only
@needs_hdiutil
def test_macos_update_waits_for_another_session_and_postpones(tmp_path, staging, monkeypatch):
    # Regex characters in the path must not hide the other session.
    apps = tmp_path / "My Apps (old)+1"
    apps.mkdir()
    app = _make_app(apps / "Zenvi.app", "1.0.0")
    # A headless Zenvi session running from the same bundle (as ps and pgrep
    # see it: macOS kills a copied system binary, so set argv[0] instead).
    session = subprocess.Popen([str(app / "Contents" / "MacOS" / "zenvi"), "30"],
                               executable="/bin/sleep")
    try:
        dmg = _make_dmg(tmp_path, "9.9.9")
        _write_manifest(staging, dmg)
        monkeypatch.setenv("ZENVI_UPDATE_APPLICATIONS_DIR", str(apps))
        opener, opened = _fake_opener(tmp_path)

        zenvi = _zenvi_stand_in(0.5)
        ui._record_attempt("9.9.9")
        assert ui._apply_macos(str(dmg), dmg.name, "9.9.9", zenvi.pid, 1, open_cmd=str(opener))
        zenvi.wait()

        assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
        assert _status(staging)["ok"] is False
        assert "Another Zenvi session" in _status(staging)["reason"]
        # Postponing isn't a failed attempt.
        assert _status(staging)["postponed"] is True and _status(staging)["attempts"] == 0
        assert _version_of(app) == "1.0.0"
        assert session.poll() is None, "the other session must not be touched"
    finally:
        session.kill()


# ── Linux AppImage (plain sh + coreutils; runs on macOS too) ────────────────

def _fake_appimage(path, version, marker):
    path.write_text(f'#!/bin/sh\necho "{version}" >> "{marker}"\n')
    path.chmod(0o755)
    return path


@posix_only
def test_appimage_update_replaces_the_file_after_zenvi_quits_and_restarts_it(tmp_path, staging, monkeypatch):
    marker = tmp_path / "ran.txt"
    installed = _fake_appimage(tmp_path / "Zenvi.AppImage", "1.0.0", marker)
    new = _fake_appimage(staging / "Zenvi-v9.9.9-x86_64.AppImage", "9.9.9", marker)
    _write_manifest(staging, new)
    monkeypatch.setenv("APPIMAGE", str(installed))

    zenvi = _zenvi_stand_in(1.5)
    assert ui._apply_appimage(str(new), "9.9.9", zenvi.pid, 1)
    time.sleep(0.5)
    assert "1.0.0" in installed.read_text()
    zenvi.wait()

    assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
    assert _status(staging)["ok"] is True
    assert "9.9.9" in installed.read_text()
    assert os.stat(installed).st_mode & stat.S_IXUSR
    assert _wait_for(lambda: marker.exists() and "9.9.9" in marker.read_text(), 10)
    assert not new.exists() and not (staging / "update_manifest.json").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".Zenvi")] == []


@posix_only
def test_appimage_update_into_a_read_only_folder_restarts_the_old_version(tmp_path, staging, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores folder permissions")
    marker = tmp_path / "ran.txt"
    folder = tmp_path / "locked"
    folder.mkdir()
    installed = _fake_appimage(folder / "Zenvi.AppImage", "1.0.0", marker)
    folder.chmod(0o555)
    try:
        new = _fake_appimage(staging / "Zenvi-v9.9.9-x86_64.AppImage", "9.9.9", marker)
        _write_manifest(staging, new)
        monkeypatch.setenv("APPIMAGE", str(installed))

        zenvi = _zenvi_stand_in(0.3)
        assert ui._apply_appimage(str(new), "9.9.9", zenvi.pid, 1)
        zenvi.wait()

        assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
        assert _status(staging)["ok"] is False
        assert "1.0.0" in installed.read_text()
        assert _wait_for(lambda: marker.exists() and "1.0.0" in marker.read_text(), 10)
        assert new.exists()
    finally:
        folder.chmod(0o755)


@posix_only
def test_a_zenvi_that_hangs_on_quit_is_ended_so_the_update_still_lands(tmp_path, staging, monkeypatch):
    marker = tmp_path / "ran.txt"
    installed = _fake_appimage(tmp_path / "Zenvi.AppImage", "1.0.0", marker)
    new = _fake_appimage(staging / "Zenvi-v9.9.9-x86_64.AppImage", "9.9.9", marker)
    _write_manifest(staging, new)
    monkeypatch.setenv("APPIMAGE", str(installed))
    monkeypatch.setattr(ui, "APP_EXIT_TIMEOUT", 1)

    hung = _zenvi_stand_in(600)
    try:
        assert ui._apply_appimage(str(new), "9.9.9", hung.pid, 1)
        assert _wait_for(lambda: _helper_done(staging), 30), (staging / "install.log").read_text()
        assert hung.poll() is not None
        assert "9.9.9" in installed.read_text()
    finally:
        hung.kill()


# ── The helpers' environment ─────────────────────────────────────────────────

@posix_only
def test_the_helper_gets_nothing_that_points_into_the_old_installation(monkeypatch):
    mount = "/tmp/.mount_ZenviAb12"
    env = {
        "HOME": "/home/u", "LANG": "en_US.UTF-8",
        "PATH": os.pathsep.join([f"{mount}/usr/bin", "/home/u/.local/bin"]),
        "LD_LIBRARY_PATH": os.pathsep.join([f"{mount}/usr/lib", f"{mount}/usr/lib/lib"]),
        "DYLD_LIBRARY_PATH": "/Applications/Zenvi.app/Contents/MacOS",
        "PYTHONPATH": f"{mount}/usr/lib", "PYTHONHOME": f"{mount}/usr",
        "QT_PLUGIN_PATH": f"{mount}/usr/plugins",
        "APPDIR": mount, "APPIMAGE": "/home/u/Zenvi.AppImage",
        # The user's own: kept.
        "XDG_DATA_DIRS": os.pathsep.join([f"{mount}/usr/share", "/var/lib/snapd/desktop", "/usr/share"]),
        "QT_STYLE_OVERRIDE": "fusion", "GTK_THEME": "Adwaita:dark",
    }
    clean = ui._clean_env(env, install_dirs=[mount])
    assert clean["HOME"] == "/home/u" and clean["LANG"] == "en_US.UTF-8"
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH",
                "APPDIR", "APPIMAGE"):
        assert key not in clean, key
    assert clean["XDG_DATA_DIRS"] == os.pathsep.join(["/var/lib/snapd/desktop", "/usr/share"])
    assert clean["QT_STYLE_OVERRIDE"] == "fusion" and clean["GTK_THEME"] == "Adwaita:dark"
    path = clean["PATH"].split(os.pathsep)
    assert "/home/u/.local/bin" in path
    assert not any(".mount_" in p for p in path)


# ── Attempts and the launch-time install ─────────────────────────────────────

def test_launching_stops_retrying_a_version_that_keeps_failing(tmp_path, staging, monkeypatch):
    package = staging / "Zenvi-v9.9.9-x86_64.AppImage"
    package.write_bytes(b"new")
    _write_manifest(staging, package)
    monkeypatch.setattr(ui, "_running_version", lambda: "1.0.0")
    spawned = []
    monkeypatch.setattr(ui, "_spawn_posix_helper", lambda script, args: spawned.append(args) or True)
    monkeypatch.setattr(ui.platform, "system", lambda: "Linux")
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Zenvi.AppImage"))

    for _ in range(ui.MAX_INSTALL_ATTEMPTS):
        assert ui.has_pending_update(for_launch=True)
        assert ui.apply_pending_update(wait_pid=12345)
        ui._record_failure("9.9.9", "the helper failed")
        # Right after a failure (the helper reopening the old version) a
        # launch doesn't try again...
        assert not ui.has_pending_update(for_launch=True)
        # ...but one later does.
        status = ui.read_status()
        status["time"] -= ui.RETRY_LAUNCH_AFTER + 1
        ui._write_status(status)

    assert not ui.has_pending_update(for_launch=True)
    assert not ui.apply_pending_update(wait_pid=12345)
    assert ui.last_failure("9.9.9") == "the helper failed"
    # Restart to Update still tries.
    assert ui.apply_pending_update(wait_pid=12345, user_requested=True)
    assert len(spawned) == ui.MAX_INSTALL_ATTEMPTS + 1
    assert spawned[0][0] == 12345


def test_a_staged_update_that_is_not_newer_is_discarded(tmp_path, staging, monkeypatch):
    package = staging / "Zenvi-v1.0.0-x86_64.AppImage"
    package.write_bytes(b"same")
    _write_manifest(staging, package, version="1.0.0")
    monkeypatch.setattr(ui, "_running_version", lambda: "1.0.0")

    assert not ui.has_pending_update(for_launch=True)
    assert not ui.apply_pending_update()
    assert not package.exists() and not (staging / "update_manifest.json").exists()


def test_stale_downloads_are_cleaned_and_the_staged_one_kept(tmp_path, staging, monkeypatch):
    monkeypatch.setattr(ui, "_running_version", lambda: "1.2.0")
    current = staging / "Zenvi-v1.3.0-arm64.dmg"
    current.write_bytes(b"x")
    _write_manifest(staging, current, version="1.3.0")
    for name in ("Zenvi-v1.0.120-arm64.dmg", "Zenvi-v1.1.0-arm64.dmg", "Zenvi-v1.3.0-arm64.dmg.part"):
        (staging / name).write_bytes(b"old")
    (staging / "install.log").write_text("log\n")

    removed = ui.cleanup_stale_staging()

    assert sorted(os.path.basename(p) for p in removed) == [
        "Zenvi-v1.0.120-arm64.dmg", "Zenvi-v1.1.0-arm64.dmg", "Zenvi-v1.3.0-arm64.dmg.part"]
    assert sorted(p.name for p in staging.iterdir()) == [
        "Zenvi-v1.3.0-arm64.dmg", "install.log", "update_manifest.json"]


def test_a_crashed_old_updater_leaves_no_second_copy_of_the_app(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    apps.mkdir()
    bundle = _make_app(apps / "Zenvi.app", "1.2.2")
    _make_app(apps / "Zenvi.app.bak", "1.2.1")
    _make_app(apps / "Zenvi.app.zenvi-update-old", "1.2.1")
    _make_app(apps / "Zenvi.app.new", "1.2.2", bundle_id="com.example.not-zenvi")
    monkeypatch.setattr(ui, "_running_bundle", lambda: str(bundle))

    removed = ui.cleanup_leftover_bundles()

    assert sorted(os.path.basename(p) for p in removed) == ["Zenvi.app.bak", "Zenvi.app.zenvi-update-old"]
    assert (apps / "Zenvi.app.new").exists(), "only Zenvi's own copies are removed"


def test_the_shell_helpers_parse(tmp_path):
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    for name, script in (("mac", ui._MACOS_HELPER), ("appimage", ui._APPIMAGE_HELPER),
                         ("deb", ui._DEB_HELPER)):
        path = tmp_path / f"{name}.sh"
        path.write_text(script)
        result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


@posix_only
def test_the_macos_admin_swap_quotes_paths_with_quotes_and_spaces(tmp_path):
    """as_admin() builds a shell command for osascript; run it with a stand-in."""
    script = textwrap.dedent('''
        LOG=/dev/null
    ''') + ui._MACOS_HELPER.split("as_admin() {", 1)[1].split("\nlog \"Update helper", 1)[0]
    script = "as_admin() {" + script
    fake_osascript = tmp_path / "osascript"
    fake_osascript.write_text('#!/bin/sh\ncat > /dev/null\nsh -c "$2"\n')
    fake_osascript.chmod(0o755)
    script = script.replace("/usr/bin/osascript", str(fake_osascript))
    target = tmp_path / "it's a folder" / "Zenvi.app"
    target.parent.mkdir()
    driver = tmp_path / "driver.sh"
    driver.write_text(script + f'\nas_admin /bin/mkdir -p "{target}"\n')
    subprocess.run(["sh", str(driver)], check=True, timeout=30)
    assert target.is_dir()


# ── One install at a time ────────────────────────────────────────────────────

@posix_only
def test_a_second_helper_stops_while_one_is_installing(tmp_path, staging, monkeypatch):
    marker = tmp_path / "ran.txt"
    installed = _fake_appimage(tmp_path / "Zenvi.AppImage", "1.0.0", marker)
    new = _fake_appimage(staging / "Zenvi-v9.9.9-x86_64.AppImage", "9.9.9", marker)
    _write_manifest(staging, new)
    monkeypatch.setenv("APPIMAGE", str(installed))

    zenvi = _zenvi_stand_in(2)
    assert ui._apply_appimage(str(new), "9.9.9", zenvi.pid, 1)
    time.sleep(0.5)
    # A second launch during the install (e.g. a double click) starts another.
    assert ui._apply_appimage(str(new), "9.9.9", zenvi.pid, 1)
    zenvi.wait()

    assert _wait_for(lambda: _helper_done(staging)), (staging / "install.log").read_text()
    log = (staging / "install.log").read_text()
    assert "Another update helper is already installing" in log
    assert log.count("Installed 9.9.9") == 1
    assert "9.9.9" in installed.read_text()


@posix_only
def test_a_parent_left_as_a_zombie_counts_as_quit(tmp_path, staging, monkeypatch):
    marker = tmp_path / "ran.txt"
    installed = _fake_appimage(tmp_path / "Zenvi.AppImage", "1.0.0", marker)
    new = _fake_appimage(staging / "Zenvi-v9.9.9-x86_64.AppImage", "9.9.9", marker)
    _write_manifest(staging, new)
    monkeypatch.setenv("APPIMAGE", str(installed))
    monkeypatch.setattr(ui, "APP_EXIT_TIMEOUT", 60)

    zombie = subprocess.Popen([sys.executable, "-c", "pass"])  # exits; never reaped here
    time.sleep(1)
    started = time.monotonic()
    assert ui._apply_appimage(str(new), "9.9.9", zombie.pid, 1)
    assert _wait_for(lambda: _helper_done(staging), 30), (staging / "install.log").read_text()
    assert time.monotonic() - started < 20
    zombie.wait()


def test_a_launch_during_an_install_waits_for_it(staging):
    ui._write_status({"version": "9.9.9", "attempts": 1, "ok": None, "time": int(time.time())})
    lock = staging / ".install-lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    assert ui.install_in_progress()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (lock / "pid").write_text(str(dead.pid))
    assert not ui.install_in_progress(), "a helper that died doesn't block launches"

    (lock / "pid").write_text(str(os.getpid()))
    ui._write_status({"version": "9.9.9", "attempts": 1, "ok": None,
                      "time": int(time.time()) - ui.INSTALL_STALE_AFTER - 1})
    assert not ui.install_in_progress(), "nor does a stale attempt"

    ui._write_status({"version": "9.9.9", "attempts": 1, "ok": True, "time": int(time.time())})
    assert not ui.install_in_progress()


def test_each_helper_runs_its_own_script_file(staging, monkeypatch):
    seen = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd[1]))
    ui._spawn_posix_helper("echo one", [])
    ui._spawn_posix_helper("echo two", [])
    assert len(set(seen)) == 2
    assert [open(p).read() for p in seen] == ["echo one", "echo two"]
