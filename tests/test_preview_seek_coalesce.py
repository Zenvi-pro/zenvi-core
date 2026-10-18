"""PlayerWorker renders a burst of seeks once, and its playback loop only polls
fast while playing. Real-Qt test; needs PyQt5 and libopenshot.
"""

from __future__ import annotations

import importlib
import os
import sys
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

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import openshot  # noqa: E402

_APP = QApplication.instance() or QApplication([])

preview_thread = importlib.import_module("windows.preview_thread")
PlayerWorker = preview_thread.PlayerWorker


class FakeQtPlayer:
    """Stands in for openshot.QtPlayer and records what PlayerWorker asks of it."""

    def __init__(self, mode):
        self.mode = mode
        self.seeks = []
        self.plays = 0

    def Seek(self, frame):
        self.seeks.append(frame)

    def Play(self):
        self.plays += 1

    def Pause(self):
        pass

    def Mode(self):
        return self.mode

    def Position(self):
        return 1

    def Reader(self, reader):
        self.readers = getattr(self, "readers", 0) + 1

    def Speed(self):
        return 1.0

    def GetRendererQObject(self):
        return 0  # no renderer, so Start() returns from initPlayer() early


@pytest.fixture
def make_worker(monkeypatch):
    def make(mode=openshot.PLAYBACK_PAUSED):
        player = FakeQtPlayer(mode)
        monkeypatch.setattr(openshot, "QtPlayer", lambda: player)
        worker = PlayerWorker()
        worker.Init(SimpleNamespace(initialized=True), None, None)
        return worker, player

    return make


def test_burst_of_seeks_renders_only_the_latest_frame(make_worker):
    worker, player = make_worker()
    worker.Seek(10)
    worker.Seek(20)
    worker.Seek(30)
    QCoreApplication.processEvents()
    assert player.seeks == [30]
    assert player.plays == 1  # one Play+Pause render, not one per seek


def test_seek_after_a_flush_is_applied(make_worker):
    worker, player = make_worker()
    worker.Seek(10)
    worker.Seek(20)
    QCoreApplication.processEvents()
    worker.Seek(40)
    QCoreApplication.processEvents()
    assert player.seeks == [20, 40]


@pytest.mark.parametrize(
    "mode, delay",
    [(openshot.PLAYBACK_PLAY, 0.01), (openshot.PLAYBACK_PAUSED, 0.04)],
    ids=["playing", "paused"],
)
def test_start_loop_polls_fast_only_while_playing(monkeypatch, make_worker, mode, delay):
    worker, _ = make_worker(mode)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        worker.is_running = False  # one pass through the loop is enough

    monkeypatch.setattr(preview_thread.time, "sleep", fake_sleep)
    worker.CheckAudioDevice = lambda: None  # its 1s timer must not fire in a later test
    worker.Start()
    assert sleeps == [delay]


def test_direct_seek_from_another_thread_applies_before_returning(make_worker):
    # cutting.py seeks to 1 and then emits Play: the seek must not trail the Play
    from PyQt5.QtCore import QThread

    worker, player = make_worker()
    thread = QThread()
    worker.moveToThread(thread)  # the worker lives on the preview thread, this test on the GUI thread
    worker.Seek(1)
    assert player.seeks == [1]


def test_refresh_while_playing_leaves_the_player_alone(make_worker):
    # #332: re-setting the reader mid-playback left playback crawling until pause/unpause
    worker, player = make_worker(openshot.PLAYBACK_PLAY)
    worker.timeline = object()
    worker.clip_path = None
    worker.refreshFrame()
    assert getattr(player, "readers", 0) == 0
    assert player.seeks == []


def test_project_open_seek_survives_a_queued_refresh(make_worker):
    # The open-project Seek(1) is pending when the deferred refresh runs; the refresh
    # re-seeked to the old position and the preview stayed off frame 1
    from unittest.mock import MagicMock

    worker, player = make_worker()
    worker.parent.LoadFileSignal = MagicMock()
    worker.timeline = object()
    worker.clip_path = None
    player.Position = lambda: 240
    worker.Seek(1)
    worker.refreshFrame()
    QCoreApplication.processEvents()
    assert player.seeks[-1] == 1


def test_a_seek_burst_through_the_signal_reaches_the_running_worker_once(make_worker):
    # The real UI path: SeekSignal is queued to the worker on its own thread
    import time

    from PyQt5.QtCore import QObject, QThread, pyqtSignal

    class Emitter(QObject):
        seek = pyqtSignal(int)

    worker, player = make_worker()
    thread = QThread()
    worker.moveToThread(thread)
    emitter = Emitter()
    emitter.seek.connect(worker.Seek)
    thread.start()
    try:
        for frame in range(1, 51):
            emitter.seek.emit(frame)
        deadline = time.time() + 2
        while (not player.seeks or player.seeks[-1] != 50) and time.time() < deadline:
            time.sleep(0.01)
    finally:
        thread.quit()
        thread.wait(2000)
    assert player.seeks[-1] == 50
    assert len(player.seeks) <= 3  # one per burst, give or take a flush racing the queue
