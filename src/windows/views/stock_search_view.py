"""
Unified media browser for the Files panel.

A single scroll area stacks three titled sections:
  * "Project Files" — the real project-files QListView, embedded here so it
    scrolls together with the stock results (drag-to-timeline, context menus and
    the hover quick-actions all keep working — it is the same widget).
  * "Stock Footage" — Pixabay results (Pexels when Pixabay has none).
  * "Stock music" — Openverse results (CC BY / CC0 / public domain).

The stock sections only appear while a query is active. The search workers,
download workers, result cards and async image loaders are reused as-is from
``pexels_dock`` and ``freesound_dock``.
"""

from typing import Dict

from qt_api import Qt, QSize, QThread, QEvent, QObject, pyqtSignal, pyqtSlot
from qt_api import QImage, QPixmap, QCursor
from qt_api import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QGridLayout, QLabel, QApplication,
    QPushButton, QLineEdit, QComboBox, QProgressBar, QSizePolicy,
)

from classes.app import get_app
from classes.file_drop import (
    EMPTY_FILES_DROP_MIN_HEIGHT,
    accept_os_file_drag,
    mime_has_file_drop,
    urls_from_mime,
)
from classes.logger import log
from themes.cosmic import tokens

from windows.pexels_dock import (
    _SearchWorker as PexelsSearchWorker,
    _DownloadWorker as PexelsDownloadWorker,
    _VideoCard, _ThumbnailRunnable, image_pool,
    CARD_W as VIDEO_W,
)
from windows.freesound_dock import (
    _SearchWorker as FreesoundSearchWorker,
    _DownloadWorker as FreesoundDownloadWorker,
    _SoundCard, _WaveformRunnable, _image_url,
    CARD_W as SOUND_W,
)
from windows.views.files_thumbnail_overlay import tile_grid_size


_STYLESHEET = tokens.resolve("""
    QWidget#StockSearchView { background: {surface}; }
    QScrollArea { background: {surface}; border: none; }
    QWidget#scrollContents { background: {surface}; }
    QLabel#sectionHeader { color: {text}; font-size: 12px; font-weight: 600;
        padding: 8px 2px 2px 2px; }
    QLabel#sectionStatus { color: {text_faint}; font-size: 11px; padding-left: 2px; }
""")

# Project-file thumbnails; their tile grid comes from tile_grid_size.
FILE_ICON = QSize(128, 72)  # 16:9; two columns fit the default panel width
# Open area under the tiles when the dock is short, so it still reads as room for more files.
FILE_CANVAS_MIN = 320
_FILES_STYLE = tokens.resolve("""
    QListView { background: {surface}; border: none; }
""")


def _same_path(a: str, b: str) -> bool:
    import os
    norm = lambda p: os.path.normcase(os.path.normpath(str(p or "").replace("\\", "/")))
    return bool(a) and norm(a) == norm(b)


def _length_for(prompt: str, lyrics: str) -> float:
    """Seconds for this request. A time written in the prompt wins; a song grows with its lines."""
    import re
    match = re.search(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|min|seconds?|secs?|sec|s)\b",
                      "%s\n%s" % (prompt or "", lyrics or ""), re.I)
    if match:
        seconds = float(match.group(1))
        if match.group(2).lower().startswith("min"):
            seconds *= 60
        return max(5.0, min(seconds, 240.0))
    lines = [line for line in (lyrics or "").splitlines()
             if line.strip() and not line.strip().startswith("[")]
    if lines:
        # A sung line needs a section, not the short instrumental bed.
        return max(45.0, min(240.0, 12.0 + 8.0 * len(lines)))
    return 30.0


class _MusicGenWorker(QObject):
    """Generates a bed or song on this computer, off the GUI thread."""
    finished = pyqtSignal(dict)   # {path, error, query, engine}
    progress = pyqtSignal(str)

    def __init__(self, prompt: str, lyrics: str, engine: str, gen: int, kind: str = "bed"):
        super().__init__()
        self._prompt = prompt
        self._lyrics = lyrics
        self._engine = engine
        self._gen = gen
        self._kind = kind

    @pyqtSlot()
    def run(self):
        path, error, declined = "", "", False
        try:
            from classes import music_local
            from classes.editor_tools import ai_generation_music as music
            music.ensure_installed(self._engine, progress=self.progress.emit)
            if self._kind == "song":
                from classes.song_lyrics import prepare_lyrics
                if (self._lyrics or "").strip():
                    self.progress.emit("Rendering the song with your lyrics. ACE-Step is quicker; YuE2 takes several minutes.")
                else:
                    self.progress.emit("Writing lyrics from the description with the indexing model...")
                lyrics = prepare_lyrics(self._prompt, self._lyrics)
                self.progress.emit("Rendering the song. ACE-Step is quicker; YuE2 takes several minutes.")
            else:
                lyrics = ""
                self.progress.emit("Rendering the instrumental bed. ACE-Step is quicker; YuE2 takes several minutes.")
            path = music_local.generate(
                self._engine, self._prompt, lyrics, _length_for(self._prompt, lyrics))
        except music.Declined as exc:
            error, declined = str(exc), True
        except Exception as exc:
            error = str(exc)
        self.finished.emit({"path": path, "error": error, "declined": declined, "query": self._prompt,
                            "engine": self._engine, "gen": self._gen})


class StockSearchView(QWidget):
    """Project files + stock footage + stock music, sharing one scroll area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StockSearchView")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(_STYLESHEET)

        self._query = ""
        self._files_view = None
        self._search_threads = []
        self._search_workers = []
        # Download threads/workers keyed by a provider-prefixed uid ("v123"/"s456")
        # so a Pexels id and a Freesound id can never clobber each other.
        self._dl_threads: Dict[str, QThread] = {}
        self._dl_workers: Dict[str, object] = {}
        self._cards: Dict[str, object] = {}
        # Bumped on every new search: late pages and pictures from an older one are dropped.
        self._search_gen = 0
        self._failed = False   # a failed or rate-limited section lets the same query run again
        self._pages = {"footage": 1, "music": 1}
        # Picture loaders carry an int key; Openverse ids are strings, so map key -> card uid.
        self._image_keys: Dict[int, str] = {}
        # Credit lines by card uid, taken at click time: the card may be gone by the time
        # its download finishes (a new search clears the grid).
        self._credits: Dict[str, str] = {}
        self._pool = image_pool()
        self._bed_busy = False
        self._declined_engines = set()   # a "no" on one model does not block the other
        self._prompt_seed = ""

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
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("scrollContents")
        self._scroll = scroll
        cv = QVBoxLayout(content)
        cv.setContentsMargins(8, 0, 8, 0)
        cv.setSpacing(0)

        # Project Files section — the real QListView is injected via set_files_view().
        # The block stays at least half the dock tall, so the generator starts at the middle
        # until the files grow and push it down the scroll.
        self._files_block = QWidget()
        files_layout = QVBoxLayout(self._files_block)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.setSpacing(2)
        self._files_header = QLabel("Project Files")
        self._files_header.setObjectName("sectionHeader")
        files_layout.addWidget(self._files_header)
        self._files_holder = QWidget()
        self._files_holder_layout = QVBoxLayout(self._files_holder)
        self._files_holder_layout.setContentsMargins(0, 2, 0, 6)
        self._files_holder_layout.setSpacing(0)
        self._files_empty = QLabel("No project files match this search.")
        self._files_empty.setObjectName("sectionStatus")
        self._files_empty.hide()
        files_layout.addWidget(self._files_empty)
        files_layout.addWidget(self._files_holder)
        files_layout.addStretch(1)
        cv.addWidget(self._files_block)
        self._build_music_composer(cv)

        # Stock Footage section (hidden until a query is active).
        self._footage_header = QLabel("Stock Footage")
        self._footage_header.setObjectName("sectionHeader")
        cv.addWidget(self._footage_header)
        self._footage_status = QLabel("")
        self._footage_status.setObjectName("sectionStatus")
        self._footage_status.setTextFormat(Qt.PlainText)   # provider text is never markup
        cv.addWidget(self._footage_status)
        self._footage_container = QWidget()
        self._footage_grid = QGridLayout(self._footage_container)
        self._footage_grid.setContentsMargins(0, 4, 0, 8)
        self._footage_grid.setSpacing(8)
        cv.addWidget(self._footage_container)
        self._footage_more = QPushButton("Load more footage")
        self._footage_more.clicked.connect(lambda: self._load_more("footage"))
        cv.addWidget(self._footage_more)

        # Stock music section (hidden until a query is active).
        self._music_header = QLabel("Stock music")
        self._music_header.setObjectName("sectionHeader")
        cv.addWidget(self._music_header)
        self._music_status = QLabel("")
        self._music_status.setObjectName("sectionStatus")
        self._music_status.setTextFormat(Qt.PlainText)
        cv.addWidget(self._music_status)
        self._music_container = QWidget()
        self._music_grid = QGridLayout(self._music_container)
        self._music_grid.setContentsMargins(0, 4, 0, 8)
        self._music_grid.setSpacing(8)
        cv.addWidget(self._music_container)
        self._music_more = QPushButton("Load more music")
        self._music_more.clicked.connect(lambda: self._load_more("music"))
        cv.addWidget(self._music_more)

        cv.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self.setAcceptDrops(True)
        scroll.setAcceptDrops(True)
        scroll.viewport().setAcceptDrops(True)
        content.setAcceptDrops(True)
        self._files_holder.setAcceptDrops(True)
        self._files_block.setAcceptDrops(True)
        for widget in (
            self, scroll, scroll.viewport(), content,
            self._files_block, self._files_holder, self._files_header,
        ):
            widget.installEventFilter(self)

        self._set_stock_visible(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._pin_files_half()

    def showEvent(self, event):
        super().showEvent(event)
        self._fill_dock()
        self._pin_files_half()

    def _fill_dock(self):
        """Take the free height of the Files dock. A preferred-size widget leaves Music Gen at the top."""
        widget = self
        while widget is not None:
            parent = widget.parentWidget()
            if parent is None:
                return
            layout = parent.layout()
            if layout is not None and layout.indexOf(widget) >= 0:
                # PyQt often hands back a plain QLayout here, which has no stretch method.
                stretch = getattr(layout, "setStretchFactor", None)
                if stretch is not None:
                    stretch(widget, 1)
                return
            widget = parent

    def _pin_files_half(self):
        height = max(self.height(), self._scroll.viewport().height())
        if height <= 0:
            return
        # Half the dock, and never a short strip: the open area is where more files land.
        canvas = max(FILE_CANVAS_MIN, height // 2)
        if self._files_block.minimumHeight() != canvas:
            self._files_block.setMinimumHeight(canvas)

    def _build_music_composer(self, parent_layout):
        """Generator sits under the files. A search is optional; the prompt can stay empty."""
        self._music_rule = QWidget()
        self._music_rule.setFixedHeight(1)
        self._music_rule.setAttribute(Qt.WA_StyledBackground, True)
        self._music_rule.setStyleSheet(tokens.resolve("background: {line_strong};"))
        parent_layout.addWidget(self._music_rule)

        box = QWidget()
        box.setObjectName("musicComposer")
        self._music_composer = box
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 16)
        layout.setSpacing(8)

        self._music_gen_header = QLabel("Music Gen")
        self._music_gen_header.setObjectName("sectionHeader")
        layout.addWidget(self._music_gen_header)

        self._music_prompt = QLineEdit()
        self._music_prompt.setPlaceholderText("Describe the music")
        self._music_prompt.returnPressed.connect(self._on_generate_music)
        self._music_prompt.textChanged.connect(self._sync_generate_enabled)
        layout.addWidget(self._music_prompt)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._music_kind = QComboBox()
        self._music_kind.addItem("Music bed", "bed")
        self._music_kind.addItem("Song", "song")
        self._music_engine = QComboBox()
        self._music_engine.addItem("ACE-Step 1.5", "ace_step")
        self._music_engine.addItem("YuE2 (non-commercial)", "yue2")
        row.addWidget(self._music_kind, 1)
        row.addWidget(self._music_engine, 1)
        layout.addLayout(row)

        self._music_lyrics = QLineEdit()
        self._music_lyrics.setPlaceholderText("Lyrics, or leave empty to sing the description")
        self._music_lyrics.returnPressed.connect(self._on_generate_music)
        self._music_lyrics.hide()
        self._music_kind.currentIndexChanged.connect(self._on_music_kind_changed)
        layout.addWidget(self._music_lyrics)

        self._music_generate = QPushButton("Generate")
        self._music_generate.setObjectName("acceptButton")
        self._music_generate.clicked.connect(self._on_generate_music)
        self._music_generate.setEnabled(False)
        layout.addWidget(self._music_generate)

        self._gen_progress = QProgressBar()
        self._gen_progress.setTextVisible(False)
        self._gen_progress.setFixedHeight(3)
        self._gen_progress.setStyleSheet(tokens.resolve(
            "QProgressBar { background: {sunken}; border: none; border-radius: 1px; }"
            "QProgressBar::chunk { background: {accent}; border-radius: 1px; }"
        ))
        self._gen_progress.hide()
        layout.addWidget(self._gen_progress)

        self._gen_status = QLabel("")
        self._gen_status.setObjectName("sectionStatus")
        self._gen_status.setTextFormat(Qt.PlainText)
        self._gen_status.setWordWrap(True)
        self._gen_status.hide()
        layout.addWidget(self._gen_status)
        parent_layout.addWidget(box)

    def _on_music_kind_changed(self):
        self._music_lyrics.setVisible(self._music_kind.currentData() == "song")

    def _show_gen_progress(self, message: str):
        self._gen_status.setText(message)
        self._gen_status.show()
        tail = message.rstrip()
        pct = None
        if tail.endswith("%"):
            digits = tail[:-1].rsplit(" ", 1)[-1]
            if digits.isdigit():
                pct = int(digits)
        self._gen_progress.show()
        if pct is None:
            self._gen_progress.setRange(0, 0)
        else:
            self._gen_progress.setRange(0, 100)
            self._gen_progress.setValue(pct)

    def set_files_view(self, files_view):
        """Embed the project-files QListView as the top section and restyle it
        to match the stock cards. The view keeps its own behaviour (drag, menu)."""
        self._files_view = files_view
        files_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        files_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        files_view.setIconSize(FILE_ICON)
        files_view.setGridSize(tile_grid_size(FILE_ICON))
        files_view.setStyleSheet(_FILES_STYLE)
        self._files_holder_layout.addWidget(files_view)

        # Refit the embedded view's height to its contents so the outer scroll
        # area owns the only scrollbar.
        try:
            files_view.files_model.ModelRefreshed.connect(self._refit_files)
        except Exception:
            pass
        model = files_view.model()
        if model is not None:
            model.layoutChanged.connect(self._refit_files)
            model.rowsInserted.connect(lambda *a: self._refit_files())
            model.rowsRemoved.connect(lambda *a: self._refit_files())
            model.modelReset.connect(self._refit_files)
        self._refit_files()

    def _refit_files(self):
        view = self._files_view
        if view is None:
            return
        model = view.model()
        count = model.rowCount() if model is not None else 0
        try:
            total = view.files_model.proxy_model.sourceModel().rowCount()
        except Exception:
            total = count
        self._files_empty.setVisible(count == 0 and total > 0)
        grid = view.gridSize()
        gw = grid.width() or 110
        gh = grid.height() or 95
        width = view.viewport().width() or self.width() or 320
        cols = max(1, width // gw)
        rows = (count + cols - 1) // cols
        content_h = rows * gh + 8 if rows else 0
        view.setFixedHeight(max(content_h, EMPTY_FILES_DROP_MIN_HEIGHT))
        self._pin_files_half()

    def eventFilter(self, obj, event):
        # Catch Finder drops on the scroll area / empty Project Files space.
        # The embedded list view keeps its own handlers for drops on cards.
        etype = event.type()
        if obj is self._scroll.viewport() and etype == QEvent.Resize:
            self._pin_files_half()
            return False
        if etype in (QEvent.DragEnter, QEvent.DragMove):
            if mime_has_file_drop(event.mimeData()):
                accept_os_file_drag(event)
                return True
            return False
        if etype == QEvent.Drop:
            if mime_has_file_drop(event.mimeData()):
                self._import_os_drop(event)
                return True
            return False
        return super().eventFilter(obj, event)

    def _import_os_drop(self, event):
        urls = urls_from_mime(event.mimeData())
        if not urls:
            event.ignore()
            return
        event.accept()
        files_model = None
        if self._files_view is not None:
            files_model = getattr(self._files_view, "files_model", None)
        if files_model is None:
            try:
                files_model = get_app().window.files_model
            except Exception:
                files_model = None
        if files_model is None:
            return
        try:
            get_app().setOverrideCursor(QCursor(Qt.WaitCursor))
            log.info("Processing Files dock drop for %s urls", len(urls))
            files_model.process_urls(urls)
        finally:
            get_app().restoreOverrideCursor()

    # ── Public API ──────────────────────────────────────────────────────────────

    def run_search(self, query: str):
        """Search both stock providers for ``query`` and show the two sections."""
        query = (query or "").strip()
        if not query:
            self.clear_stock()
            return
        if query == self._query and not self._failed:
            return
        self._query = query
        self._failed = False
        self._clear_grids()
        self._set_stock_visible(True)
        self._offer_local_prompt(query)
        self._footage_status.setText("Searching…")
        self._music_status.setText("Searching…")
        self._search_page("footage", 1)
        self._search_page("music", 1)

    def _search_page(self, section: str, page: int):
        self._pages[section] = page
        gen = self._search_gen
        if section == "footage":
            worker, done = PexelsSearchWorker(self._query, page), self._on_footage_results
        else:
            worker, done = FreesoundSearchWorker(self._query, page), self._on_music_results
        self._start_search(worker, lambda data, g=gen: done(data) if g == self._search_gen else None)

    def _load_more(self, section: str):
        more = self._footage_more if section == "footage" else self._music_more
        more.setEnabled(False)
        more.setText("Loading…")
        self._search_page(section, self._pages[section] + 1)

    def _show_more(self, more: QPushButton, section: str, shown: int, total: int):
        more.setText("Load more " + section)
        more.setEnabled(True)
        more.setVisible(shown < total)

    def clear_stock(self):
        """Hide and empty the stock sections (project files stay visible)."""
        self._query = ""
        self._clear_grids()
        self._footage_status.setText("")
        self._music_status.setText("")
        self._set_stock_visible(False)

    # ── Section visibility ────────────────────────────────────────────────────────

    def _set_stock_visible(self, visible: bool):
        for w in (self._footage_header, self._footage_status, self._footage_container,
                  self._music_header, self._music_status, self._music_container):
            w.setVisible(visible)
        if not visible:
            self._footage_more.hide()
            self._music_more.hide()

    def _clear_grids(self):
        self._search_gen += 1
        self._clear_grid(self._footage_grid)
        self._clear_grid(self._music_grid)
        self._cards.clear()
        self._image_keys.clear()
        self._footage_more.hide()
        self._music_more.hide()

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

    def _image_key(self, uid: str) -> int:
        key = len(self._image_keys)
        self._image_keys[key] = uid
        return key

    def _load_image(self, runnable_cls, uid: str, url: str, on_ready):
        gen = self._search_gen
        # A queued fetch for a superseded search is skipped, so it cannot hold up the next one.
        runnable = runnable_cls(self._image_key(uid), url, gen, alive=lambda g=gen: g == self._search_gen)
        runnable.signals.ready.connect(on_ready)
        self._pool.start(runnable)

    @pyqtSlot(dict)
    def _on_footage_results(self, data: dict):
        error = data.get("error")
        videos = data.get("videos", [])
        first = self._footage_grid.count()
        if error or not videos:
            self._failed = self._failed or bool(error)
            if first and error:   # a later page failed: keep what is shown, offer that page again
                self._pages["footage"] -= 1
                self._footage_status.setText(f"Error: {error}")
                self._show_more(self._footage_more, "footage", 0, 1)
                return
            if not first:
                self._footage_status.setText(f"Error: {error}" if error else "No footage found.")
            self._footage_more.hide()
            return
        total = int(data.get("total_results", 0) or 0)
        self._footage_status.setText(f"{total:,} results")
        provider = data.get("provider") or "pexels"

        cols = self._cols(VIDEO_W)
        for idx, video in enumerate(videos, start=first):
            video = dict(video, provider=provider)
            uid = f"v{video.get('id', idx)}"
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
            thumb_url = thumb_url or video.get("image", "")
            if thumb_url:
                self._load_image(_ThumbnailRunnable, uid, thumb_url, self._on_thumbnail_ready)
        self._show_more(self._footage_more, "footage", self._footage_grid.count(), total)

    @pyqtSlot(dict)
    def _on_music_results(self, data: dict):
        error = data.get("error")
        sounds = data.get("sounds", [])
        first = self._music_grid.count()
        if error or not sounds:
            self._failed = self._failed or bool(error)
            if first and error:
                self._pages["music"] -= 1
                self._music_status.setText(f"Error: {error}")
                self._show_more(self._music_more, "music", 0, 1)
                return
            self._music_more.hide()
            if not first:
                self._music_status.setText(error if error else "No stock music found.")
                self._offer_local_prompt(self._query)
            return
        total = int(data.get("count", 0) or 0)
        self._music_status.setText(f"{total:,} results")

        cols = self._cols(SOUND_W)
        for idx, sound in enumerate(sounds, start=first):
            uid = f"s{sound.get('id', idx)}"
            card = _SoundCard(sound)
            card.clicked.connect(self._on_music_clicked)
            self._cards[uid] = card
            row, col = divmod(idx, cols)
            self._music_grid.addWidget(card, row, col)

            # Openverse has no waveform image; its album art fills the same slot.
            image_url = _image_url(sound)
            if image_url:
                self._load_image(_WaveformRunnable, uid, image_url, self._on_waveform_ready)
        self._show_more(self._music_more, "music", self._music_grid.count(), total)

    def _offer_local_prompt(self, query: str):
        """Fill the prompt from the search until the user edits it."""
        current = self._music_prompt.text().strip()
        if not current or current == self._prompt_seed:
            self._music_prompt.setText(query)
            self._prompt_seed = query
        self._sync_generate_enabled()

    def _sync_generate_enabled(self):
        self._music_generate.setEnabled(bool(self._music_prompt.text().strip()) and not self._bed_busy)

    def _on_generate_music(self):
        prompt = self._music_prompt.text().strip()
        kind = self._music_kind.itemData(self._music_kind.currentIndex())
        typed = self._music_lyrics.text() if kind == "song" else ""
        self._start_local_bed(
            prompt,
            self._music_engine.itemData(self._music_engine.currentIndex()) or "ace_step",
            typed,
            kind or "bed",
        )

    @pyqtSlot(str)
    def _on_music_progress(self, message: str):
        self._show_gen_progress(message)

    def _start_local_bed(self, prompt: str, engine: str = "ace_step", lyrics: str = "", kind: str = "bed"):
        """Generate a bed or song on this computer and add it to Project Files."""
        engine = engine or "ace_step"
        prompt = str(prompt or "").strip()
        if not prompt:
            self._gen_status.setText("Describe the music first.")
            self._gen_status.show()
            return
        if engine in self._declined_engines or self._bed_busy:
            return
        self._bed_busy = True
        self._sync_generate_enabled()
        self._show_gen_progress("Preparing music generation...")
        worker = _MusicGenWorker(prompt, lyrics or "", engine, self._search_gen, kind or "bed")
        worker.progress.connect(self._on_music_progress)
        self._start_search(worker, self._on_local_bed_done)

    @pyqtSlot(dict)
    def _on_local_bed_done(self, data: dict):
        self._bed_busy = False
        self._sync_generate_enabled()
        self._gen_progress.hide()
        error, path = data.get("error", ""), data.get("path", "")
        current = data.get("gen") == self._search_gen
        if error:
            if data.get("declined") and data.get("engine"):
                self._declined_engines.add(data["engine"])
            log.warning("Local music generation failed: %s", error)
            if current:
                self._gen_status.setText("Local generation is off for this model (download declined)."
                                         if data.get("declined") else f"Local music generation failed: {error}")
                self._gen_status.show()
            return
        if not current:   # the user searched for something else meanwhile; the file stays in Downloads
            log.info("Local music for an earlier search not imported: %s", path)
            return
        self._add_to_project(path)
        self._gen_status.setText("Generated the track and added it to Project Files.")
        self._gen_status.show()

    def _card_for_image(self, key: int, search: int):
        return self._cards.get(self._image_keys.get(key, "")) if search == self._search_gen else None

    @pyqtSlot(int, QImage, int)
    def _on_thumbnail_ready(self, key: int, image: QImage, search: int):
        card = self._card_for_image(key, search)
        if card:
            card.set_thumbnail(QPixmap.fromImage(image))

    @pyqtSlot(object, QImage, int)
    def _on_waveform_ready(self, key: int, image: QImage, search: int):
        card = self._card_for_image(key, search)
        if card:
            card.set_waveform(QPixmap.fromImage(image))

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
        # Pixabay and Pexels ids overlap, and a download reuses a same-named file.
        name = f"{video.get('provider') or 'pexels'}_{vid_id}"
        worker = PexelsDownloadWorker(vid_id, chosen.get("link", ""), name)
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
        raw_name = sound.get("name", "") or f"openverse_{sid}"
        safe_name = re.sub(r"[^\w\-]", "_", raw_name)[:60]
        if card:
            card.set_downloading(True)
        if sound.get("attribution"):
            self._credits[uid] = sound["attribution"]
        worker = FreesoundDownloadWorker(sid, preview_url, f"openverse_{sid}_{safe_name}")
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
        credit = self._credits.pop(uid, "") or (getattr(card, "_sound", None) or {}).get("attribution", "")
        if credit:
            self._keep_credit(local_path, credit)

    def _keep_credit(self, local_path: str, credit: str):
        """CC BY needs the credit with the clip, not only on the search card."""
        try:
            from classes.tool_handlers import resolve_imported_file
            f = resolve_imported_file(local_path)
            # The resolver falls back to a same-named file; a credit must only go on this one.
            if not f or not _same_path(f.absolute_path(), local_path):
                log.warning("Imported %s but could not find it to store its credit", local_path)
                return
            f.data["stock_credit"] = credit
            # Metadata, not an edit: no undo step, and a pending action (an effect drag) is kept.
            get_app().updates.update_untracked(f.key or ["files", {"id": f.id}], f.data)
        except Exception as exc:
            log.warning("Could not store the stock credit for %s: %s", local_path, exc)

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
                        files_model._index_file_async(existing.id)
                else:
                    files_model.add_files([local_path], quiet=True)   # an unreadable file is logged, no modal
                    log.info("Stock media added to Project Files: %s", local_path)
        except Exception as exc:
            log.error("Failed to add stock media to Project Files: %s", exc)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _cols(self, card_w: int) -> int:
        return max(1, (self.width() - 24) // (card_w + 8))

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
        self._refit_files()
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
