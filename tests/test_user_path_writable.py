"""Regression tests for the startup write-access probe (issue #313)."""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.modules.setdefault("openshot", types.ModuleType("openshot"))

from classes import app, info  # noqa: E402


class UserPathWritableTests(unittest.TestCase):

    def test_file_held_by_another_process_does_not_fail(self):
        # A leftover probe file still open elsewhere (antivirus, a second
        # instance) must not make a writable directory look read-only.
        with tempfile.TemporaryDirectory() as path:
            os.makedirs(os.path.join(path, "PERMISSION"))
            with open(os.path.join(path, "PERMISSION", "test%s" % info.PROJECT_EXT), "w"):
                app.check_user_path_writable(path)

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                     "directory chmod is not enforced here")
    def test_read_only_dir_raises_permission_error(self):
        with tempfile.TemporaryDirectory() as path:
            os.chmod(path, 0o500)
            try:
                with self.assertRaises(PermissionError):
                    app.check_user_path_writable(path)
            finally:
                os.chmod(path, 0o700)


if __name__ == "__main__":
    unittest.main()
