"""The windowed Windows build must not flash a console for each child process."""

import ctypes
import subprocess
import sys
from unittest.mock import MagicMock

from classes.win_console import CREATE_NO_WINDOW, hide_child_consoles

NEW_PROCESS_GROUP = 0x00000200


def _spawn_kwargs(monkeypatch, platform, console_window):
    """Install the hook as `platform` would, then return what a spawn passes on."""
    seen = {}
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, *a, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "platform", platform)
    windll = MagicMock()
    windll.kernel32.GetConsoleWindow.return_value = console_window
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    hide_child_consoles()
    subprocess.Popen.__init__(object(), ["hermes"], creationflags=NEW_PROCESS_GROUP)
    return seen


def test_windowed_app_hides_every_child_console(monkeypatch):
    seen = _spawn_kwargs(monkeypatch, "win32", console_window=0)
    assert seen["creationflags"] == NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def test_console_build_lets_children_share_its_console(monkeypatch):
    seen = _spawn_kwargs(monkeypatch, "win32", console_window=1234)
    assert seen["creationflags"] == NEW_PROCESS_GROUP


def test_other_platforms_are_untouched(monkeypatch):
    seen = _spawn_kwargs(monkeypatch, "linux", console_window=0)
    assert seen["creationflags"] == NEW_PROCESS_GROUP
