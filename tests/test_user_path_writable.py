"""Regression tests for the startup write-access probe (issue #313)."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from classes import info  # noqa: E402


class UserPathWritableTests(unittest.TestCase):

    def test_writable_dir_passes_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as path:
            info.check_user_path_writable(path)
            self.assertEqual(os.listdir(path), [])

    def test_concurrent_launches_do_not_collide(self):
        # Two Zenvi processes starting together used to race on one fixed
        # PERMISSION/test.zvn file and report a bogus permission error.
        with tempfile.TemporaryDirectory() as path:
            errors = []

            def probe():
                for _ in range(50):
                    try:
                        info.check_user_path_writable(path)
                    except OSError as ex:
                        errors.append(ex)

            threads = [threading.Thread(target=probe) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])

    def test_file_held_by_another_process_does_not_fail(self):
        # A leftover probe file still open elsewhere (antivirus, a second
        # instance) must not make a writable directory look read-only.
        with tempfile.TemporaryDirectory() as path:
            os.makedirs(os.path.join(path, "PERMISSION"))
            with open(os.path.join(path, "PERMISSION", "test%s" % info.PROJECT_EXT), "w"):
                info.check_user_path_writable(path)

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                     "directory chmod is not enforced here")
    def test_read_only_dir_raises_permission_error(self):
        with tempfile.TemporaryDirectory() as path:
            os.chmod(path, 0o500)
            try:
                with self.assertRaises(PermissionError):
                    info.check_user_path_writable(path)
            finally:
                os.chmod(path, 0o700)


if __name__ == "__main__":
    unittest.main()
