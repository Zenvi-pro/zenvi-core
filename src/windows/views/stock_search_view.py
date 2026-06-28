"""
Unified stock-media search view for the Files panel.

Renders two titled sections for a single query — "Stock Footage" (Pexels) and
"Stock music" (Freesound). Clicking a result downloads it and imports it into
Project Files. This view is a thin orchestration layer: the search workers,
download workers, result cards and async image loaders are reused as-is from
``pexels_dock`` and ``freesound_dock``.
"""

from typing import Dict

from PyQt5.QtCore import Qt, QThread, QThreadPool, pyqtSlot
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea, QGridLayout, QLabel, QApplication,
)

from classes.logger import log

from windows.pexels_dock import (
    _SearchWorker as PexelsSearchWorker,
    _DownloadWorker as PexelsDownloadWorker,
    _VideoCard, _ThumbnailRunnable,
    CARD_W as VIDEO_W,
)
from windows.freesound_dock import (
    _SearchWorker as FreesoundSearchWorker,
    _DownloadWorker as FreesoundDownloadWorker,
    _SoundCard, _WaveformRunnable,
    CARD_W as SOUND_W,
)


_STYLESHEET = """
    QWidget#StockSearchView { background: #0d0d0d; }
    QScrollArea { background: #0d0d0d; border: none; }
    QWidget#scrollContents { background: #0d0d0d; }
    QLabel#sectionHeader { color: #d4d4d4; font-size: 12px; font-weight: bold;
        padding: 6px 2px 2px 2px; }
    QLabel#sectionStatus { color: #8a8a8a; font-size: 10px; padding-left: 2px; }
"""


class StockSearchView(QWidget):
    """Stock footage + music results for a single query, embedded in the Files panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StockSearchView")
        self.setStyleSheet(_STYLESHEET)

        self._query = ""
        self._search_threads = []   # keep search threads alive while running
        self._search_workers = []
        # Download threads/workers keyed by a provider-prefixed uid ("v123"/"s456")
        # so a Pexels id and a Freesound id can never clobber each other.
        self._dl_threads: Dict[str, QThread] = {}
        self._dl_workers: Dict[str, object] = {}
        self._cards: Dict[str, object] = {}
        self._pool = QThreadPool.globalInstance()

        self._build_ui()

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._cleanup_threads)

    # ── UI construction ─────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("scrollContents")
        cv = QVBoxLayout(content)
        cv.setContentsMargins(4, 4, 4, 4)
        cv.setSpacing(2)

        self._footage_header = QLabel("Stock Footage")
        self._footage_header.setObjectName("sectionHeader")
        cv.addWidget(self._footage_header)
        self._footage_status = QLabel("")
        self._footage_status.setObjectName("sectionStatus")
        cv.addWidget(self._footage_status)
        footage_container = QWidget()
        self._footage_grid = QGridLayout(footage_container)
        self._footage_grid.setContentsMargins(0, 4, 0, 8)
        self._footage_grid.setSpacing(6)
        cv.addWidget(footage_container)

        self._music_header = QLabel("Stock music")
        self._music_header.setObjectName("sectionHeader")
        cv.addWidget(self._music_header)
        self._music_status = QLabel("")
        self._music_status.setObjectName("sectionStatus")
        cv.addWidget(self._music_status)
        music_container = QWidget()
        self._music_grid = QGridLayout(music_container)
        self._music_grid.setContentsMargins(0, 4, 0, 8)
        self._music_grid.setSpacing(6)
        cv.addWidget(music_container)

        cv.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ── Public API ──────────────────────────────────────────────────────────────

    def run_search(self, query: str):
        """Search both providers for ``query`` and populate the two sections."""
        query = (query or "").strip()
        if not query:
            self.clear_results()
            return
        self._query = query
        self.clear_results()
        self._footage_status.setText("Searching…")
        self._music_status.setText("Searching…")

        self._start_search(PexelsSearchWorker(query, 1), self._on_footage_results)
        self._start_search(FreesoundSearchWorker(query, 1), self._on_music_results)

    def clear_results(self):
        self._clear_grid(self._footage_grid)
        self._clear_grid(self._music_grid)
        self._cards.clear()
        self._footage_status.setText("")
        self._music_status.setText("")

    # ── Search flow ───────────────────────────────────────────────────────────────

    def _start_search(self, worker, on_done):
        thread = QThread()
        worker.moveToThread(thread)
        self._search_threads.append(thread)
        self._search_workers.append(worker)
        thread.started.connect(worker.run)
        worker.finished.connect(on_done)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda t=thread, w=worker: self._cleanup_search(t, w))
        thread.start()

    def _cleanup_search(self, thread, worker):
        if thread in self._search_threads:
            self._search_threads.remove(thread)
        if worker in self._search_workers:
            self._search_workers.remove(worker)

    @pyqtSlot(dict)
    def _on_footage_results(self, data: dict):
        error = data.get("error")
        if error:
            self._footage_status.setText(f"Error: {error}")
            return
        videos = data.get("videos", [])
        if not videos:
            self._footage_status.setText("No footage found.")
            return
        self._footage_status.setText(f"{data.get('total_results', 0):,} results")

        cols = self._cols(VIDEO_W)
        for idx, video in enumerate(videos):
            vid_id = video.get("id", idx)
            uid = f"v{vid_id}"
            card = _VideoCard(video)
            card.clicked.connect(self._on_footage_clicked)
            self._cards[uid] = card
            row, col = divmod(idx, cols)
            self._footage_grid.addWidget(card, row, col)

            thumb_url = ""
            pictures = video.get("video_pictures", [])
            if pictures:
                pic0 = next((p for p in pictures if p.get("nr") == 0), pictures[0])
                thumb_url = pic0.get("picture", "")
            if not thumb_url:
                thumb_url = video.get("image", "")
            if thumb_url:
                runnable = _ThumbnailRunnable(vid_id, thumb_url)
                runnable.signals.ready.connect(self._on_thumbnail_ready)
                self._pool.start(runnable)

    @pyqtSlot(dict)
    def _on_music_results(self, data: dict):
        error = data.get("error")
        if error:
            self._music_status.setText(f"Error: {error}")
            return
        sounds = data.get("sounds", [])
        if not sounds:
            self._music_status.setText("No music found.")
            return
        self._music_status.setText(f"{data.get('count', 0):,} results")

        cols = self._cols(SOUND_W)
        for idx, sound in enumerate(sounds):
            sid = sound.get("id", idx)
            uid = f"s{sid}"
            card = _SoundCard(sound)
            card.clicked.connect(self._on_music_clicked)
            self._cards[uid] = card
            row, col = divmod(idx, cols)
            self._music_grid.addWidget(card, row, col)

            wave_url = (sound.get("images") or {}).get("waveform_m", "")
            if wave_url:
                runnable = _WaveformRunnable(sid, wave_url)
                runnable.signals.ready.connect(self._on_waveform_ready)
                self._pool.start(runnable)

    @pyqtSlot(int, QPixmap)
    def _on_thumbnail_ready(self, video_id: int, pix: QPixmap):
        card = self._cards.get(f"v{video_id}")
        if card:
            card.set_thumbnail(pix)

    @pyqtSlot(int, QPixmap)
    def _on_waveform_ready(self, sound_id: int, pix: QPixmap):
        card = self._cards.get(f"s{sound_id}")
        if card:
            card.set_waveform(pix)

    # ── Download + add-to-project flow ────────────────────────────────────────────

    @pyqtSlot(dict)
    def _on_footage_clicked(self, video: dict):
        uid = f"v{video.get('id', 0)}"
        if uid in self._dl_threads:
            return
        card = self._cards.get(uid)

        files = video.get("video_files", [])
        mp4_files = [f for f in files if "mp4" in f.get("file_type", "").lower()]
        hd_files = [f for f in mp4_files if f.get("quality") == "hd"]
        chosen = hd_files[0] if hd_files else (mp4_files[0] if mp4_files else None)
        if not chosen:
            if card:
                card.set_error("No downloadable MP4 found")
            return

        if card:
            card.set_downloading(True)
        vid_id = video.get("id", 0)
        worker = PexelsDownloadWorker(vid_id, chosen.get("link", ""), f"pexels_{vid_id}")
        self._start_download(uid, worker)

    @pyqtSlot(dict)
    def _on_music_clicked(self, sound: dict):
        uid = f"s{sound.get('id', 0)}"
        if uid in self._dl_threads:
            return
        card = self._cards.get(uid)

        preview_url = (sound.get("previews") or {}).get("hq_mp3", "")
        if not preview_url:
            if card:
                card.set_error("No preview URL available")
            return

        import re
        sid = sound.get("id", 0)
        raw_name = sound.get("name", "") or f"freesound_{sid}"
        safe_name = re.sub(r"[^\w\-]", "_", raw_name)[:60]
        if card:
            card.set_downloading(True)
        worker = FreesoundDownloadWorker(sid, preview_url, f"freesound_{sid}_{safe_name}")
        self._start_download(uid, worker)

    def _start_download(self, uid: str, worker):
        thread = QThread()
        worker.moveToThread(thread)
        self._dl_threads[uid] = thread
        self._dl_workers[uid] = worker
        thread.started.connect(worker.run)
        # The worker emits (media_id, local_path, error); re-bind the provider uid so
        # the right card updates and the per-uid refs are released safely.
        worker.finished.connect(
            lambda _mid, path, err, u=uid: self._on_download_done(u, path, err)
        )
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda u=uid: self._cleanup_dl(u))
        thread.start()

    def _cleanup_dl(self, uid: str):
        self._dl_threads.pop(uid, None)
        self._dl_workers.pop(uid, None)

    def _on_download_done(self, uid: str, local_path: str, error: str):
        card = self._cards.get(uid)
        if error or not local_path:
            log.error("Stock download error: %s", error)
            if card:
                card.set_error(error or "Download failed")
            return
        if card:
            card.set_done()
        self._add_to_project(local_path)

    def _add_to_project(self, local_path: str):
        """Import the downloaded file into Project Files (re-tag if already present)."""
        try:
            from classes.app import get_app
            from classes.query import File
            app = get_app()
            if app and getattr(app, "window", None):
                files_model = app.window.files_model
                existing = File.get(path=local_path)
                if existing:
                    if not (existing.data.get("ai_metadata") or {}).get("analyzed"):
                        files_model._tag_file_async(existing.id)
                else:
                    files_model.add_files([local_path])
                    log.info("Stock media added to Project Files: %s", local_path)
        except Exception as exc:
            log.error("Failed to add stock media to Project Files: %s", exc)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _cols(self, card_w: int) -> int:
        return max(1, (self.width() - 24) // (card_w + 6))

    def _clear_grid(self, grid: QGridLayout):
        while grid.count():
            item = grid.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _reflow(self, grid: QGridLayout, card_w: int):
        if grid.count() == 0:
            return
        cols = self._cols(card_w)
        widgets = []
        while grid.count():
            item = grid.takeAt(0)
            if item and item.widget():
                widgets.append(item.widget())
        for idx, w in enumerate(widgets):
            row, col = divmod(idx, cols)
            grid.addWidget(w, row, col)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow(self._footage_grid, VIDEO_W)
        self._reflow(self._music_grid, SOUND_W)

    def _cleanup_threads(self):
        threads = list(self._search_threads) + list(self._dl_threads.values())
        for t in threads:
            if t and t.isRunning():
                t.quit()
                t.wait(2000)
        self._dl_threads.clear()
        self._dl_workers.clear()
