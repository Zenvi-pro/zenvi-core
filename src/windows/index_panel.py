"""Index panel — timeline transcript browser (same store as agent tools)."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("windows.index_panel")

try:
    from qt_api import Qt
    from qt_api import (
        QDockWidget,
        QColor,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
        QTextBrowser,
        QTextCursor,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except Exception:  # headless / stub
    Qt = None  # type: ignore
    QDockWidget = object  # type: ignore
    QWidget = object  # type: ignore


READER_STYLE = (
    "body { font-size: 14px; line-height: 160%; }"
    "p { margin: 0 0 14px 0; }"
    "a { color: palette(text); text-decoration: none; }"
    ".who { font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: palette(mid); }"
)


class IndexPanel(QDockWidget if QDockWidget is not object else object):
    """Dockable transcript reader: clean paragraphs, the spoken word lit as the video plays, and a click on a word seeks to it."""

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

        self._people_btn = QPushButton("Delete people data…")
        self._people_btn.setToolTip("Remove every face scan and every name stored on this computer")
        self._people_btn.clicked.connect(self.delete_people_data)
        layout.addWidget(self._people_btn, 0)

        self._view = QTextBrowser()
        self._view.setOpenLinks(False)
        self._view.setFrameShape(QTextBrowser.NoFrame)
        self._view.document().setDefaultStyleSheet(READER_STYLE)
        self._view.anchorClicked.connect(self._on_link)
        layout.addWidget(self._view, 1)

        self.setWidget(body)
        self._generation = None
        self._loading = False
        self._model = {"words": [], "paragraphs": []}
        self._starts = []
        self._lit = -1
        self._word_spans = {}
        win = parent if hasattr(parent, "previewFrameSignal") else getattr(self._app, "window", None)
        if win is not None:
            # Playback and scrubbing move preview_thread.position_changed. previewFrameSignal
            # fires only for a frame-step key, so the word has to follow both.
            for signal in (getattr(win, "previewFrameSignal", None),
                           getattr(getattr(win, "preview_thread", None), "position_changed", None)):
                if signal is not None:
                    signal.connect(self.follow)

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

    def _confirm_delete_people(self):
        return QMessageBox.question(
            self, "Delete people data",
            "Delete every face scan and every name stored on this computer? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def delete_people_data(self):
        """Remove all people data (face scans, names). The files are removed on a worker, never on the GUI thread."""
        if QDockWidget is object or not self._confirm_delete_people():
            return
        self._status.setText("Deleting people data…")
        threading.Thread(target=self._delete_people_worker, name="index_panel_delete_people", daemon=True).start()

    def _delete_people_worker(self):
        from classes.qt_main_thread import invoke_on_gui
        try:
            from classes.media_index import people
            out = people.delete_all()
            text = f"Deleted {out['scans']} face scan(s) and the people list."
        except Exception as exc:
            log.warning("Deleting people data failed: %s", exc)
            text = f"Could not delete people data: {exc}"
        invoke_on_gui(self._status.setText, text)

    def _project_token(self):
        """The open project's data dict: New and Open replace it."""
        return getattr(getattr(self._app, "project", None), "_data", None)

    def _load_transcript(self, token=None):
        from classes.qt_main_thread import invoke_on_gui
        try:
            from classes.agent_tools.transcript import get_transcript
            # The default receipt is compact (script only); the list needs words.
            raw = get_transcript(include_words=True)
        except Exception as exc:
            log.warning("Index refresh failed: %s", exc)
            raw = exc
        invoke_on_gui(self._populate, raw, token)

    def _populate(self, raw, token=None):
        self._loading = False
        self._show_words({"words": [], "paragraphs": []})
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

        data = receipt.get("data") or {}
        self._generation = data.get("transcript_generation")
        from classes.speech import transcript_view
        model = transcript_view.reading_model(data.get("clips") or [], fps=self._fps())
        self._show_words(model)
        src = data.get("transcriptionSource") or "local"
        self._status.setText(f"{len(model['words'])} words · {src} · gen {self._generation}")

    def _fps(self):
        try:
            from classes.clip_utils import project_fps_fraction
            fps = project_fps_fraction()
            return float(fps.num) / float(fps.den)
        except Exception:
            return 30.0

    def _show_words(self, model):
        from classes.speech import transcript_view
        self._model = model
        self._starts = [w["frame"] for w in model["words"]]
        self._lit = -1
        self._word_spans = {}
        self._view.setExtraSelections([])
        self._view.setHtml(transcript_view.reading_html(model))
        # Where each word sits in the document, read from the link fragments themselves (never by searching the text).
        block = self._view.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if frag.isValid() and fmt.isAnchor() and fmt.anchorHref().startswith("w:"):
                    self._word_spans[int(fmt.anchorHref()[2:])] = (frag.position(), frag.position() + frag.length())
                it += 1
            block = block.next()

    def follow(self, frame):
        """Light the word spoken at this preview frame (connected to the preview's frame signal)."""
        if QDockWidget is object or not self._starts:
            return
        from classes.speech import transcript_view
        i = transcript_view.word_at(self._starts, int(frame) - 1)
        if i == self._lit:
            return
        self._lit = i
        span = self._word_spans.get(i)
        if span is None:
            self._view.setExtraSelections([])
            return
        cursor = QTextCursor(self._view.document())
        cursor.setPosition(span[0])
        cursor.setPosition(span[1], QTextCursor.KeepAnchor)
        sel = QTextEdit.ExtraSelection()
        sel.cursor = cursor
        colour = self.palette().highlight().color()
        sel.format.setBackground(QColor(colour.red(), colour.green(), colour.blue(), 110))
        self._view.setExtraSelections([sel])
        if self.isVisible():                                  # keep the lit word on screen as the reading moves down
            caret = QTextCursor(self._view.document())
            caret.setPosition(span[0])
            self._view.setTextCursor(caret)
            self._view.ensureCursorVisible()

    def _on_link(self, url):
        if QDockWidget is object or self._app is None:
            return
        from classes.speech import transcript_view
        frame = transcript_view.seek_frame(self._model, url.toString())
        if frame is None:
            return
        try:
            win = self._app.window
            if hasattr(win, "SeekSignal"):
                win.SeekSignal.emit(int(frame) + 1)
        except Exception as exc:
            log.warning("Index seek failed: %s", exc)
