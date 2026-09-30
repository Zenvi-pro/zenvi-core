"""One desktop window per profile: a second launch hands its paths to the first.

``launch.py cut.zvn`` while Zenvi is already open (the zenvi CLI's ``zenvi open``,
a second terminal, a file-manager double-click on Windows or Linux) connects to
the running window's local socket, hands over the path and exits 0. The window
comes forward and opens the project through File > Open's code path, including
its unsaved-changes prompt. macOS delivers the same request to a running bundle
as a QFileOpenEvent instead (see OpenShotApp.event).

The socket name is derived from the user and the profile directory
(``~/.openshot_qt``), so different users and different HOMEs never hand off to
each other. Only the desktop window listens; a ``--headless`` session never
does, so neither mode can take the other's requests.

Wire format, one JSON line each way over QLocalSocket::

    launch -> window: {"v": 1, "paths": ["/abs/cut.zvn"]}
    window -> launch: {"ok": true}   or   {"ok": false, "error": "..."}

An empty ``paths`` just brings the window forward.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import sys

from qt_api import QObject, QTimer

# A module logger, not classes.logger: a second launch hands off and exits
# before the app's logging is set up, and must not open the shared log file.
log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
# Nothing listening fails at once; this only bounds a busy named pipe on
# Windows, so a first launch never waits noticeably.
CONNECT_TIMEOUT_MS = 200
# How long a second launch waits for the window to acknowledge. The window
# answers from its event loop, so a busy one (mid-export) can be slow.
REPLY_TIMEOUT_MS = 5000
MAX_MESSAGE_BYTES = 64 * 1024

# hand_off() outcomes
DELIVERED = "delivered"
NOT_RUNNING = "not-running"
NO_REPLY = "no-reply"
REFUSED = "refused"


def server_name(user_dir: str) -> str:
    """Local socket name of the desktop window for this user and profile.

    Short on purpose: on macOS/Linux it becomes a socket file under the temp
    directory, and those paths are limited to ~104 bytes.
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    key = "%s\0%s" % (user, os.path.normcase(os.path.abspath(user_dir)))
    return "zenvi-gui-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def launch_paths(positional, project=None, cwd=None) -> list:
    """Absolute paths a launch was asked to open (the window has its own cwd)."""
    base = cwd or os.getcwd()
    items = ([project] if project else []) + [p for p in (positional or []) if p and not p.startswith("-")]
    return [os.path.abspath(os.path.join(base, os.path.expanduser(p))) for p in items]


def split_launch_paths(paths, project_exts):
    """(project, media) for a list of handed-over paths, read like a command
    line: a project given first is opened, otherwise everything is media to
    import into the open project."""
    paths = list(paths)
    if paths and paths[0].endswith(tuple(project_exts)):
        return paths[0], []
    return None, paths


def encode_request(paths) -> bytes:
    return (json.dumps({"v": PROTOCOL_VERSION, "paths": list(paths)}) + "\n").encode("utf-8")


def decode_request(line: bytes) -> list:
    """The paths in one request line; ValueError when it is not one."""
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict) or data.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported handoff request")
    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("handoff request needs a list of paths")
    return paths


def encode_reply(ok: bool, error: str = "") -> bytes:
    reply = {"ok": True} if ok else {"ok": False, "error": error}
    return (json.dumps(reply) + "\n").encode("utf-8")


def decode_reply(line: bytes):
    """(ok, error) from one reply line; ValueError when it is not one."""
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise ValueError("unrecognised handoff reply")
    return data["ok"], str(data.get("error") or "")


class LineBuffer:
    """Collects socket data until one newline-terminated message is complete."""

    def __init__(self, limit=MAX_MESSAGE_BYTES):
        self._data = b""
        self._limit = limit

    def feed(self, chunk: bytes):
        """The complete line (without its newline) once it has arrived, else None."""
        self._data += chunk
        line, newline, _rest = self._data.partition(b"\n")
        if newline:
            return line
        if len(self._data) > self._limit:
            raise ValueError("handoff message too long")
        return None


def _new_socket():
    from qt_api import QLocalSocket
    return QLocalSocket()


def _allow_foreground_window():
    """Windows only lets the foreground process move another window to the
    front. The launch the user just started is foreground, so it passes that
    right on; elsewhere this is a no-op."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        log.debug("AllowSetForegroundWindow failed", exc_info=True)


def is_running(name: str, socket_factory=_new_socket) -> bool:
    """True when a window is listening on *name* (a stale socket is not)."""
    sock = socket_factory()
    sock.connectToServer(name)
    connected = bool(sock.waitForConnected(CONNECT_TIMEOUT_MS))
    if connected:
        sock.disconnectFromServer()
    return connected


def hand_off(name: str, paths, socket_factory=_new_socket,
             connect_timeout_ms=CONNECT_TIMEOUT_MS, reply_timeout_ms=REPLY_TIMEOUT_MS):
    """Give *paths* to the window listening on *name*.

    Runs before this process has a QApplication, with blocking socket calls.
    Returns (outcome, detail): DELIVERED, NOT_RUNNING (nothing listens, or only
    a crashed instance's socket is left), NO_REPLY (it accepted the connection
    but did not answer in time) or REFUSED (it answered with an error).
    """
    sock = socket_factory()
    sock.connectToServer(name)
    if not sock.waitForConnected(connect_timeout_ms):
        return NOT_RUNNING, ""
    _allow_foreground_window()
    try:
        sock.write(encode_request(paths))
        if not sock.waitForBytesWritten(reply_timeout_ms):
            return NO_REPLY, "the request could not be sent"
        buffer = LineBuffer()
        line = None
        while line is None:
            if not sock.waitForReadyRead(reply_timeout_ms):
                return NO_REPLY, "no answer within %.0f s" % (reply_timeout_ms / 1000.0)
            line = buffer.feed(bytes(sock.readAll()))
        ok, error = decode_reply(line)
    except ValueError as exc:
        return REFUSED, str(exc)
    finally:
        sock.disconnectFromServer()
    return (DELIVERED, "") if ok else (REFUSED, error)


class InstanceServer(QObject):
    """The desktop window's end of the handoff.

    Event-driven on the GUI thread (readyRead, never waitFor*), and it answers
    before acting, so a launch is never kept waiting on a prompt the window
    shows while opening the project.
    """

    def __init__(self, name, on_paths, parent=None):
        super().__init__(parent)
        self._name = name
        self._on_paths = on_paths
        self._server = None

    def listen(self) -> bool:
        from qt_api import QLocalServer
        # Checked first because listen() cannot be trusted to refuse: with an
        # access option set, Qt binds in a temporary directory and renames the
        # socket over the name, silently orphaning a live window's server.
        if is_running(self._name):
            log.warning("Another Zenvi window already listens on %s; this one will not "
                        "receive other launches' files", self._name)
            return False
        # Whatever is left under the name belongs to a crashed instance.
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._name):
            log.warning("Not listening for other launches on %s: %s",
                        self._name, server.errorString())
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def close(self):
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_new_connection(self):
        while self._server is not None and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is not None:
                _Connection(sock, self._on_paths)


class _Connection:
    """One launch's request: read a line, answer it, hand the paths over."""

    def __init__(self, sock, on_paths):
        self._sock = sock
        self._on_paths = on_paths
        self._buffer = LineBuffer()
        self._answered = False
        # A client that never finishes its line must not hold the socket open;
        # parented to the socket so it dies with it.
        self._timeout = QTimer(sock)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(sock.abort)
        self._timeout.start(REPLY_TIMEOUT_MS)
        sock.disconnected.connect(sock.deleteLater)
        # A lambda, not the bound method: PyQt holds a connected lambda (and so
        # this object) for as long as the socket lives, but may only weakly
        # reference a plain object's bound method.
        sock.readyRead.connect(lambda: self._read())
        if sock.bytesAvailable():
            self._read()

    def _read(self):
        if self._answered:
            return
        try:
            line = self._buffer.feed(bytes(self._sock.readAll()))
            if line is None:
                return
            paths = decode_request(line)
        except ValueError as exc:
            self._answer(encode_reply(False, str(exc)))
            return
        self._answer(encode_reply(True))
        on_paths = self._on_paths
        QTimer.singleShot(0, lambda: on_paths(paths))

    def _answer(self, reply):
        self._answered = True
        self._timeout.stop()
        self._sock.write(reply)
        self._sock.flush()
        self._sock.disconnectFromServer()
