"""
Basic smoke tests for Zenvi / OpenShot query layer.

Run headlessly:
    python3 src/tests/query_tests.py -platform minimal
"""

import os
import re
import sys
import unittest

# Ensure src/ is on the path regardless of where the script is invoked from
SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(SRC_DIR))

# Strip our own args before Qt/unittest see them (e.g. -platform minimal)
_KNOWN_QT_FLAGS = {"-platform", "-style", "-stylesheet", "-widgetcount",
                   "-reverse", "-qmljsdebugger", "-display", "-geometry"}
_clean_argv = [sys.argv[0]]
_i = 1
while _i < len(sys.argv):
    arg = sys.argv[_i]
    if arg in _KNOWN_QT_FLAGS:
        _i += 2  # skip flag and its value
    elif arg.startswith("-platform=") or arg.startswith("-style="):
        _i += 1  # skip combined form
    else:
        _clean_argv.append(arg)
        _i += 1
sys.argv = _clean_argv


# ── Minimal QApplication setup ────────────────────────────────────────────────

from PyQt5.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(["zenvi-test", "-platform", "minimal"])


# ── Tests ─────────────────────────────────────────────────────────────────────

class InfoTests(unittest.TestCase):
    """Smoke-tests for classes.info."""

    def test_version_format(self):
        from classes import info
        parts = info.VERSION.split(".")
        self.assertEqual(len(parts), 3, f"VERSION should be x.y.z, got {info.VERSION!r}")
        for p in parts:
            self.assertTrue(p.isdigit(), f"Version part {p!r} is not numeric")

    def test_product_name(self):
        from classes import info
        self.assertEqual(info.PRODUCT_NAME, "Zenvi")

    def test_backend_url_points_to_zenvi_pro(self):
        from classes import info
        self.assertIn("zenvi.pro", info.BACKEND_URL,
                      f"BACKEND_URL should point to zenvi.pro, got {info.BACKEND_URL!r}")

    def test_paths_are_strings(self):
        from classes import info
        for attr in ("PATH", "USER_PATH", "PROFILES_PATH", "RESOURCES_PATH"):
            val = getattr(info, attr, None)
            self.assertIsInstance(val, str, f"info.{attr} should be a string")


class SettingsTests(unittest.TestCase):
    """Smoke-tests for classes.settings."""

    def test_settings_loads(self):
        from classes.settings import SettingStore
        s = SettingStore()
        # Should be able to instantiate without error
        self.assertIsNotNone(s)

    def test_default_settings_file_exists(self):
        from classes import info
        default = os.path.join(info.PATH, "settings", "_default.settings")
        self.assertTrue(os.path.exists(default),
                        f"Default settings file not found: {default}")


class AuthManagerTests(unittest.TestCase):
    """Verify auth manager constants."""

    def test_zenvi_website_is_pro(self):
        from classes.auth_manager import ZENVI_WEBSITE
        self.assertNotIn("zenvi.app", ZENVI_WEBSITE,
                         "ZENVI_WEBSITE must not point to zenvi.app")
        self.assertIn("zenvi.pro", ZENVI_WEBSITE,
                      f"ZENVI_WEBSITE should be zenvi.pro, got {ZENVI_WEBSITE!r}")

    def test_supabase_url_configured(self):
        from classes.auth_manager import SUPABASE_URL
        self.assertTrue(SUPABASE_URL.startswith("https://"),
                        f"SUPABASE_URL should be an https URL, got {SUPABASE_URL!r}")


class LinkTests(unittest.TestCase):
    """Guard against stale domains in links we actually send users to.

    Scoped to navigation targets on purpose: this is a GPL fork, so the
    openshot.org URLs in the file headers are required attribution and the
    credits data legitimately links to upstream authors' pages."""

    # zenvi.org has never resolved; zenvi.app is the old domain
    DEAD_DOMAINS = ("zenvi.org", "zenvi.app")

    # Lines that navigate somewhere: url = "...", webbrowser.open("..."),
    # QDesktopServices.openUrl(QUrl("..."))
    NAV_PATTERN = re.compile(
        r"(url\s*=\s*[\"'])|(webbrowser\.open)|(QDesktopServices\.openUrl)")

    def _source_files(self):
        from classes import info
        for root, dirs, files in os.walk(info.PATH):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", "locale", "legacy", "tests")]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)

    def test_no_dead_domains_in_navigation(self):
        offenders = []
        for path in self._source_files():
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if not self.NAV_PATTERN.search(line):
                        continue
                    lowered = line.lower()
                    for domain in self.DEAD_DOMAINS:
                        if domain in lowered:
                            offenders.append(
                                f"{os.path.relpath(path)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "Dead domains still linked:\n" +
                         "\n".join(offenders))

    def test_update_action_downloads_from_zenvi_pro(self):
        """The update button's manual-download fallback must reach the real site."""
        from classes import info
        path = os.path.join(info.PATH, "windows", "main_window.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"https://zenvi.pro/download"', source)


class AutoUpdaterTests(unittest.TestCase):
    """Download-progress reporting for the background auto-updater."""

    def test_percent_advances_and_clamps(self):
        from classes.auto_updater import ProgressThrottle
        t = ProgressThrottle(1000)
        self.assertEqual(t.percent_for(0), 0)
        self.assertEqual(t.percent_for(250), 25)
        self.assertEqual(t.percent_for(1000), 100)
        # A response longer than advertised must not exceed 100%
        self.assertEqual(t.percent_for(1200), 100)

    def test_unknown_size_is_indeterminate(self):
        from classes.auto_updater import ProgressThrottle, PROGRESS_INDETERMINATE
        t = ProgressThrottle(0)
        self.assertEqual(t.percent_for(0), PROGRESS_INDETERMINATE)
        self.assertEqual(t.percent_for(5_000_000), PROGRESS_INDETERMINATE)

    def test_throttle_suppresses_redundant_ticks(self):
        from classes.auto_updater import ProgressThrottle
        t = ProgressThrottle(1000, interval=0.25)
        self.assertEqual(t.tick(0, 0.0), 0)          # first tick always reports
        self.assertIsNone(t.tick(1, 0.01))           # same percent, too soon
        self.assertEqual(t.tick(250, 0.02), 25)      # percent advanced
        self.assertIsNone(t.tick(251, 0.03))         # same percent again
        self.assertEqual(t.tick(252, 0.30), 25)      # heartbeat after interval

    def test_throttle_reports_every_percent_of_a_large_asset(self):
        """A 120MB asset in 64KB chunks must yield 101 reports, not ~2000."""
        from classes.auto_updater import ProgressThrottle, DOWNLOAD_CHUNK_SIZE
        total = 120 * 1024 * 1024
        t = ProgressThrottle(total)
        reported, downloaded, now = [], 0, 0.0
        while downloaded < total:
            downloaded = min(total, downloaded + DOWNLOAD_CHUNK_SIZE)
            now += 0.001  # fast connection: interval never triggers
            percent = t.tick(downloaded, now)
            if percent is not None:
                reported.append(percent)
        self.assertEqual(reported, list(range(0, 101)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
