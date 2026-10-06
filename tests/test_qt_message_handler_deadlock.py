"""Qt warnings must not run Python on the thread that emitted them.

The editor froze ("Not Responding") right after a title was added. Backtraces of
the hung process: the thumbnail thread, rendering the title's SVG, was inside
Qt's font code holding the font-database lock when Qt emitted a warning
("QObject::startTimer: Timers can only be used with threads started with
QThread"); the Python message handler then waited for the GIL. The GUI thread
held the GIL and was waiting for that same lock in QFontMetrics. The preview
player drawing a Caption was queued on the lock behind them.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import app  # noqa: E402


def test_warnings_are_filtered_inside_qt_before_the_python_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "QLoggingCategory",
                        type("L", (), {"setFilterRules": staticmethod(lambda rules: calls.append(("rules", rules)))}))
    monkeypatch.setattr(app, "qInstallMessageHandler", lambda handler: calls.append(("handler", handler)))

    app._install_qt_message_handler()

    assert calls[0] == ("rules", "default.warning=false")
    assert calls[1] == ("handler", app._qt_message_handler)
