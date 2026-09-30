"""Second launch -> running window handoff (classes.single_instance), headless.

The socket is a fake with QLocalSocket's blocking API; tests/test_single_instance_qt.py
runs the same exchange over a real QLocalServer/QLocalSocket.
"""

import json
import os
import types

import pytest

from classes import single_instance as si


class FakeSocket:
    """QLocalSocket's blocking client API, scripted."""

    def __init__(self, connects=True, replies=(), writes_ok=True):
        self.connects = connects
        self.replies = list(replies)  # chunks returned by successive reads
        self.writes_ok = writes_ok
        self.written = b""
        self.server = None
        self.disconnected = False

    def connectToServer(self, name):
        self.server = name

    def waitForConnected(self, _ms):
        return self.connects

    def write(self, data):
        self.written += bytes(data)
        return len(data)

    def waitForBytesWritten(self, _ms):
        return self.writes_ok

    def waitForReadyRead(self, _ms):
        return bool(self.replies)

    def readAll(self):
        return self.replies.pop(0)

    def disconnectFromServer(self):
        self.disconnected = True


# --- naming ------------------------------------------------------------------

def test_server_name_is_per_profile_short_and_stable(tmp_path):
    a = si.server_name(str(tmp_path / "home-a" / ".openshot_qt"))
    b = si.server_name(str(tmp_path / "home-b" / ".openshot_qt"))
    assert a == si.server_name(str(tmp_path / "home-a" / ".openshot_qt"))
    assert a != b
    assert a.startswith("zenvi-gui-")
    # It becomes a socket file under $TMPDIR on macOS/Linux (~104-byte limit).
    assert len(a) <= 32
    assert all(c.isalnum() or c == "-" for c in a)


def test_server_name_differs_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr(si.getpass, "getuser", lambda: "alice")
    alice = si.server_name(str(tmp_path))
    monkeypatch.setattr(si.getpass, "getuser", lambda: "bob")
    assert si.server_name(str(tmp_path)) != alice


def test_launch_paths_are_absolute_for_the_other_process(tmp_path):
    paths = si.launch_paths(["cut.zvn", "-style", "clips/a.mp4"], project=None, cwd=str(tmp_path))
    assert paths == [str(tmp_path / "cut.zvn"), str(tmp_path / "clips" / "a.mp4")]
    assert si.launch_paths([], project="p.zvn", cwd=str(tmp_path)) == [str(tmp_path / "p.zvn")]
    assert si.launch_paths(None) == []


# --- wire format --------------------------------------------------------------

def test_request_round_trip():
    line = si.encode_request(["/a/cut.zvn"])
    assert line.endswith(b"\n")
    assert si.decode_request(line.rstrip(b"\n")) == ["/a/cut.zvn"]
    assert si.decode_request(si.encode_request([]).rstrip(b"\n")) == []


@pytest.mark.parametrize("line", [
    b"not json",
    b'{"v": 2, "paths": []}',
    b'{"v": 1}',
    b'{"v": 1, "paths": "cut.zvn"}',
    b'{"v": 1, "paths": [""]}',
    b'[1]',
])
def test_bad_requests_are_rejected(line):
    with pytest.raises(ValueError):
        si.decode_request(line)


def test_reply_round_trip():
    assert si.decode_reply(si.encode_reply(True).rstrip(b"\n")) == (True, "")
    assert si.decode_reply(si.encode_reply(False, "nope").rstrip(b"\n")) == (False, "nope")
    with pytest.raises(ValueError):
        si.decode_reply(b'{"ok": "yes"}')


def test_line_buffer_waits_for_the_newline_and_bounds_the_message():
    buf = si.LineBuffer()
    assert buf.feed(b'{"v": 1, ') is None
    assert buf.feed(b'"paths": []}\nleftover') == b'{"v": 1, "paths": []}'

    small = si.LineBuffer(limit=8)
    with pytest.raises(ValueError):
        small.feed(b"123456789")


# --- the second launch's side -------------------------------------------------------

def test_nothing_listening_means_start_normally():
    sock = FakeSocket(connects=False)
    assert si.hand_off("zenvi-gui-x", ["/a.zvn"], socket_factory=lambda: sock) == (si.NOT_RUNNING, "")
    assert sock.written == b""  # a stale socket file never gets a request


def test_delivered_when_the_window_acknowledges():
    sock = FakeSocket(replies=[si.encode_reply(True)])
    outcome = si.hand_off("zenvi-gui-x", ["/a/cut.zvn"], socket_factory=lambda: sock)
    assert outcome == (si.DELIVERED, "")
    assert sock.server == "zenvi-gui-x"
    assert json.loads(sock.written) == {"v": 1, "paths": ["/a/cut.zvn"]}
    assert sock.disconnected


def test_reply_split_across_reads():
    reply = si.encode_reply(True)
    sock = FakeSocket(replies=[reply[:3], reply[3:]])
    assert si.hand_off("n", [], socket_factory=lambda: sock)[0] == si.DELIVERED


def test_no_reply_is_reported_not_retried_as_a_new_window():
    sock = FakeSocket(replies=[])
    outcome, detail = si.hand_off("n", ["/a.zvn"], socket_factory=lambda: sock, reply_timeout_ms=10)
    assert outcome == si.NO_REPLY
    assert "no answer" in detail
    assert sock.disconnected


def test_unsent_request_is_no_reply():
    sock = FakeSocket(writes_ok=False)
    assert si.hand_off("n", ["/a.zvn"], socket_factory=lambda: sock)[0] == si.NO_REPLY


def test_refusal_and_garbage_replies_are_refused():
    sock = FakeSocket(replies=[si.encode_reply(False, "bad request")])
    assert si.hand_off("n", [], socket_factory=lambda: sock) == (si.REFUSED, "bad request")
    sock = FakeSocket(replies=[b"HTTP/1.1 400\n"])
    assert si.hand_off("n", [], socket_factory=lambda: sock)[0] == si.REFUSED


def test_is_running_distinguishes_a_stale_socket():
    live = FakeSocket(connects=True)
    assert si.is_running("n", socket_factory=lambda: live) is True
    assert live.disconnected
    assert si.is_running("n", socket_factory=lambda: FakeSocket(connects=False)) is False


# --- the window's side (one connection) ---------------------------------------------

class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self):
        for slot in list(self.slots):
            slot()


class FakeServerSocket:
    """The server-side QLocalSocket a _Connection drives."""

    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.written = b""
        self.readyRead = FakeSignal()
        self.disconnected = FakeSignal()
        self.closed = False

    def bytesAvailable(self):
        return len(self.incoming)

    def readAll(self):
        data, self.incoming = self.incoming, b""
        return data

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def disconnectFromServer(self):
        self.closed = True

    def abort(self):
        self.closed = True

    def deleteLater(self):
        pass


@pytest.fixture
def immediate_timers(monkeypatch):
    """QTimer whose single shots run at once (the stub's does nothing)."""
    class _Timer:
        def __init__(self, *_a):
            self.timeout = FakeSignal()

        def setSingleShot(self, _v):
            pass

        def start(self, _ms):
            pass

        def stop(self):
            pass

        @staticmethod
        def singleShot(_ms, fn):
            fn()

    monkeypatch.setattr(si, "QTimer", _Timer)


def test_connection_answers_then_hands_over(immediate_timers):
    got = []
    sock = FakeServerSocket()
    si._Connection(sock, got.append)
    sock.incoming = si.encode_request(["/a/cut.zvn"])
    sock.readyRead.emit()

    assert si.decode_reply(sock.written.rstrip(b"\n")) == (True, "")
    assert sock.closed
    assert got == [["/a/cut.zvn"]]

    # Anything after the answer is ignored.
    sock.incoming = si.encode_request(["/b.zvn"])
    sock.readyRead.emit()
    assert got == [["/a/cut.zvn"]]


def test_connection_reads_data_that_arrived_before_it_existed(immediate_timers):
    got = []
    sock = FakeServerSocket(incoming=si.encode_request([]))
    si._Connection(sock, got.append)
    assert got == [[]]


def test_connection_rejects_a_bad_request_without_acting(immediate_timers):
    got = []
    sock = FakeServerSocket(incoming=b'{"v": 9}\n')
    si._Connection(sock, got.append)
    ok, error = si.decode_reply(sock.written.rstrip(b"\n"))
    assert ok is False and "unsupported" in error
    assert got == []


# --- the app routes handoffs to the window once it is up ------------------------------

def _fake_app():
    from classes.app import OpenShotApp

    delivered = []
    app = types.SimpleNamespace(
        _external_requests=[], _external_paths_ready=False,
        window=types.SimpleNamespace(open_external_paths=delivered.append))
    app._deliver_external_paths = lambda: OpenShotApp._deliver_external_paths(app)
    app.open_external_paths = lambda paths: OpenShotApp.open_external_paths(app, paths)
    return app, delivered


def test_handoffs_wait_for_the_main_window():
    app, delivered = _fake_app()
    app.open_external_paths(["/a.zvn"])
    app.open_external_paths([])
    assert delivered == []

    app._external_paths_ready = True
    app._deliver_external_paths()
    assert delivered == [["/a.zvn"], []]

    app.open_external_paths(["/b.zvn"])
    assert delivered == [["/a.zvn"], [], ["/b.zvn"]]


def test_macos_file_open_event_is_a_handoff(monkeypatch):
    import classes.app as app_mod

    file_open = object()
    monkeypatch.setattr(app_mod, "QEvent", types.SimpleNamespace(FileOpen=file_open))
    opened = []
    app = types.SimpleNamespace(headless=False, open_external_paths=opened.append)
    event = types.SimpleNamespace(type=lambda: file_open, file=lambda: "/Movies/cut.zvn")
    assert app_mod.OpenShotApp.event(app, event) is True
    assert opened == [["/Movies/cut.zvn"]]

    # A headless session is not the window macOS means.
    app.headless = True
    assert app_mod.OpenShotApp.event(app, event) is True
    assert opened == [["/Movies/cut.zvn"]]


def test_handed_over_paths_read_like_a_command_line():
    exts = (".zvn", ".osp", ".flow")
    assert si.split_launch_paths(["/a/cut.zvn", "/a/b.mp4"], exts) == ("/a/cut.zvn", [])
    assert si.split_launch_paths(["/a/old.osp"], exts) == ("/a/old.osp", [])
    assert si.split_launch_paths(["/a/b.mp4", "/a/c.wav"], exts) == (None, ["/a/b.mp4", "/a/c.wav"])
    assert si.split_launch_paths([], exts) == (None, [])


def test_launch_paths_resolve_against_this_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert si.launch_paths(["x.zvn"]) == [os.path.join(str(tmp_path), "x.zvn")]
