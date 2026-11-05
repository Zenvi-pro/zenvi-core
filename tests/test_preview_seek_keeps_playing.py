"""A seek during playback must not pause it. Real-Qt test; needs PyQt5 and libopenshot.

Playback moves the timeline playhead and the timeline echoes that back as a seek. libopenshot reports
a non-play mode while that seek loads, and _flush_seek took it for "paused" and ran its Play+Pause
nudge, so playback stopped a few frames after Play.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

import openshot  # noqa: E402

PlayerWorker = importlib.import_module("windows.preview_thread").PlayerWorker


class Player:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def Mode(self):
        return self.mode

    def Seek(self, n):
        self.calls.append(("Seek", n))
        if self.mode == openshot.PLAYBACK_PLAY:
            self.mode = openshot.PLAYBACK_LOADING  # while the seek loads

    def Play(self):
        self.calls.append(("Play",))
        self.mode = openshot.PLAYBACK_PLAY

    def Pause(self):
        self.calls.append(("Pause",))
        self.mode = openshot.PLAYBACK_PAUSED


def _worker(player):
    return SimpleNamespace(player=player, parent=SimpleNamespace(initialized=True),
                           _seek_lock=threading.Lock(), _pending_seek=42)


def test_a_seek_while_playing_keeps_playing():
    player = Player(openshot.PLAYBACK_PLAY)
    PlayerWorker._flush_seek(_worker(player))
    assert ("Pause",) not in player.calls, player.calls
    assert ("Seek", 42) in player.calls


def test_back_to_back_seeks_while_playing_keep_playing():
    """The timeline echoes the playhead every frame, so the next seek often lands while the last still loads."""
    player = Player(openshot.PLAYBACK_PLAY)
    worker = _worker(player)
    PlayerWorker._flush_seek(worker)
    worker._pending_seek = 43
    PlayerWorker._flush_seek(worker)
    assert ("Pause",) not in player.calls, player.calls


def test_a_seek_while_paused_still_renders_the_frame():
    player = Player(openshot.PLAYBACK_PAUSED)
    PlayerWorker._flush_seek(_worker(player))
    assert player.calls == [("Seek", 42), ("Play",), ("Pause",)]
