"""
Tests for the Windows auto-update apply path: the ctypes named-mutex
singleton (classes.win_singleton) and the blocking-wait/verify logic in
classes.update_installer._apply_windows.

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


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class WinSingletonTests(unittest.TestCase):
    """The named mutex is what makes Inno's AppMutex-driven
    /CLOSEAPPLICATIONS able to find running Zenvi processes at all."""

    def setUp(self):
        from classes import win_singleton
        win_singleton.release()  # start each test from a clean slate

    def tearDown(self):
        from classes import win_singleton
        win_singleton.release()

    def test_acquire_returns_true(self):
        from classes import win_singleton
        self.assertTrue(win_singleton.acquire())

    def test_acquire_idempotent(self):
        from classes import win_singleton
        win_singleton.acquire()
        n = len(win_singleton._handles)
        win_singleton.acquire()
        self.assertEqual(len(win_singleton._handles), n)

    def test_mutex_visible_to_other_process(self):
        """A second, independent process must be able to see the named
        mutex — this is exactly what Inno/Restart Manager relies on."""
        from classes import win_singleton
        win_singleton.acquire()

        code = (
            "import ctypes, sys\n"
            "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
            "SYNCHRONIZE = 0x00100000\n"
            "h = k.OpenMutexW(SYNCHRONIZE, False, %r)\n"
            "sys.exit(0 if h else 1)\n" % win_singleton.MUTEX_NAME
        )
        result = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 0,
                          "second process could not see the named mutex")


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class RunAndWaitTests(unittest.TestCase):
    """_run_and_wait must only report success on a confirmed zero exit
    code, and must not hang past its timeout."""

    def test_success(self):
        from classes import update_installer as ui
        ok, rc = ui._run_and_wait(
            [sys.executable, "-c", "import sys; sys.exit(0)"], timeout=10)
        self.assertTrue(ok)
        self.assertEqual(rc, 0)

    def test_nonzero_exit(self):
        from classes import update_installer as ui
        ok, rc = ui._run_and_wait(
            [sys.executable, "-c", "import sys; sys.exit(1)"], timeout=10)
        self.assertFalse(ok)
        self.assertEqual(rc, 1)

    def test_timeout(self):
        from classes import update_installer as ui
        # Sleep just long enough to outlive the timeout, not longer — a
        # multi-second sleeper would outlive the test process too (real
        # production code intentionally leaves a timed-out installer
        # running rather than killing it mid-write, so _run_and_wait
        # doesn't hand back a handle to terminate it here).
        ok, rc = ui._run_and_wait(
            [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.3)
        self.assertFalse(ok)
        self.assertIsNone(rc)

    def test_missing_executable(self):
        from classes import update_installer as ui
        ok, rc = ui._run_and_wait(
            [os.path.join(tempfile.gettempdir(), "zenvi_does_not_exist.exe")],
            timeout=5)
        self.assertFalse(ok)
        self.assertIsNone(rc)


class ApplyPendingUpdateCleanupTests(unittest.TestCase):
    """Platform-agnostic: exercises apply_pending_update()'s existing
    cleanup-vs-keep-staged branching against a mocked _apply_windows, so
    this runs in CI (Linux) too, not just on a Windows dev machine."""

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
        self._platform_patch = mock.patch("platform.system", return_value="Windows")
        self._platform_patch.start()

    def tearDown(self):
        self._manifest_patch.stop()
        self._platform_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_success_cleans_up_manifest_and_installer(self):
        with mock.patch.object(self.ui, "_apply_windows", return_value=True):
            result = self.ui.apply_pending_update()
        self.assertTrue(result)
        self.assertFalse(os.path.exists(self.manifest_path),
                          "manifest should be deleted after a confirmed success")
        self.assertFalse(os.path.exists(self.installer_path),
                          "staged installer should be deleted after a confirmed success")

    def test_failure_keeps_staged_files_for_retry(self):
        with mock.patch.object(self.ui, "_apply_windows", return_value=False):
            result = self.ui.apply_pending_update()
        self.assertFalse(result)
        self.assertTrue(os.path.exists(self.manifest_path),
                         "manifest must survive a failed install so it can be retried")
        self.assertTrue(os.path.exists(self.installer_path),
                         "staged installer must survive a failed install so it can be retried")


if __name__ == "__main__":
    unittest.main(verbosity=2)
