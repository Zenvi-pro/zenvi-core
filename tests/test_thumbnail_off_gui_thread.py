"""Thumbnails never block the GUI thread, and a client that hangs up cannot end the app.

Regression tests for two v1.2.0 RC bugs:

* Importing long-GOP, stream-copy-trimmed media froze the UI for over a
  minute: the Project Files model called GetThumbPath -- a blocking HTTP
  request, with no timeout, to the local thumbnail server, which decodes the
  frame first -- on the GUI thread.
* One client resetting its connection to the thumbnail server killed the app
  with exit code 13: libopenshot's CrashHandler traps SIGPIPE and exit()s.

Headless (stubbed Qt). The real-Qt Project Files flow lives in
test_files_model_thumbnails.py and the real-libopenshot SIGPIPE check in
test_sigpipe_real_libopenshot.py.
"""

from __future__ import annotations

import ast
import collections
import ctypes
import importlib.util
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import classes.thumbnail as thumbnail  # noqa: E402
from classes import crash_handler  # noqa: E402

WORKER_MODULE = "windows/views/timeline_backend/qwidget/thumbnails.py"
POSIX_ONLY = pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="no SIGPIPE on Windows")


def _load_worker_module():
    """Load the thumbnail worker without its package (the native timeline needs real Qt).

    QTimer.singleShot only records the drain it would schedule; the tests run
    it by hand.
    """
    spec = importlib.util.spec_from_file_location("_thumbnail_worker_under_test", SRC / WORKER_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.QTimer = SimpleNamespace(singleShot=lambda _msec, _callback: None)
    return module


def _os_sigpipe_handler():
    """The process's real SIGPIPE disposition (0 = SIG_DFL, 1 = SIG_IGN, else a handler).

    signal.getsignal() only reports what Python last set; libopenshot installs
    its handler with sigaction() behind Python's back, so ask the OS.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    buf = ctypes.create_string_buffer(512)  # larger than struct sigaction on macOS and glibc
    if libc.sigaction(signal.SIGPIPE, None, buf) != 0:
        raise OSError(ctypes.get_errno(), "sigaction")
    # sa_handler is the first member on both
    return ctypes.c_void_p.from_buffer(buf).value or 0


@pytest.fixture
def sigpipe_restored():
    yield
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # Python's own default


# ---------------------------------------------------------------- GetThumbPath

@pytest.fixture
def thumb_server_app(monkeypatch):
    app = SimpleNamespace(window=SimpleNamespace(
        http_server_thread=SimpleNamespace(server_address=("127.0.0.1", 9))))
    monkeypatch.setattr(thumbnail, "get_app", lambda: app)
    monkeypatch.setattr(thumbnail.time, "sleep", lambda _s: None)


def test_get_thumb_path_waits_a_bounded_time(monkeypatch, thumb_server_app):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, text="/thumbs/F1/7.png", status_code=200)

    monkeypatch.setattr(thumbnail, "get", fake_get)

    assert thumbnail.GetThumbPath("F1", 7) == "/thumbs/F1/7.png"
    connect, read = calls[0]["timeout"]
    assert 0 < connect <= 10
    assert 0 < read <= 600


def test_get_thumb_path_does_not_ask_again_after_a_timeout(monkeypatch, thumb_server_app):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        raise requests.exceptions.ReadTimeout("server still decoding")

    monkeypatch.setattr(thumbnail, "get", fake_get)

    # A retry would only start a second decode of the same frame.
    assert thumbnail.GetThumbPath("F1", 7, attempts=3) == ""
    assert len(calls) == 1


def test_get_thumb_path_still_retries_quick_failures(monkeypatch, thumb_server_app):
    replies = [requests.exceptions.ConnectionError("refused"),
               SimpleNamespace(ok=True, text="", status_code=200),
               SimpleNamespace(ok=True, text="/thumbs/F1/7.png", status_code=200)]

    def fake_get(url, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(thumbnail, "get", fake_get)

    assert thumbnail.GetThumbPath("F1", 7, attempts=3) == "/thumbs/F1/7.png"
    assert replies == []


# ------------------------------------------------ nothing else asks the server

def _get_thumb_path_callers():
    callers = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("tests/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "GetThumbPath" not in text:
            continue
        tree = ast.parse(text, rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "GetThumbPath":
                    callers.add(rel)
    return callers


def test_only_the_thumbnail_worker_asks_the_thumbnail_server():
    # GetThumbPath waits while the server decodes a frame (a minute or more on
    # long-GOP media). Views get thumbnails from the worker -- through
    # FilesModel.request_thumbnail / thumbnail_icon -- never by calling it.
    assert _get_thumb_path_callers() == {WORKER_MODULE}


# ------------------------------------------------------------ worker queueing

def _jobs(*jobs):
    return [tuple(job) for job in jobs]


def test_worker_keeps_one_job_per_slot_and_never_drops_a_force_refresh():
    worker = _load_worker_module()._ThumbnailWorker(max_pending=None)

    worker.enqueue_batch(_jobs(("F1", "F1", 7, 0, True)))   # FileUpdated: regenerate
    worker.enqueue_batch(_jobs(("F2", "F2", 1, 0, False)))
    worker.enqueue_batch(_jobs(("F1", "F1", 7, 0, False)))  # a plain request for the same slot

    assert list(worker._queue) == [("F2", "F2", 1, 0, False), ("F1", "F1", 7, 0, True)]


def test_project_files_worker_keeps_every_request():
    module = _load_worker_module()
    jobs = _jobs(*[("F%d" % i, "F%d" % i, 1, 0, False) for i in range(200)])

    unbounded = module._ThumbnailWorker(max_pending=None)
    unbounded.enqueue_batch(jobs)
    timeline = module._ThumbnailWorker()
    timeline.enqueue_batch(jobs)

    # Rows are not repainted into asking again, so a dropped request would
    # leave a file on its placeholder for good.
    assert len(unbounded._queue) == 200
    assert len(timeline._queue) == module._MAX_PENDING_JOBS


def test_manager_coalescing_keeps_a_pending_force_refresh():
    module = _load_worker_module()
    manager = SimpleNamespace(_pending=collections.OrderedDict(), _max_pending=None, _emit_scheduled=True)

    module.TimelineThumbnailManager.request_thumbnail(manager, "F1", "F1", 7, 0, clear_cache=True)
    module.TimelineThumbnailManager.request_thumbnail(manager, "F1", "F1", 7, 0)

    assert list(manager._pending.values()) == [("F1", "F1", 7, 0, True)]


def test_worker_passes_the_retry_budget_to_get_thumb_path(monkeypatch):
    module = _load_worker_module()
    calls = []
    monkeypatch.setattr(module, "existing_thumb_path", lambda *a, **k: "")
    monkeypatch.setattr(module, "prewarmed_thumb_path", lambda *a, **k: "")
    monkeypatch.setattr(module, "GetThumbPath",
                        lambda file_id, frame, **kwargs: calls.append((file_id, frame, kwargs)) or "")

    worker = module._ThumbnailWorker(max_pending=None, attempts=3)
    worker.enqueue_batch(_jobs(("F1", "F1", 7, 0, False), ("F2", "F2", 1, 0, True)))
    worker._process_next()

    assert calls == [("F1", 7, {"attempts": 3}), ("F2", 1, {"clear_cache": True, "attempts": 3})]


# ------------------------------------------------------- reset client, SIGPIPE

@pytest.fixture
def thumbnail_server(monkeypatch, tmp_path):
    """A real thumbnail server whose file lookup waits until the test says go."""
    thumb = tmp_path / "7.png"
    thumb.write_bytes(b"\x89PNG not really")
    entered = threading.Event()
    go = threading.Event()
    served = []

    def fake_file_get(**kwargs):
        entered.set()  # the request line and headers have been read
        go.wait(10)
        served.append(kwargs.get("id"))
        if kwargs.get("id") != "F1":
            return None
        return SimpleNamespace(data={"media_type": "video"}, absolute_path=lambda: str(tmp_path / "clip.mp4"))

    monkeypatch.setattr(thumbnail.File, "get", staticmethod(fake_file_get))
    monkeypatch.setattr(thumbnail, "resolve_thumbnail_path", lambda *a, **k: str(thumb))
    monkeypatch.setattr(thumbnail, "preferred_thumbnail_path", lambda *a, **k: str(thumb))
    monkeypatch.setattr(thumbnail, "GenerateThumbnail", lambda *a, **k: None)

    server = thumbnail.httpThumbnailServer(("127.0.0.1", 0), thumbnail.httpThumbnailHandler)
    server.daemon_threads = True
    errors = []
    server.handle_error = lambda request, address: errors.append(sys.exc_info()[1])
    loop = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    loop.start()
    try:
        yield SimpleNamespace(port=server.server_address[1], entered=entered, go=go,
                              errors=errors, served=served, thumb=thumb)
    finally:
        go.set()
        server.shutdown()
        server.server_close()


def test_thumbnail_server_shrugs_off_a_client_that_resets(thumbnail_server):
    # The RC tester's repro: send a request, then close with RST (SO_LINGER 1,0)
    # while the server is still working on it.
    client = socket.create_connection(("127.0.0.1", thumbnail_server.port), timeout=5)
    client.sendall(b"GET /thumbnails/F1/7/path/no-cache/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert thumbnail_server.entered.wait(10)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()
    time.sleep(0.2)  # let the RST land before the handler writes its reply
    thumbnail_server.go.set()

    deadline = time.monotonic() + 10
    while not thumbnail_server.served and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)  # the handler writes (and hits the dead socket) after the lookup

    # The write to the reset socket raised inside the handler; it must not
    # reach the server's error reporting (a traceback on stderr per client).
    assert thumbnail_server.served == ["F1"]
    assert thumbnail_server.errors == []

    # ...and the server keeps serving.
    reply = requests.get("http://127.0.0.1:%d/thumbnails/F1/7/path/" % thumbnail_server.port, timeout=10)
    assert reply.ok and reply.text == str(thumbnail_server.thumb)
    assert thumbnail_server.errors == []


@POSIX_ONLY
def test_ignore_sigpipe_takes_sigpipe_back_from_a_native_handler(sigpipe_restored):
    # Stand-in for libopenshot's CrashHandler: any handler that is not SIG_IGN
    signal.signal(signal.SIGPIPE, lambda *_: None)
    assert _os_sigpipe_handler() not in (0, 1)

    assert crash_handler.ignore_sigpipe() is True
    assert _os_sigpipe_handler() == 1  # SIG_IGN: EPIPE now surfaces as BrokenPipeError


def test_ignore_sigpipe_is_a_no_op_without_sigpipe(monkeypatch):
    windows_signal = SimpleNamespace(SIG_IGN=signal.SIG_IGN, signal=lambda *a: pytest.fail("no SIGPIPE to set"))
    monkeypatch.setattr(crash_handler, "signal", windows_signal)
    assert crash_handler.ignore_sigpipe() is False


@POSIX_ONLY
def test_ignore_sigpipe_off_the_main_thread_reports_instead_of_raising(sigpipe_restored):
    result = []
    worker = threading.Thread(target=lambda: result.append(crash_handler.ignore_sigpipe()))
    worker.start()
    worker.join(10)
    assert result == [False]


@POSIX_ONLY
def test_timeline_sync_ignores_sigpipe_after_creating_its_timeline(monkeypatch, sigpipe_restored):
    import classes.timeline as timeline_module

    class CrashHandlerInstallingTimeline:
        """openshot.Timeline's constructor calls CrashHandler::Instance(), which traps SIGPIPE."""

        def __init__(self, *args):
            signal.signal(signal.SIGPIPE, lambda *_: None)
            self.info = SimpleNamespace()

        def Open(self):
            pass

    project = {"fps": {"num": 30, "den": 1}, "width": 1280, "height": 720,
               "sample_rate": 48000, "channels": 2, "channel_layout": 3}
    app = SimpleNamespace(project=SimpleNamespace(get=project.get),
                          updates=SimpleNamespace(add_listener=lambda *a: None))
    monkeypatch.setattr(timeline_module, "get_app", lambda: app)
    monkeypatch.setattr(timeline_module.openshot, "Timeline", CrashHandlerInstallingTimeline, raising=False)
    monkeypatch.setattr(timeline_module.openshot, "Fraction", lambda num, den: (num, den), raising=False)

    timeline_module.TimelineSync(SimpleNamespace(MaxSizeChanged=SimpleNamespace(connect=lambda *a: None)))

    # Reset *after* the Timeline exists: before it, libopenshot would override it.
    assert _os_sigpipe_handler() == 1
