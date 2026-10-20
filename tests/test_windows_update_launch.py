"""What a Windows user sees while an update installs, and launches that land mid-install."""

import os
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.modules.setdefault("openshot", types.ModuleType("openshot"))

from classes import update_installer as ui  # noqa: E402


def test_the_windows_installer_shows_its_progress_window():
    # /VERYSILENT hides Setup for the whole multi-minute install, so people
    # think nothing is happening and launch again mid-install.
    assert "/SILENT" in ui._INNO_SILENT_ARGS
    assert "/VERYSILENT" not in ui._INNO_SILENT_ARGS
    assert "/SUPPRESSMSGBOXES" in ui._INNO_SILENT_ARGS


def _launch_during_install(tmp_path, argv, setup=""):
    """Run launch.py as a frozen build while an installer helper is working
    and Setup has half-replaced the install (the Qt binding can't import)."""
    stage = tmp_path / "updates"
    (stage / ".install-lock").mkdir(parents=True)
    (stage / ".install-lock" / "pid").write_text(str(os.getpid()))
    script = textwrap.dedent(f"""
        import runpy, sys, types
        sys.path.insert(0, {_ROOT!r})
        sys.frozen = True
        sys.modules["openshot"] = types.ModuleType("openshot")
        sys.modules["qt_api"] = None  # half-installed: importing it fails
        from classes import update_installer as ui
        stage = {str(stage)!r}
        ui.UPDATE_STAGING_DIR = stage
        ui.UPDATE_STATUS = stage + "/update_status.json"
        ui.UPDATE_MANIFEST = stage + "/update_manifest.json"
        ui.UPDATE_LOG = stage + "/install.log"
        ui._write_status({{"version": "9.9.9", "attempts": 1, "ok": None, "time": {int(time.time())}}})
        ui._show_update_notice = lambda system, version: print("NOTICE", version)
    """) + textwrap.dedent(setup) + textwrap.dedent(f"""
        sys.argv = {argv!r}
        runpy.run_path({os.path.join(_ROOT, "launch.py")!r}, run_name="__main__")
    """)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)


def test_a_launch_during_an_install_exits_before_loading_the_half_installed_app(tmp_path):
    proc = _launch_during_install(tmp_path, ["zenvi"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "NOTICE" in proc.stdout
    assert "No suitable Qt binding" not in proc.stderr


def test_an_encoder_trial_during_an_install_still_runs_the_trial(tmp_path):
    # Exit 0 from a trial child means "this hardware encoder works"; the
    # install guard must not answer for it.
    proc = _launch_during_install(tmp_path, ["zenvi", "--zenvi-trial-encode", "x"], setup="""
        from classes import encoder_trial
        encoder_trial.main = lambda argv: print("TRIAL", argv) or 3
    """)
    assert "TRIAL" in proc.stdout, proc.stderr[-2000:]
    assert proc.returncode == 3


def test_the_installer_cannot_be_cancelled_mid_update():
    # /SILENT shows a Cancel button; cancelling after [InstallDelete] has
    # emptied {app} would leave a half-deleted install for the helper to open.
    iss = os.path.join(_ROOT, "..", "installer", "windows-installer.iss")
    with open(iss, encoding="utf-8") as fh:
        assert "AllowCancelDuringInstall=no" in fh.read()


def test_launches_at_the_same_moment_both_pass_the_write_access_check(tmp_path):
    # The helper's relaunch and a manual launch start together; a shared
    # test file let one delete it under the other.
    from classes import app

    errors = []

    def launch():
        for _ in range(50):
            try:
                app.check_user_path_writable(str(tmp_path))
            except Exception as ex:  # noqa: BLE001 - any failure is the bug
                errors.append(ex)

    threads = [threading.Thread(target=launch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert os.listdir(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="runs the generated PowerShell helper")
def test_the_helper_records_its_outcome_in_real_unix_time(tmp_path):
    # Windows PowerShell's `Get-Date -UFormat %s` is local time read as UTC, so
    # a failure looked hours old (west of UTC) or in the future (east of it).
    status = tmp_path / "update_status.json"
    script = ui._build_update_helper_script(
        str(tmp_path / "Zenvi-v9.9.9-x86_64.exe"), str(tmp_path / "update_manifest.json"),
        "", str(tmp_path / "install.log"), version="9.9.9", status_path=str(status))
    status_fn = script[script.index("function Status"):]
    status_fn = status_fn[:status_fn.index("\n}\n") + 3]
    ps = tmp_path / "status.ps1"
    ps.write_text(status_fn + "Status $false 'test'\n", encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(ps)], check=True, timeout=60)
    import json
    with open(status, encoding="utf-8-sig") as fh:
        recorded = json.load(fh)["time"]
    assert abs(recorded - time.time()) < 120
