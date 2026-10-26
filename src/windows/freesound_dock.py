"""
Freesound stock-music/SFX search dock for Zenvi.

Search bar (QLineEdit + magnifying-glass button) → results list (waveform
image + name + duration) → click a card to download the HQ MP3 preview and
import it into Project Files.
"""

from typing import Dict, Any

from qt_api import (
    Qt, QThread, pyqtSignal, QTimer, QRunnable, QThreadPool,
    pyqtSlot, QObject,
)
from qt_api import QPixmap, QPainter, QPainterPath, QPen, QRect, QRectF, QImage
from qt_api import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QScrollArea, QGridLayout,
    QLabel, QFrame, QProgressBar, QApplication, )

from classes.logger import log
from themes.cosmic import tokens
from windows.views.files_thumbnail_overlay import (
    TILE_RADIUS, paint_caption, paint_text, token_color,
)


# ── Async helpers ─────────────────────────────────────────────────────────────

class _SearchWorker(QObject):
    """Runs freesound_search on a background thread."""
    finished = pyqtSignal(dict)

    def __init__(self, query: str, page: int = 1):
        super().__init__()
        self._query = query
        self._page = page

    @pyqtSlot()
    def run(self):
        try:
            from classes.api_client import get_backend_client
            result = get_backend_client().freesound_search(self._query, page_size=16, page=self._page)
        except Exception as exc:
            result = {"sounds": [], "error": str(exc)}
        self.finished.emit(result)


class _DownloadWorker(QObject):
    """Downloads a single Freesound MP3 preview on a background thread."""
    finished = pyqtSignal(object, str, str)   # sound_id (Openverse UUID), local_path, error

    def __init__(self, sound_id, preview_url: str, filename: str):
        super().__init__()
        self._sound_id = sound_id
        self._preview_url = preview_url
        self._filename = filename

    @pyqtSlot()
    def run(self):
        try:
            from classes.credits_client import charge_operation_on_success, check_operation

            _, _, blocked = check_operation("stock_add", "stock media download")
            if blocked:
                self.finished.emit(self._sound_id, "", blocked)
                return

            from classes.api_client import get_backend_client
            result = get_backend_client().freesound_download(
                self._sound_id, self._preview_url, self._filename
            )
            local_path = result.get("local_path", "")
            error = result.get("error", "")
            if not error and local_path:
                charge_operation_on_success(
                    True,
                    "stock_add",
                    "stock_add",
                    provider="freesound",
                    note=f"dock sound {self._sound_id}",
                )
        except Exception as exc:
            local_path, error = "", str(exc)
        self.finished.emit(self._sound_id, local_path, error)


class _WaveformRunnable(QRunnable):
    """Fetches a waveform image URL and emits the result via a signal carrier."""

    class _Signals(QObject):
        ready = pyqtSignal(int, QImage, int)   # sound_id, image (QPixmap is GUI-thread only), search
        finished = pyqtSignal(int, int)        # sound_id, search; success or not

    def __init__(self, sound_id: int, url: str, search: int):
        super().__init__()
        self.signals = _WaveformRunnable._Signals()
        self._sound_id = sound_id
        self._url = url
        self._search = search

    def run(self):
        try:
            import requests as _req
            resp = _req.get(
                self._url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Zenvi/1.0"},
                timeout=10,
            )
            resp.raise_for_status()
            image = QImage.fromData(resp.content)
            if not image.isNull():
                self.signals.ready.emit(self._sound_id, image, self._search)
        except Exception as exc:
            log.warning("Freesound waveform fetch failed (%s): %s", self._url, exc)
        finally:
            self.signals.finished.emit(self._sound_id, self._search)


# ── Sound card widget ─────────────────────────────────────────────────────────

CARD_W = 240
CARD_MEDIA_H = 72            # waveform strip
CARD_H = CARD_MEDIA_H + 40   # plus the name and metadata lines


class _SoundCard(QFrame):
    """A result tile: waveform, then the sound's name and username · duration."""

    clicked = pyqtSignal(dict)   # emits the sound dict

    def __init__(self, sound: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._sound = sound
        self._waveform: QPixmap | None = None
        self._media_pending = bool((sound.get("images") or {}).get("waveform_m"))
        self._downloading = False

        self.setFixedSize(CARD_W, CARD_H)
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)

        # Duration string
        dur = float(sound.get("duration") or 0)
        mins, secs = divmod(int(dur), 60)
        self._dur_text = f"{mins}:{secs:02d}"

        # Status overlay (Downloading… / ✓ Added / Error)
        self._status_label = QLabel("", self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(tokens.resolve(
            "color: {text}; font-size: 11px; background: transparent;"
        ))
        self._status_label.setGeometry(4, CARD_MEDIA_H // 2, CARD_W - 8, CARD_MEDIA_H // 2)
        self._status_label.hide()

        name = sound.get("name", "")
        username = sound.get("username", "")
        tags = ", ".join((sound.get("tags") or [])[:5])
        credit = sound.get("attribution") or sound.get("license", "")
        self.setToolTip(f"{name}\nBy: {username}\n{self._dur_text}\nTags: {tags}\n{credit}")

    def set_waveform(self, pix: QPixmap):
        self._waveform = pix.scaled(CARD_W, CARD_MEDIA_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._media_pending = False
        self.update()

    def set_media_pending(self, pending: bool):
        self._media_pending = pending
        self.update()

    def set_downloading(self, downloading: bool):
        self._downloading = downloading
        self._status_label.setText("Downloading…" if downloading else "")
        self._status_label.setVisible(downloading)
        self.update()

    def set_done(self):
        self._downloading = False
        self._status_label.setText("✓ Added")
        self._status_label.setStyleSheet(tokens.resolve(
            "color: {success}; font-size: 11px; background: transparent;"
        ))
        self._status_label.show()
        QTimer.singleShot(2500, self._status_label.hide)
        self.update()

    def set_error(self, msg: str):
        self._downloading = False
        self._status_label.setText(f"Error: {msg[:40]}")
        self._status_label.setStyleSheet(tokens.resolve(
            "color: {danger}; font-size: 11px; background: transparent;"
        ))
        self._status_label.show()
        self.update()

    # ── Qt overrides ──────────────────────────────────────────────────────────
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        well = QPainterPath()
        well.addRoundedRect(QRectF(0, 0, CARD_W, CARD_MEDIA_H), TILE_RADIUS, TILE_RADIUS)
        painter.fillPath(well, token_color("sunken"))
        painter.save()
        painter.setClipPath(well)
        if self._waveform:
            painter.setOpacity(0.75)
            painter.drawPixmap(0, 0, self._waveform)
            painter.setOpacity(1.0)
        elif self._media_pending:
            paint_text(painter, QRect(0, 0, CARD_W, CARD_MEDIA_H), "Loading…", 11, "text_faint", Qt.AlignCenter)
        if self._downloading:
            veil = token_color("bg")
            veil.setAlpha(140)
            painter.fillRect(QRect(0, 0, CARD_W, CARD_MEDIA_H), veil)
        painter.restore()

        paint_caption(painter, QRect(6, CARD_MEDIA_H + 6, CARD_W - 12, 36),
                      self._sound.get("name", ""),
                      f"{self._sound.get('username', '')} · {self._dur_text}")

        if self.underMouse():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(token_color("line_strong"), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), TILE_RADIUS, TILE_RADIUS)

    def enterEvent(self, event):
        self.update()

    def leaveEvent(self, event):
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._downloading:
            self.clicked.emit(self._sound)


# ── Main dock ─────────────────────────────────────────────────────────────────

class FreesoundDock(QDockWidget):
    """Freesound stock-music/SFX search dock."""

    def __init__(self, parent=None):
        super().__init__("Freesound Music & SFX", parent)
        self.setObjectName("FreesoundDock")
        self.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self.setMinimumWidth(300)
        self.resize(340, 520)

        self._search_thread: QThread | None = None
        self._dl_threads: Dict[int, QThread] = {}
        self._dl_workers: Dict[int, _DownloadWorker] = {}
        self._cards: Dict[int, _SoundCard] = {}
        self._wave_pool = QThreadPool.globalInstance()
        self._search_gen = 0   # results from an older search are ignored

        self._build_ui()

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._cleanup_threads)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("FreesoundDockContents")
        root.setStyleSheet(tokens.resolve("""
            QWidget#FreesoundDockContents { background: {surface}; }
            QLineEdit {
                background: {sunken};
                border: 1px solid {line_strong};
                border-radius: 6px;
                color: {text};
                padding: 4px 8px;
                font-size: 12px;
            }
            QLineEdit:focus { border-color: {accent}; }
            QPushButton#searchBtn {
                background: {raised};
                border: 1px solid {line_strong};
                border-radius: 6px;
                color: {text};
                font-size: 12px;
                padding: 4px 12px;
            }
            QPushButton#searchBtn:hover { background: {hover}; }
            QPushButton#searchBtn:pressed { background: {active}; }
            QPushButton#searchBtn:disabled { color: {text_off}; }
            QLabel#statusLabel { color: {text_faint}; font-size: 11px; }
            QScrollArea { background: {surface}; border: none; }
            QWidget#scrollContents { background: {surface}; }
            QPushButton#loadMoreBtn {
                background: {raised};
                border: 1px solid {line_strong};
                border-radius: 6px;
                color: {text};
                padding: 6px;
                font-size: 12px;
                margin: 6px;
            }
            QPushButton#loadMoreBtn:hover { background: {hover}; }
        """))

        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        # ── Search bar ────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search Freesound music & SFX…")
        self._search_input.returnPressed.connect(self._on_search)
        bar.addWidget(self._search_input)

        self._search_btn = QPushButton("Search")
        self._search_btn.setObjectName("searchBtn")
        self._search_btn.setToolTip("Search")
        self._search_btn.clicked.connect(self._on_search)
        bar.addWidget(self._search_btn)
        vbox.addLayout(bar)

        # ── Status label ──────────────────────────────────────────────────────
        self._status_label = QLabel("")
        self._status_label.setObjectName("statusLabel")
        self._status_label.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self._status_label)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setFixedHeight(3)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(tokens.resolve(
            "QProgressBar { background: {sunken}; border: none; border-radius: 1px; }"
            "QProgressBar::chunk { background: {accent}; border-radius: 1px; }"
        ))
        self._progress.hide()
        vbox.addWidget(self._progress)

        # ── Results scroll area ───────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._grid_container = QWidget()
        self._grid_container.setObjectName("scrollContents")
        self._grid = QGridLayout(self._grid_container)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setSpacing(8)
        # Let the scroll area grow to the rows instead of squeezing them into the viewport.
        self._scroll.setWidget(self._grid_container)
        vbox.addWidget(self._scroll, 1)

        # ── Load more ─────────────────────────────────────────────────────────
        self._load_more_btn = QPushButton("Load more…")
        self._load_more_btn.setObjectName("loadMoreBtn")
        self._load_more_btn.hide()
        self._load_more_btn.clicked.connect(self._on_load_more)
        vbox.addWidget(self._load_more_btn)

        self.setWidget(root)

        # State
        self._current_query = ""
        self._current_page = 1
        self._total_count = 0

    # ── Search flow ───────────────────────────────────────────────────────────

    def _on_search(self):
        query = self._search_input.text().strip()
        if not query:
            return
        self._current_query = query
        self._current_page = 1
        self._clear_grid()
        self._cards.clear()
        self._load_more_btn.hide()
        self._run_search(query, page=1)

    def _on_load_more(self):
        self._current_page += 1
        self._run_search(self._current_query, page=self._current_page)

    def _stop_search_thread(self):
        t = getattr(self, "_search_thread", None)
        if t and t.isRunning():
            t.quit()
            if not t.wait(2000):
                t.terminate()
                t.wait(500)

    def _run_search(self, query: str, page: int):
        self._set_searching(True)
        self._stop_search_thread()

        self._search_thread = QThread()
        self._worker = _SearchWorker(query, page)
        self._worker.moveToThread(self._search_thread)
        self._search_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_search_done)
        self._worker.finished.connect(self._search_thread.quit)
        self._search_thread.start()

    @pyqtSlot(dict)
    def _on_search_done(self, data: dict):
        self._set_searching(False)
        error = data.get("error")
        if error:
            self._status_label.setText(f"Error: {error}")
            return

        sounds = data.get("sounds", [])
        self._total_count = data.get("count", 0)

        if not sounds:
            self._status_label.setText("No results found.")
            return

        self._status_label.setText(
            f"{self._total_count:,} results  •  page {self._current_page}"
        )

        existing = self._grid.count()
        cols = max(1, (self.width() - 24) // (CARD_W + 8))
        for idx, sound in enumerate(sounds):
            sid = sound.get("id", idx)
            card = _SoundCard(sound)
            card.clicked.connect(self._on_card_clicked)
            self._cards[sid] = card
            pos = existing + idx
            row, col = divmod(pos, cols)
            self._grid.addWidget(card, row, col)

            # Fetch waveform image asynchronously
            wave_url = (sound.get("images") or {}).get("waveform_m", "")
            if wave_url:
                runnable = _WaveformRunnable(sid, wave_url, self._search_gen)
                runnable.signals.ready.connect(self._on_waveform_ready)
                runnable.signals.finished.connect(self._on_waveform_finished)
                self._wave_pool.start(runnable)

        self._fit_grid()

        shown = self._current_page * data.get("page_size", 15)
        if shown < self._total_count:
            self._load_more_btn.show()
        else:
            self._load_more_btn.hide()

    @pyqtSlot(int, QImage, int)
    def _on_waveform_ready(self, sound_id: int, image: QImage, search: int):
        card = self._cards.get(sound_id) if search == self._search_gen else None
        if card:
            card.set_waveform(QPixmap.fromImage(image))

    @pyqtSlot(int, int)
    def _on_waveform_finished(self, sound_id: int, search: int):
        # Failed or invalid waveform: stop showing "Loading…" (a stale search is ignored)
        card = self._cards.get(sound_id) if search == self._search_gen else None
        if card:
            card.set_media_pending(False)

    # ── Download flow ─────────────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _on_card_clicked(self, sound: dict):
        sid = sound.get("id", 0)
        card = self._cards.get(sid)

        if sid in self._dl_threads:
            return   # already downloading

        if card:
            card.set_downloading(True)

        preview_url = (sound.get("previews") or {}).get("hq_mp3", "")
        if not preview_url:
            if card:
                card.set_error("No preview URL available")
            return

        # Sanitise sound name for use as filename
        raw_name = sound.get("name", "") or f"freesound_{sid}"
        import re
        safe_name = re.sub(r"[^\w\-]", "_", raw_name)[:60]
        filename = f"freesound_{sid}_{safe_name}"

        thread = QThread()
        worker = _DownloadWorker(sid, preview_url, filename)
        worker.moveToThread(thread)
        self._dl_threads[sid] = thread
        self._dl_workers[sid] = worker
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_download_done)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._cleanup_dl(sid))
        thread.start()

    def _cleanup_dl(self, sound_id: int):
        self._dl_threads.pop(sound_id, None)
        self._dl_workers.pop(sound_id, None)

    @pyqtSlot(int, str, str)
    def _on_download_done(self, sound_id: int, local_path: str, error: str):
        card = self._cards.get(sound_id)
        if error or not local_path:
            log.error("Freesound download error: %s", error)
            if card:
                card.set_error(error or "Download failed")
            return

        if card:
            card.set_done()

        # Add to Project Files
        try:
            from classes.app import get_app
            from classes.query import File
            app = get_app()
            if app and hasattr(app, "window") and app.window:
                files_model = app.window.files_model
                existing = File.get(path=local_path)
                if existing:
                    if not (existing.data.get("ai_metadata") or {}).get("analyzed"):
                        files_model._index_file_async(existing.id)
                else:
                    files_model.add_files([local_path])
                    log.info("Freesound audio added to Project Files: %s", local_path)
        except Exception as exc:
            log.error("Failed to add Freesound audio to Project Files: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_searching(self, active: bool):
        self._search_btn.setEnabled(not active)
        self._search_input.setEnabled(not active)
        if active:
            self._progress.show()
        else:
            self._progress.hide()

    def _fit_grid(self):
        """Re-flow cards to the viewport width; fix only the height so the dock can still narrow."""
        cols = max(1, (self._scroll.viewport().width() - 8) // (CARD_W + 8))
        widgets = []
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget():
                widgets.append(item.widget())
        for idx, w in enumerate(widgets):
            row, col = divmod(idx, cols)
            self._grid.addWidget(w, row, col)
        self._grid_container.setMinimumHeight(self._grid.sizeHint().height())

    def _clear_grid(self):
        self._search_gen += 1
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._status_label.setText("")

    def _cleanup_threads(self):
        self._stop_search_thread()
        threads = list(self._dl_threads.values())
        for t in threads:
            if t and t.isRunning():
                t.quit()
                t.wait(2000)
        self._dl_threads.clear()
        self._dl_workers.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cards:
            self._fit_grid()
