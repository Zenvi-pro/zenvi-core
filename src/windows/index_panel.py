"""Index panel — timeline transcript browser (same store as agent tools)."""

from __future__ import annotations

import logging
import threading

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


def _timecode(sec):
    """0:03.2 style: minutes, seconds and tenths."""
    tenths = round(sec * 10)
    minutes, rest = divmod(tenths, 600)
    return f"{minutes}:{rest // 10:02d}.{rest % 10}"


def _line(words, clip_id):
    first = words[0]
    return {
        "text": " ".join(t for t in (str(w.get("text") or "").strip() for w in words) if t),
        "startSec": float(first.get("timelineStartSec", first.get("startSec")) or 0),
        "startFrame": first.get("startFrame"),
        "speakerId": first.get("speakerId") or "",
        "clipId": clip_id,
    }


def _clip_title(clip_id):
    """The timeline clip's name (what the user sees), else its id."""
    try:
        from classes.query import Clip
        clip = Clip.get(id=clip_id)
        return (clip.title() if clip else "") or clip_id
    except Exception:
        return clip_id


def transcript_lines(words, pause=0.7, max_words=12, clip_id=""):
    """Group one clip's word rows into readable lines. A line ends before a pause
    of `pause` seconds or more, after a word ending in . ? !, at `max_words`
    words, and where the speaker changes."""
    lines, cur, last_end = [], [], 0.0
    for w in words:
        start = float(w.get("timelineStartSec", w.get("startSec")) or 0)
        if cur and (
            start - last_end >= pause
            or len(cur) >= max_words
            or (w.get("speakerId") or "") != (cur[0].get("speakerId") or "")
            or str(cur[-1].get("text") or "").rstrip().endswith((".", "?", "!"))
        ):
            lines.append(_line(cur, clip_id))
            cur = []
        cur.append(w)
        last_end = float(w.get("timelineEndSec", w.get("endSec", start)) or start)
    if cur:
        lines.append(_line(cur, clip_id))
    return lines


class IndexPanel(QDockWidget if QDockWidget is not object else object):
    """Dockable transcript list. Click a line to seek the playhead."""

    def __init__(self, parent=None):
        if QDockWidget is object:
            return
        super().__init__("Transcription", parent)
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
        self._status.setWordWrap(True)
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
        self._loading = False
        self._data = None
        # Built inside MainWindow.__init__, before get_app().window is set: follow the parent.
        window = parent if hasattr(parent, "SelectionChanged") else getattr(self._app, "window", None)
        if window is not None:
            window.SelectionChanged.connect(self._render)

    def refresh(self):
        """Reload the word list. Transcription can take seconds on a cache
        miss, so it runs on a worker and the list fills in on the GUI thread."""
        if QDockWidget is object:
            return
        if self._app is None:
            self._status.setText("App unavailable.")
            return
        if getattr(self, "_loading", False):
            return
        self._loading = True
        self._status.setText("Loading transcript…")
        token = self._project_token()
        threading.Thread(
            target=self._load_transcript, args=(token,), name="index_panel_transcript", daemon=True,
        ).start()

    def _project_token(self):
        """The open project's data dict: New and Open replace it."""
        return getattr(getattr(self._app, "project", None), "_data", None)

    def _load_transcript(self, token=None):
        from classes.qt_main_thread import invoke_on_gui
        try:
            from classes.agent_tools.transcript import get_transcript
            # The default receipt is compact (script only); the list needs words.
            raw = get_transcript(includeWords=True)
        except Exception as exc:
            log.warning("Index refresh failed: %s", exc)
            raw = exc
        invoke_on_gui(self._populate, raw, token)

    def _populate(self, raw, token=None):
        self._loading = False
        self._list.clear()
        self._data = None
        if token is not None and token is not self._project_token():
            # A project was opened while this one transcribed: its words
            # would seek in the wrong timeline.
            self._status.setText("Project changed. Press Refresh.")
            return
        if isinstance(raw, Exception):
            self._status.setText(f"Refresh failed: {raw}")
            return
        from classes.agent_tools.receipt import parse_receipt
        receipt = parse_receipt(raw)

        if receipt.get("status") in ("error", "refused"):
            self._status.setText(receipt.get("summary") or "No transcript.")
            return

        self._data = receipt.get("data") or {}
        self._generation = self._data.get("transcriptGeneration")
        self._render()

    def _render(self):
        """Fill the list from the last receipt: the selected clips' lines, or every
        clip under its name. Selection changes call this again without reloading."""
        if self._data is None:
            return
        self._list.clear()
        try:
            selected = {str(c) for c in self._app.window.selected_clips}
        except Exception:
            selected = set()
        lines_n = words_n = 0
        names = []
        for clip in self._data.get("clips") or []:
            cid = str(clip.get("clipId") or "")
            if selected and cid not in selected:
                continue
            words = clip.get("words") or []
            lines = transcript_lines(words, clip_id=cid)
            if not lines:
                continue
            name = clip.get("title") or _clip_title(cid)
            names.append(name)
            lines_n += len(lines)
            words_n += len(words)
            if len(selected) != 1:
                header = QListWidgetItem(name)
                header.setFlags(Qt.NoItemFlags)
                self._list.addItem(header)
            for line in lines:
                prefix = f"[{line['speakerId']}] " if line["speakerId"] else ""
                item = QListWidgetItem(f"{_timecode(line['startSec'])}  {prefix}{line['text']}")
                item.setData(Qt.UserRole, line)
                self._list.addItem(item)

        parts = [f"{lines_n} line{'s' * (lines_n != 1)}", f"{words_n} word{'s' * (words_n != 1)}"]
        if selected:
            parts.append(", ".join(names) or "not transcribed")
        parts += [self._data.get("transcriptionSource") or "local", f"gen {self._generation}"]
        self._status.setText(" · ".join(parts))

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
