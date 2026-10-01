"""The single-instance handoff over a real QLocalServer / QLocalSocket.

Run with ZENVI_REAL_QT=1 (auto-skipped under the headless Qt stub). The second
launch's blocking client runs on a worker thread while this thread spins the
window's event loop, as the two processes would.
"""

import os
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("PyQt5.QtNetwork")

from PyQt5.QtCore import QDir  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from classes import single_instance as si  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    # A QApplication, not a QCoreApplication: other real-Qt modules in the same
    # session need widgets, and there can be only one application object.
    return QApplication.instance() or QApplication([])


@pytest.fixture
def name(tmp_path):
    # Derived from a per-test directory, so parallel tests never share a socket.
    return si.server_name(str(tmp_path / ".openshot_qt"))


def _spin_until(qapp, done, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return done()


def _hand_off_in_background(name, paths):
    box = {}
    worker = threading.Thread(target=lambda: box.update(result=si.hand_off(name, paths)), daemon=True)
    worker.start()
    return worker, box


def test_second_launch_hands_its_project_to_the_window(qapp, name):
    received = []
    server = si.InstanceServer(name, received.append)
    assert server.listen()
    try:
        worker, box = _hand_off_in_background(name, ["/Movies/cut.zvn"])
        assert _spin_until(qapp, lambda: not worker.is_alive() and received)
        assert box["result"] == (si.DELIVERED, "")
        assert received == [["/Movies/cut.zvn"]]
    finally:
        server.close()


def test_nothing_listening_is_quick(qapp, name):
    started = time.monotonic()
    assert si.hand_off(name, ["/a.zvn"]) == (si.NOT_RUNNING, "")
    assert time.monotonic() - started < 1.0


def test_a_live_window_keeps_its_name(qapp, name):
    first = si.InstanceServer(name, lambda paths: None)
    assert first.listen()
    try:
        second = si.InstanceServer(name, lambda paths: None)
        assert second.listen() is False  # does not steal the first one's socket
        assert si.is_running(name)
    finally:
        first.close()
    assert not si.is_running(name)


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes vanish with their process")
def test_a_crashed_windows_socket_does_not_block_the_next_one(qapp, name):
    # What a SIGKILLed window leaves behind: a socket file nobody listens on.
    path = os.path.join(QDir.tempPath(), name)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(path)
    leftover.close()
    try:
        assert os.path.exists(path)
        assert si.hand_off(name, ["/a.zvn"]) == (si.NOT_RUNNING, "")

        received = []
        server = si.InstanceServer(name, received.append)
        assert server.listen()  # took the name over
        try:
            worker, box = _hand_off_in_background(name, [])
            assert _spin_until(qapp, lambda: not worker.is_alive() and received)
            assert box["result"] == (si.DELIVERED, "")
            assert received == [[]]
        finally:
            server.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_garbage_is_refused_and_nothing_is_opened(qapp, name):
    from PyQt5.QtNetwork import QLocalSocket

    received = []
    server = si.InstanceServer(name, received.append)
    assert server.listen()
    try:
        box = {}

        def client():
            sock = QLocalSocket()
            sock.connectToServer(name)
            assert sock.waitForConnected(1000)
            sock.write(b"GET / HTTP/1.1\n")
            sock.waitForBytesWritten(1000)
            data = b""
            while b"\n" not in data and sock.waitForReadyRead(3000):
                data += bytes(sock.readAll())
            box["reply"] = data

        worker = threading.Thread(target=client, daemon=True)
        worker.start()
        assert _spin_until(qapp, lambda: not worker.is_alive())
        ok, error = si.decode_reply(box["reply"].rstrip(b"\n"))
        assert ok is False and error
        assert received == []
    finally:
        server.close()
