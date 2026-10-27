"""
Pexels stock-video search dock for Zenvi.

Search bar (QLineEdit + magnifying-glass button) → results grid (thumbnail +
duration overlay) → click a card to download and import into Project Files.
"""

import os
import html
from typing import List, Dict, Any

from qt_api import (
    Qt, QThread, pyqtSignal, QSize, QTimer, QRunnable, QThreadPool,
    pyqtSlot, QObject,
)
from qt_api import QPixmap, QPainter, QPainterPath, QPen, QBrush, QRect, QRectF, QImage
from qt_api import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QScrollArea, QGridLayout,
    QLabel, QSizePolicy, QFrame, QProgressBar, QApplication, )

from classes.logger import log
from themes.cosmic import tokens
from windows.views.files_thumbnail_overlay import (
    TILE_RADIUS, paint_caption, paint_text, token_color,
)


# ── Async helpers ─────────────────────────────────────────────────────────────

class _SearchWorker(QObject):
    """Runs pexels_search on a background thread."""
    finished = pyqtSignal(dict)   # payload: PexelsSearchResponse dict

    def __init__(self, query: str, page: int = 1):
        super().__init__()
        self._query = query
        self._page = page

    @pyqtSlot()
    def run(self):
        try:
            from classes.api_client import get_backend_client
            result = get_backend_client().pexels_search(self._query, per_page=16, page=self._page)
        except Exception as exc:
            result = {"videos": [], "error": str(exc)}
        self.finished.emit(result)


class _DownloadWorker(QObject):
    """Downloads a single Pexels video on a background thread."""
    finished = pyqtSignal(int, str, str)   # video_id, local_path, error

    def __init__(self, video_id: int, link: str, filename: str):
        super().__init__()
        self._video_id = video_id
        self._link = link
        self._filename = filename

    @pyqtSlot()
    def run(self):
        try:
            from classes.credits_client import charge_operation_on_success, check_operation

            _, _, blocked = check_operation("stock_add", "stock media download")
            if blocked:
                self.finished.emit(self._video_id, "", blocked)
                return

            from classes.api_client import get_backend_client
            result = get_backend_client().pexels_download(
                self._video_id, self._link, self._filename
            )
            local_path = result.get("local_path", "")
            error = result.get("error", "")
            if not error and local_path:
                charge_operation_on_success(
                    True,
                    "stock_add",
                    "stock_add",
                    provider="pexels",
                    note=f"dock video {self._video_id}",
                )
        except Exception as exc:
            local_path, error = "", str(exc)
        self.finished.emit(self._video_id, local_path, error)


_image_pool = None


def image_pool():
    """Where network image fetches run: never QThreadPool.globalInstance().

    Qt smooth-scales a large image across the global pool and the GUI thread waits
    for it; fetches parked there on a slow host froze the editor for minutes.
    """
    global _image_pool
    if _image_pool is None:
        _image_pool = QThreadPool()
        _image_pool.setMaxThreadCount(4)
    return _image_pool


class _ThumbnailRunnable(QRunnable):
    """Fetches a thumbnail URL and emits the result via a signal carrier."""

    class _Signals(QObject):
        ready = pyqtSignal(int, QImage, int)   # video_id, image (QPixmap is GUI-thread only), search
        finished = pyqtSignal(int, int)        # video_id, search; success or not

    def __init__(self, video_id: int, url: str, search: int):
        super().__init__()
        self.signals = _ThumbnailRunnable._Signals()
        self._video_id = video_id
        self._url = url
        self._search = search

    def run(self):
        try:
            import requests as _req
            resp = _req.get(
                self._url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                timeout=10,
            )
            resp.raise_for_status()
            image = QImage.fromData(resp.content)
            if not image.isNull():
                self.signals.ready.emit(self._video_id, image, self._search)
        except Exception as exc:
            log.warning("Pexels thumbnail fetch failed (%s): %s", self._url, exc)
        finally:
            self.signals.finished.emit(self._video_id, self._search)


# ── Video card widget ─────────────────────────────────────────────────────────

CARD_W = 160
CARD_MEDIA_H = 90            # 16:9 thumbnail
CARD_H = CARD_MEDIA_H + 40   # plus the name and metadata lines


class _VideoCard(QFrame):
    """A result tile: thumbnail, then the photographer's name and resolution · duration."""

    clicked = pyqtSignal(dict)   # emits the video dict

    def __init__(self, video: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._video = video
        self._thumb: QPixmap | None = None
        self._media_pending = bool(video.get("video_pictures") or video.get("image"))
        self._downloading = False

        self.setFixedSize(CARD_W, CARD_H)
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)

        dur = video.get("duration", 0)
        mins, secs = divmod(int(dur), 60)
        self._dur_text = f"{mins}:{secs:02d}"
        h = video.get("height", 0)
        if h >= 2160:
            res_text = "4K"
        elif h >= 1080:
            res_text = "HD"
        elif h >= 720:
            res_text = "720p"
        else:
            res_text = f"{h}p"
        self._meta_text = f"{res_text} · {self._dur_text}"

        self._status_label = QLabel("", self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(tokens.resolve(
            "color: {text}; font-size: 11px; background: transparent;"
        ))
        self._status_label.setGeometry(4, CARD_MEDIA_H // 2, CARD_W - 8, CARD_MEDIA_H // 2)
        self._status_label.hide()

        # Provider text, shown literally: a plain-text tooltip would turn any tags in it into markup.
        tip = html.escape(f"User: {video.get('user_name','')}\n{video.get('width',0)}×{video.get('height',0)}  "
                          f"{self._dur_text}")
        self.setToolTip(f"<p style='white-space:pre-wrap'>{tip}</p>")

    def set_thumbnail(self, pix: QPixmap):
        scaled = pix.scaled(CARD_W, CARD_MEDIA_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = (scaled.width() - CARD_W) // 2
        y = (scaled.height() - CARD_MEDIA_H) // 2
        self._thumb = scaled.copy(x, y, CARD_W, CARD_MEDIA_H)
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
        if self._thumb:
            painter.drawPixmap(0, 0, self._thumb)
        elif self._media_pending:
            paint_text(painter, QRect(0, 0, CARD_W, CARD_MEDIA_H), "Loading…", 11, "text_faint", Qt.AlignCenter)
        if self._downloading:
            veil = token_color("bg")
            veil.setAlpha(140)
            painter.fillRect(QRect(0, 0, CARD_W, CARD_MEDIA_H), veil)
        painter.restore()

        paint_caption(painter, QRect(6, CARD_MEDIA_H + 6, CARD_W - 12, 36),
                      self._video.get("user_name", ""), self._meta_text)

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
            self.clicked.emit(self._video)


# ── Main dock ─────────────────────────────────────────────────────────────────

class PexelsDock(QDockWidget):
    """Pexels stock-video search dock."""

    def __init__(self, parent=None):
        super().__init__("Pexels Stock Videos", parent)
        self.setObjectName("PexelsDock")
        self.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self.setMinimumWidth(300)
        self.resize(340, 520)

        self._search_thread: QThread | None = None
        # Keyed by video_id so concurrent downloads don't clobber each other.
        self._dl_threads: Dict[int, QThread] = {}
        self._dl_workers: Dict[int, _DownloadWorker] = {}
        self._cards: Dict[int, _VideoCard] = {}
        self._thumb_pool = image_pool()

        self._build_ui()

        # Clean up background threads on app quit
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._cleanup_threads)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("PexelsDockContents")
        root.setStyleSheet(tokens.resolve("""
            QWidget#PexelsDockContents { background: {surface}; }
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
        self._search_input.setPlaceholderText("Search Pexels videos…")
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

        # ── Progress bar (hidden normally) ────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
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
        self._total_results = 0

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

        videos = data.get("videos", [])
        self._total_results = data.get("total_results", 0)

        if not videos:
            self._status_label.setText("No results found.")
            return

        self._status_label.setText(
            f"{self._total_results:,} results  •  page {self._current_page}"
        )

        # Add cards
        existing = self._grid.count()
        cols = max(1, (self.width() - 24) // (CARD_W + 8))
        for idx, video in enumerate(videos):
            vid_id = video.get("id", idx)
            card = _VideoCard(video)
            card.clicked.connect(self._on_card_clicked)
            self._cards[vid_id] = card
            pos = existing + idx
            row, col = divmod(pos, cols)
            self._grid.addWidget(card, row, col)

            # Prefer video_pictures[nr==0], fall back to image field
            thumb_url = ""
            pictures = video.get("video_pictures", [])
            if pictures:
                # find nr==0, else just take the first entry
                pic0 = next((p for p in pictures if p.get("nr") == 0), pictures[0])
                thumb_url = pic0.get("picture", "")
            if not thumb_url:
                thumb_url = video.get("image", "")
            if thumb_url:
                runnable = _ThumbnailRunnable(vid_id, thumb_url, self._search_gen)
                runnable.signals.ready.connect(self._on_thumbnail_ready)
                runnable.signals.finished.connect(self._on_thumbnail_finished)
                self._thumb_pool.start(runnable)

        # Show "Load more" if more pages exist
        self._fit_grid()

        shown = self._current_page * data.get("per_page", 15)
        if shown < self._total_results:
            self._load_more_btn.show()
        else:
            self._load_more_btn.hide()

    @pyqtSlot(int, QImage, int)
    def _on_thumbnail_ready(self, video_id: int, image: QImage, search: int):
        card = self._cards.get(video_id) if search == self._search_gen else None
        if card:
            card.set_thumbnail(QPixmap.fromImage(image))

    @pyqtSlot(int, int)
    def _on_thumbnail_finished(self, video_id: int, search: int):
        # Failed or invalid thumbnail: stop showing "Loading…" (a stale search is ignored)
        card = self._cards.get(video_id) if search == self._search_gen else None
        if card:
            card.set_media_pending(False)

    # ── Download flow ─────────────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _on_card_clicked(self, video: dict):
        vid_id = video.get("id", 0)
        card = self._cards.get(vid_id)

        # Ignore clicks while this video is already downloading.
        if vid_id in self._dl_threads:
            return

        if card:
            card.set_downloading(True)

        # Pick best HD file (prefer hd, then sd)
        files = video.get("video_files", [])
        mp4_files = [f for f in files if "mp4" in f.get("file_type", "").lower()]
        hd_files = [f for f in mp4_files if f.get("quality") == "hd"]
        chosen = hd_files[0] if hd_files else (mp4_files[0] if mp4_files else None)
        if not chosen:
            if card:
                card.set_error("No downloadable MP4 found")
            return

        link = chosen.get("link", "")
        filename = f"pexels_{vid_id}"

        thread = QThread()
        worker = _DownloadWorker(vid_id, link, filename)
        worker.moveToThread(thread)
        # Keep strong Python references in the dicts so the worker/thread are
        # never GC'd while the download is in flight.
        self._dl_threads[vid_id] = thread
        self._dl_workers[vid_id] = worker
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_download_done)
        worker.finished.connect(thread.quit)
        # Release Python refs only after QThread::finished fires.  That signal
        # is emitted after Qt internally sets running=False, so destroying the
        # wrapper there is safe.  Doing it inside _on_download_done (which is
        # triggered by the *worker's* custom pyqtSignal) would race against the
        # OS thread still running its cleanup, causing the
        # "QThread: Destroyed while thread is still running" abort.
        thread.finished.connect(lambda: self._cleanup_dl(vid_id))
        thread.start()

    def _cleanup_dl(self, video_id: int):
        """Release per-video thread/worker refs after QThread::finished."""
        self._dl_threads.pop(video_id, None)
        self._dl_workers.pop(video_id, None)

    @pyqtSlot(int, str, str)
    def _on_download_done(self, video_id: int, local_path: str, error: str):
        # NOTE: do NOT touch self._dl_threads/_dl_workers here — the QThread
        # is still running its OS-level cleanup at this point.  _cleanup_dl
        # (connected to thread.finished) handles that safely.
        card = self._cards.get(video_id)
        if error or not local_path:
            log.error("Pexels download error: %s", error)
            if card:
                card.set_error(error or "Download failed")
            return

        if card:
            card.set_done()

        # Add to Project Files and trigger indexing/summarize.
        try:
            from classes.app import get_app
            from classes.query import File
            app = get_app()
            if app and hasattr(app, "window") and app.window:
                files_model = app.window.files_model
                existing = File.get(path=local_path)
                if existing:
                    # File already in project — re-trigger indexing if it was
                    # never successfully analyzed (e.g., backend was down during
                    # the first import).
                    if not (existing.data.get("ai_metadata") or {}).get("analyzed"):
                        log.info(
                            "Pexels video already in project but untagged, "
                            "re-triggering indexing: %s", local_path,
                        )
                        files_model._index_file_async(existing.id)
                else:
                    files_model.add_files([local_path])
                    log.info("Pexels video added to Project Files: %s", local_path)
        except Exception as exc:
            log.error("Failed to add Pexels video to Project Files: %s", exc)

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
