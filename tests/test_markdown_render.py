"""Regression tests for assistant markdown rendering."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _mod in (
    "PyQt5",
    "PyQt5.QtCore",
    "PyQt5.QtWidgets",
    "PyQt5.QtGui",
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebKitWidgets",
):
    sys.modules.setdefault(_mod, MagicMock())

from windows.ai_chat_ui import _markdown_to_html  # noqa: E402

try:
    import markdown  # noqa: F401
    _HAS_MARKDOWN = True
except ImportError:
    _HAS_MARKDOWN = False


@unittest.skipUnless(_HAS_MARKDOWN, "markdown package not installed")
class MarkdownRenderTests(unittest.TestCase):
    def test_bold_renders_strong(self):
        html = _markdown_to_html("**x**")
        self.assertIn("<strong>x</strong>", html)

    def test_inline_code_renders_code_tag(self):
        html = _markdown_to_html("Use `foo` here")
        self.assertIn("<code>foo</code>", html)


class MarkdownFailureOnceTests(unittest.TestCase):
    def setUp(self):
        import windows.ai_chat_ui as ui
        self._ui = ui
        self._prev = ui._markdown_failed
        ui._markdown_failed = False

    def tearDown(self):
        self._ui._markdown_failed = self._prev

    def test_markdown_failure_is_logged_once(self):
        fake = MagicMock()
        fake.markdown.side_effect = ImportError("no extra")
        with patch.dict(sys.modules, {"markdown": fake}):
            with patch("windows.ai_chat_ui.log") as log:
                first = _markdown_to_html("hello")
                second = _markdown_to_html("again")
        self.assertIn("hello", first)
        self.assertIn("again", second)
        self.assertEqual(log.warning.call_count, 1)
        self.assertTrue(self._ui._markdown_failed)


if __name__ == "__main__":
    unittest.main()
