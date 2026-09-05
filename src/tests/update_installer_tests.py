"""
Tests for the Windows auto-update apply path: building the detached external
updater's PowerShell script, spawning it, and the has_pending_update /
apply_pending_update cleanup-vs-keep-staged branching in
classes.update_installer.

No PyQt5 required — classes.update_installer intentionally avoids it so it
can run at the very top of launch.py before any heavy imports.

Run:
    python src/tests/update_installer_tests.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# Ensure src/ is on the path regardless of where the script is invoked from
SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(SRC_DIR))


class UpdateHelperScriptTests(unittest.TestCase):
    """_build_update_helper_script generates the PowerShell source handed to
    the detached updater. Platform-agnostic: it's pure string building, no
    Windows API calls, so this runs on every CI runner."""

    def test_contains_installer_and_manifest_paths(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            r"C:\Program Files\Zenvi\zenvi.exe",
            r"C:\staged\install.log",
        )
        self.assertIn(r"C:\staged\Zenvi-Setup.exe", script)
        self.assertIn(r"C:\staged\update_manifest.json", script)
        self.assertIn(r"C:\Program Files\Zenvi\zenvi.exe", script)
        self.assertIn("-Wait -PassThru", script)
        self.assertIn("/CLOSEAPPLICATIONS", script)
        self.assertNotIn("/CURRENTUSER", script)

    def test_omits_relaunch_block_when_no_target(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            None,
            r"C:\staged\install.log",
        )
        self.assertNotIn("Relaunched app", script)

    def test_single_quotes_in_paths_are_escaped(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\Users\O'Brien\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            None,
            r"C:\staged\install.log",
        )
        self.assertIn("O''Brien", script)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only")
    def test_script_is_syntactically_valid_powershell(self):
        """Parse (not execute) the generated script with PowerShell's own
        tokenizer/parser so a real quoting bug would fail this test."""
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            r"C:\Program Files\Zenvi\zenvi.exe",
            r"C:\staged\install.log",
        )
        fd, path = tempfile.mkstemp(suffix=".ps1")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(script)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-Command",
                 "$null = [System.Management.Automation.Language.Parser]::"
                 "ParseFile($args[0], [ref]$null, [ref]$errors); "
                 "if ($errors.Count -gt 0) { $errors | ForEach-Object "
                 "{ Write-Error $_ }; exit 1 } else { exit 0 }",
                 path],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(
                result.returncode, 0,
                f"PowerShell parse errors:\n{result.stdout}\n{result.stderr}")
        finally:
            os.unlink(path)


class SpawnExternalUpdaterTests(unittest.TestCase):
    """_spawn_external_updater writes the script and launches it detached.
    Mocked subprocess/filesystem calls, so this runs on every CI runner."""

    def setUp(self):
        from classes import update_installer as ui
        self.ui = ui
        self.tmpdir = tempfile.mkdtemp(prefix="zenvi_update_test_")
        self._staging_patch = mock.patch.object(
            self.ui, "UPDATE_STAGING_DIR", self.tmpdir)
        self._staging_patch.start()

    def tearDown(self):
        self._staging_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_script_and_launches_powershell_detached(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            ok = self.ui._spawn_external_updater(
                r"C:\staged\Zenvi-Setup.exe", r"C:\Program Files\Zenvi\zenvi.exe")

        self.assertTrue(ok)
        script_path = os.path.join(
            self.tmpdir, self.ui._UPDATE_HELPER_SCRIPT_NAME)
        self.assertTrue(os.path.isfile(script_path))

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "powershell.exe")
        self.assertIn("-File", cmd)
        self.assertEqual(cmd[cmd.index("-File") + 1], script_path)
        self.assertEqual(kwargs.get("creationflags"),
                          self.ui._HELPER_CREATIONFLAGS)

    def test_popen_failure_returns_false(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("boom")):
            ok = self.ui._spawn_external_updater(
                r"C:\staged\Zenvi-Setup.exe", None)
        self.assertFalse(ok)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only")
    def test_end_to_end_with_stub_installer(self):
        """Real PowerShell, real process spawn, real files — no mocks. A
        stub 'Setup.exe' (a tiny batch file) stands in for Inno Setup and a
        stub relaunch target stands in for zenvi.exe; verifies the full
        wait -> verify exit code -> cleanup -> relaunch chain end to end."""
        # This test calls _spawn_external_updater directly (bypassing
        # _apply_windows's filename.endswith(".exe") gate), so a .bat stub
        # standing in for Setup.exe is fine — Start-Process runs it via
        # cmd.exe either way.
        stub_installer = os.path.join(self.tmpdir, "Zenvi-Setup.bat")
        with open(stub_installer, "w", encoding="utf-8") as fh:
            fh.write("@echo off\r\nexit /b 0\r\n")

        relaunch_marker = os.path.join(self.tmpdir, "relaunched.txt")
        relaunch_target = os.path.join(self.tmpdir, "fake_zenvi.bat")
        with open(relaunch_target, "w", encoding="utf-8") as fh:
            fh.write(f'@echo off\r\necho relaunched> "{relaunch_marker}"\r\n')

        manifest_path = os.path.join(self.tmpdir, "update_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"filepath": stub_installer}, fh)
        self._manifest_patch = mock.patch.object(
            self.ui, "UPDATE_MANIFEST", manifest_path)
        self._manifest_patch.start()
        self.addCleanup(self._manifest_patch.stop)

        ok = self.ui._spawn_external_updater(stub_installer, relaunch_target)
        self.assertTrue(ok)

        import time
        deadline = time.time() + 20
        while time.time() < deadline:
            if not os.path.exists(manifest_path) and os.path.exists(relaunch_marker):
                break
            time.sleep(0.5)

        self.assertFalse(os.path.exists(manifest_path),
                          "manifest should be removed by the helper on success")
        self.assertFalse(os.path.exists(stub_installer),
                          "staged installer should be removed by the helper on success")
        self.assertTrue(os.path.exists(relaunch_marker),
                         "helper should have relaunched the target")


class ApplyWindowsTests(unittest.TestCase):
    """_apply_windows: gate on filename, delegate to the external updater,
    never touch staged files itself (that's the updater's job now)."""

    def test_unknown_extension_rejected_without_spawning(self):
        from classes import update_installer as ui
        with mock.patch.object(ui, "_spawn_external_updater") as spawn:
            ok = ui._apply_windows(r"C:\staged\Zenvi-Setup.msi", "Zenvi-Setup.msi")
        self.assertFalse(ok)
        spawn.assert_not_called()

    def test_success_delegates_to_external_updater(self):
        from classes import update_installer as ui
        with mock.patch.object(ui, "_spawn_external_updater", return_value=True) as spawn:
            ok = ui._apply_windows(r"C:\staged\Zenvi-Setup.exe", "Zenvi-Setup.exe")
        self.assertTrue(ok)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args[0][0], r"C:\staged\Zenvi-Setup.exe")

    def test_spawn_failure_propagates(self):
        from classes import update_installer as ui
        with mock.patch.object(ui, "_spawn_external_updater", return_value=False):
            ok = ui._apply_windows(r"C:\staged\Zenvi-Setup.exe", "Zenvi-Setup.exe")
        self.assertFalse(ok)


class ApplyPendingUpdateCleanupTests(unittest.TestCase):
    """apply_pending_update()'s cleanup-vs-keep-staged branching, exercised
    against a mocked _apply_windows/_apply_linux so this runs in CI (Linux)
    too, not just on a Windows dev machine."""

    def setUp(self):
        from classes import update_installer as ui
        self.ui = ui
        self.tmpdir = tempfile.mkdtemp(prefix="zenvi_update_test_")
        self.installer_path = os.path.join(self.tmpdir, "Zenvi-Setup.exe")
        with open(self.installer_path, "wb") as fh:
            fh.write(b"dummy installer bytes")

        import hashlib
        sha256 = hashlib.sha256()
        with open(self.installer_path, "rb") as fh:
            sha256.update(fh.read())

        self.manifest_path = os.path.join(self.tmpdir, "update_manifest.json")
        self.manifest = {
            "version": "9.9.9",
            "filename": "Zenvi-Setup.exe",
            "filepath": self.installer_path,
            "sha256": sha256.hexdigest(),
            "size": os.path.getsize(self.installer_path),
            "platform": "windows",
        }
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(self.manifest, fh)

        self._manifest_patch = mock.patch.object(
            self.ui, "UPDATE_MANIFEST", self.manifest_path)
        self._manifest_patch.start()

    def tearDown(self):
        self._manifest_patch.stop()
        if hasattr(self, "_platform_patch"):
            self._platform_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _with_platform(self, name):
        self._platform_patch = mock.patch("platform.system", return_value=name)
        self._platform_patch.start()

    def test_windows_success_hands_off_and_leaves_staged_files(self):
        """On Windows, _apply_windows returning True only means "handed off
        to the external updater" — the manifest/installer are still needed
        by that detached process and must NOT be deleted here."""
        self._with_platform("Windows")
        with mock.patch.object(self.ui, "_apply_windows", return_value=True):
            result = self.ui.apply_pending_update()
        self.assertTrue(result)
        self.assertTrue(os.path.exists(self.manifest_path),
                         "manifest must survive hand-off — the external updater deletes it")
        self.assertTrue(os.path.exists(self.installer_path),
                         "staged installer must survive hand-off — the external updater deletes it")

    def test_windows_failure_keeps_staged_files_for_retry(self):
        self._with_platform("Windows")
        with mock.patch.object(self.ui, "_apply_windows", return_value=False):
            result = self.ui.apply_pending_update()
        self.assertFalse(result)
        self.assertTrue(os.path.exists(self.manifest_path))
        self.assertTrue(os.path.exists(self.installer_path))

    def test_non_windows_success_cleans_up_immediately(self):
        """Linux/macOS apply the update synchronously in-process, so a
        confirmed success there really does mean it's safe to clean up now."""
        self._with_platform("Linux")
        with mock.patch.object(self.ui, "_apply_linux", return_value=True):
            result = self.ui.apply_pending_update()
        self.assertTrue(result)
        self.assertFalse(os.path.exists(self.manifest_path))
        self.assertFalse(os.path.exists(self.installer_path))

    def test_non_windows_failure_keeps_staged_files_for_retry(self):
        self._with_platform("Linux")
        with mock.patch.object(self.ui, "_apply_linux", return_value=False):
            result = self.ui.apply_pending_update()
        self.assertFalse(result)
        self.assertTrue(os.path.exists(self.manifest_path))
        self.assertTrue(os.path.exists(self.installer_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
