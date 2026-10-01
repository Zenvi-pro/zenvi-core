"""Index panel — timeline transcript browser (same store as agent tools)."""

from __future__ import annotations

import logging

log = logging.getLogger("windows.index_panel")

try:
    from qt_api import Qt
    from qt_api import (
        QDockWidget,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except Exception:  # headless / stub
    Qt = None  # type: ignore
    QDockWidget = object  # type: ignore
    QWidget = object  # type: ignore


class IndexPanel(QDockWidget if QDockWidget is not object else object):
    """Dockable transcript list. Click a word to seek the playhead."""

    def __init__(self, parent=None):
        if QDockWidget is object:
            return
        super().__init__("Index", parent)
        self.setObjectName("dockIndex")
        self._app = None
        try:
            from classes.app import get_app
            self._app = get_app()
        except Exception:
            pass

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)

        row = QHBoxLayout()
        self._status = QLabel("No transcript loaded.")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self._status, 1)
        row.addWidget(self._refresh_btn, 0)
        layout.addLayout(row)

        self._list = QListWidget()
        self._list.itemActivated.connect(self._on_item)
        self._list.itemClicked.connect(self._on_item)
        layout.addWidget(self._list, 1)

        self.setWidget(body)
        self._generation = None

    def refresh(self):
        if QDockWidget is object:
            return
        self._list.clear()
        if self._app is None:
            self._status.setText("App unavailable.")
            return
        try:
            from classes.agent_tools.transcript import get_transcript
            from classes.agent_tools.receipt import parse_receipt
            # The default receipt is compact (script only); the list needs words.
            raw = get_transcript(includeWords=True)
            receipt = parse_receipt(raw)
        except Exception as exc:
            self._status.setText(f"Refresh failed: {exc}")
            log.warning("Index refresh failed: %s", exc)
            return

        if receipt.get("status") in ("error", "refused"):
            self._status.setText(receipt.get("summary") or "No transcript.")
            return

        data = receipt.get("data") or {}
        self._generation = data.get("transcriptGeneration")
        count = 0
        for clip in data.get("clips") or []:
            cid = clip.get("clipId") or ""
            for w in clip.get("words") or []:
                text = str(w.get("text") or "")
                sid = w.get("speakerId") or ""
                prefix = f"[{sid}] " if sid else ""
                label = f"{prefix}{text}   f{w.get('startFrame', '?')}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "clipId": cid,
                    "startFrame": w.get("startFrame"),
                    "startSec": w.get("timelineStartSec", w.get("startSec")),
                    "speakerId": sid,
                })
                if sid:
                    item.setToolTip(f"{sid} @ frame {w.get('startFrame')}")
                self._list.addItem(item)
                count += 1

        src = data.get("transcriptionSource") or "local"
        self._status.setText(
            f"{count} words · {src} · gen {self._generation}"
        )

    def _on_item(self, item):
        if QDockWidget is object or item is None or self._app is None:
            return
        meta = item.data(Qt.UserRole) or {}
        try:
            frame = meta.get("startFrame")
            win = self._app.window
            if frame is not None and hasattr(win, "SeekSignal"):
                win.SeekSignal.emit(int(frame) + 1)
                return
            sec = meta.get("startSec")
            if sec is not None and hasattr(win, "SeekSignal"):
                from classes.frame_time import to_frame
                from classes.clip_utils import project_fps_fraction
                f = to_frame(float(sec), project_fps_fraction())
                win.SeekSignal.emit(int(f) + 1)
        except Exception as exc:
            log.warning("Index seek failed: %s", exc)
