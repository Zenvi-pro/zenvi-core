"""Run one MainWindow lock-file scenario in a clean interpreter.

Importing windows.main_window has two prerequisites that cannot be met inside a
shared pytest session: QtWebEngineWidgets must be imported before any
QApplication exists, and get_app() must already return something shaped like
OpenShotApp (classes.metrics and several view modules read settings and _tr at
import time). Once another test module has created a plain QApplication, the
WebEngine import fails and windows/views/timeline.py picks a backend whose
metaclass can't compose with updates.UpdateInterface.

So the scenarios run out-of-process. Invoked by tests/test_crash_recovery.py.

usage: python _main_window_lock_probe.py <scenario>
Reads HOME from the environment; prints a single JSON object on stdout.
"""

import json
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import openshot  # noqa: E402,F401  (real bindings, before PyQt)
from PyQt5 import QtWebEngineWidgets  # noqa: E402,F401  (before any QApplication)
from PyQt5.QtWidgets import QApplication  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default

    def set(self, *args):
        pass


class _Project:
    def needs_save(self):
        return False


class _App(QApplication):
    def get_settings(self):
        return _Settings()

    def _tr(self, message):
        return message


def main():
    scenario = sys.argv[1]

    app = _App([])
    app.project = _Project()

    from classes import info
    from windows import main_window as mw

    os.makedirs(info.USER_PATH, exist_ok=True)
    lock = os.path.join(info.USER_PATH, ".lock")
    MainWindow = mw.MainWindow
    result = {"ran": False, "error": None}

    if scenario == "recovery_raises":
        # create_lock_file() runs inside MainWindow.__init__; a raising recovery
        # there used to take the whole launch down.
        with open(lock, "w") as fh:
            fh.write("stale")

        def _boom():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        mw.exceptions.libopenshot_crash_recovery = _boom

        class _Stub:
            destroy_lock_file = MainWindow.destroy_lock_file

            def clear_temporary_files(self):
                pass

        MainWindow.create_lock_file(_Stub())

    else:
        with open(lock, "w") as fh:
            fh.write("held")

        ran = []

        class _Stub:
            shutting_down = (scenario == "already_shutting_down")
            tutorial_manager = None
            _restart_for_update = False
            destroy_lock_file = MainWindow.destroy_lock_file

            def _shutdown_sequence(self, app):
                ran.append(True)
                if scenario == "shutdown_raises":
                    raise RuntimeError("preview thread refused to stop")

        MainWindow.closeEvent(_Stub(), object())
        result["ran"] = bool(ran)

    result["lock_exists"] = os.path.exists(lock)
    result["lock_text"] = ""
    if result["lock_exists"]:
        with open(lock) as fh:
            result["lock_text"] = fh.read()
    return result


if __name__ == "__main__":
    try:
        out = main()
        out["ok"] = True
    except Exception as exc:
        out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    print("PROBE:" + json.dumps(out))
