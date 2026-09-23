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
        self.assertIn("-ArgumentList", script)
        self.assertNotIn("/CURRENTUSER", script)

    def test_non_exe_stub_omits_inno_argument_list(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.bat",
            r"C:\staged\update_manifest.json",
            None,
            r"C:\staged\install.log",
        )
        self.assertIn(r"C:\staged\Zenvi-Setup.bat", script)
        self.assertIn("-Wait -PassThru", script)
        self.assertNotIn("-ArgumentList", script)

    def test_waits_for_parent_pid_before_starting_setup(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            r"C:\Program Files\Zenvi\zenvi.exe",
            r"C:\staged\install.log",
            parent_pid=4242,
        )
        self.assertIn("Wait-Process -Id 4242", script)
        self.assertIn("External updater started", script)

    def test_omits_wait_when_no_parent_pid(self):
        from classes import update_installer as ui
        script = ui._build_update_helper_script(
            r"C:\staged\Zenvi-Setup.exe",
            r"C:\staged\update_manifest.json",
            None,
            r"C:\staged\install.log",
        )
        self.assertNotIn("Wait-Process", script)

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

        # Skip Wait-Process: this unittest process does not exit, and the
        # real app path always waits via the default parent_pid=os.getpid().
        ok = self.ui._spawn_external_updater(
            stub_installer, relaunch_target, parent_pid=None)
        self.assertTrue(ok)

        import time
        deadline = time.time() + 60
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


class ParseVersionTests(unittest.TestCase):
    def test_plain_and_prefixed(self):
        from classes.update_installer import parse_version, is_version_newer
        self.assertEqual(parse_version("1.1.0"), (1, 1, 0))
        self.assertEqual(parse_version("v1.1.0"), (1, 1, 0))
        self.assertTrue(is_version_newer("1.1.1", "1.1.0"))
        self.assertFalse(is_version_newer("1.1.0", "1.1.0"))

    def test_strips_prerelease_and_build_metadata(self):
        from classes.update_installer import parse_version
        self.assertEqual(parse_version("1.1.0-rc1"), (1, 1, 0))
        self.assertEqual(parse_version("1.1.0+build.5"), (1, 1, 0))
        self.assertNotEqual(parse_version("1.1.0-rc1"), (0,))


class VerifyIntegrityTests(unittest.TestCase):
    def test_missing_sha256_fails_closed(self):
        from classes import update_installer as ui
        tmp = tempfile.mkdtemp(prefix="zenvi_sha_")
        try:
            path = os.path.join(tmp, "pkg.exe")
            with open(path, "wb") as fh:
                fh.write(b"abc")
            self.assertFalse(ui._verify_integrity({"filepath": path, "sha256": ""}))
            self.assertFalse(ui._verify_integrity({"filepath": path}))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_matching_sha256_passes(self):
        import hashlib
        from classes import update_installer as ui
        tmp = tempfile.mkdtemp(prefix="zenvi_sha_")
        try:
            path = os.path.join(tmp, "pkg.exe")
            data = b"abc"
            with open(path, "wb") as fh:
                fh.write(data)
            digest = hashlib.sha256(data).hexdigest()
            self.assertTrue(ui._verify_integrity({"filepath": path, "sha256": digest}))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class ApplyMacosSwapTests(unittest.TestCase):
    def test_copytree_uses_symlinks_and_swaps_via_bak(self):
        from classes import update_installer as ui
        recorded = {}

        def fake_copytree(src, dst, symlinks=False):
            recorded["copy"] = (src, dst, symlinks)

        def fake_exists(path):
            return path.endswith(".app") and ".new" not in path and ".bak" not in path

        with mock.patch("os.listdir", return_value=["Zenvi.app"]), \
             mock.patch("os.path.exists", side_effect=fake_exists), \
             mock.patch("shutil.copytree", side_effect=fake_copytree), \
             mock.patch("shutil.rmtree"), \
             mock.patch("os.rename") as renamed, \
             mock.patch.object(ui, "_relaunch"), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            ok = ui._apply_macos("/tmp/Zenvi.dmg", "Zenvi.dmg")
        self.assertTrue(ok)
        self.assertTrue(recorded["copy"][2], "copytree must preserve symlinks")
        self.assertTrue(recorded["copy"][1].endswith(".new"))
        dests = [c[0][1] for c in renamed.call_args_list]
        self.assertTrue(any(d.endswith(".bak") for d in dests))


class ApplyDebRelaunchTests(unittest.TestCase):
    def test_deb_success_relaunches(self):
        from classes import update_installer as ui
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")), \
             mock.patch("shutil.which", return_value="/usr/bin/zenvi"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch.object(ui, "_relaunch") as relaunch:
            ok = ui._apply_deb("/tmp/Zenvi.deb")
        self.assertTrue(ok)
        relaunch.assert_called_once_with(["/usr/bin/zenvi"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
