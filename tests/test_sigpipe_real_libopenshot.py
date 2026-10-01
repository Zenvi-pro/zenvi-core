"""With real libopenshot, a client resetting a local socket must not end the app.

libopenshot's CrashHandler -- installed by the first openshot.Timeline -- traps
SIGPIPE and exit()s with status 13, so the RC tester's repro (send a request,
reset the connection, server writes its reply) killed the editor. This pins,
against the real library, that TimelineSync's crash_handler.ignore_sigpipe()
call comes late enough: after the first Timeline, and that later Timelines and
readers do not reinstall the trap.

Each case runs in its own interpreter because the native handlers stay
installed for the life of the process. Skipped when libopenshot is not
importable (the headless CI job); run locally with, for example,
PYTHONPATH=$HOME/zenvi-deps-1.0/python.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="no SIGPIPE on Windows")

SCRIPT = textwrap.dedent(r"""
    import ctypes, json, os, signal, socket, struct, sys, time
    sys.path.insert(0, os.environ["ZENVI_SRC"])
    import openshot
    from classes import crash_handler

    def disposition():
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(512)
        libc.sigaction(signal.SIGPIPE, None, buf)
        return ctypes.c_void_p.from_buffer(buf).value or 0  # 0 SIG_DFL, 1 SIG_IGN

    def timeline():
        return openshot.Timeline(640, 360, openshot.Fraction(30, 1), 44100, 2, openshot.LAYOUT_STEREO)

    report = {"at_start": disposition()}
    keep = [timeline()]
    report["after_first_timeline"] = disposition()
    if os.environ["MODE"] == "fixed":
        report["ignored"] = crash_handler.ignore_sigpipe()
    report["after_fix"] = disposition()
    keep += [timeline(), timeline(), openshot.Clip(),
             openshot.DummyReader(openshot.Fraction(30, 1), 64, 64, 44100, 2, 1.0)]
    report["after_more_timelines_and_readers"] = disposition()
    print(json.dumps(report), flush=True)

    # The RC tester's repro: a client sends a request and resets the
    # connection (SO_LINGER 1,0) before the server writes its reply.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()
    client.sendall(b"GET /thumbnails/F1/7/path/no-cache/ HTTP/1.1\r\n\r\n")
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()
    time.sleep(0.2)
    errors = []
    for _ in range(3):  # Linux reports ECONNRESET first, then EPIPE (with SIGPIPE)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 65536)
        except OSError as ex:
            errors.append(type(ex).__name__)
        time.sleep(0.05)
    print(json.dumps({"write_errors": errors}), flush=True)
""")


def _run(mode):
    env = dict(os.environ, MODE=mode, ZENVI_SRC=str(SRC))
    return subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                          text=True, timeout=120)


def _reports(proc):
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


@pytest.fixture(scope="module", autouse=True)
def real_libopenshot():
    probe = subprocess.run([sys.executable, "-c", "import openshot"], capture_output=True, timeout=120)
    if probe.returncode != 0:
        pytest.skip("libopenshot is not importable here")


def test_a_reset_client_kills_the_process_without_the_fix():
    # Negative control: proves the repro still reaches libopenshot's trap.
    proc = _run("unfixed")
    first = _reports(proc)[0]

    assert first["at_start"] == 1  # Python's own SIG_IGN
    assert first["after_first_timeline"] not in (0, 1)  # CrashHandler took SIGPIPE
    assert proc.returncode == 13, proc.stderr[-2000:]
    assert "Caught signal 13" in proc.stderr


def test_ignore_sigpipe_after_the_first_timeline_keeps_the_process_alive():
    proc = _run("fixed")
    assert proc.returncode == 0, proc.stderr[-2000:]
    first, writes = _reports(proc)

    assert first["after_first_timeline"] not in (0, 1)
    assert first["ignored"] is True
    assert first["after_fix"] == 1
    assert first["after_more_timelines_and_readers"] == 1  # libopenshot installs its handlers once
    assert writes["write_errors"], "the reply to the reset client should have failed"
    assert set(writes["write_errors"]) <= {"BrokenPipeError", "ConnectionResetError"}
    assert "Caught signal" not in proc.stderr
