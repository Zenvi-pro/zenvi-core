"""
Director Selection Panel UI

PyQt dock widget with HTML/CSS/JS overlay for selecting and managing directors.
In the split architecture, director data is fetched from the zenvi-backend API.
"""

import os
import json
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtWidgets import QDockWidget, QLabel

from classes.logger import log
from windows.embedded_web import (
    attach_webkit_window_object,
    web_embed_backend,
)


class DirectorPanelBridge(QObject):
    """
    Bridge between Python and JavaScript for director panel UI.

    Exposed via QWebChannel (WebEngine) or addToJavaScriptWindowObject (WebKit)
    as 'directorPanelBridge'.
    """

    directorsLoaded = pyqtSignal(str)

    directors_selected = pyqtSignal(str)
    open_marketplace = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.directors = []

    @pyqtSlot(str)
    def deleteDirector(self, director_id: str):
        """Delete a director (called from JavaScript)."""
        try:
            user_dir = os.path.expanduser("~/.config/zenvi/directors")
            user_path = os.path.join(user_dir, f"{director_id}.director")

            if os.path.exists(user_path):
                os.remove(user_path)
                log.info(f"Deleted user director: {director_id}")
                self.loadDirectors()
            else:
                log.warning(f"Director not found or is a built-in: {director_id}")

        except Exception as e:
            log.error(f"Failed to delete director {director_id}: {e}", exc_info=True)

    @pyqtSlot()
    def loadDirectors(self):
        """Load available directors from backend API."""
        try:
            from classes.api_client import get_backend_client
            directors_data = get_backend_client().list_directors()
            if directors_data:
                self.directors = directors_data
                directors_json = json.dumps(directors_data)
                self.directorsLoaded.emit(directors_json)
                log.info(f"Loaded {len(directors_data)} directors from backend")
                return

            self._load_local_directors()

        except Exception as e:
            log.error(f"Failed to load directors: {e}", exc_info=True)
            self.directorsLoaded.emit("[]")

    def _load_local_directors(self):
        """Load directors from local .director files as fallback."""
        directors_data = []

        user_dir = os.path.expanduser("~/.config/zenvi/directors")
        if os.path.isdir(user_dir):
            for fname in os.listdir(user_dir):
                if fname.endswith(".director"):
                    fpath = os.path.join(user_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        directors_data.append({
                            "id": data.get("id", fname.replace(".director", "")),
                            "name": data.get("name", fname),
                            "description": data.get("description", ""),
                            "author": data.get("author", ""),
                            "version": data.get("version", "1.0.0"),
                            "tags": data.get("tags", []),
                            "expertise": data.get("personality", {}).get("expertise_areas", []),
                            "focus": data.get("personality", {}).get("analysis_focus", []),
                        })
                    except Exception as e:
                        log.warning(f"Failed to load director file {fpath}: {e}")

        from classes import info
        builtin_dir = os.path.join(info.PATH, "directors", "built_in")
        if os.path.isdir(builtin_dir):
            for fname in os.listdir(builtin_dir):
                if fname.endswith(".director"):
                    fpath = os.path.join(builtin_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        directors_data.append({
                            "id": data.get("id", fname.replace(".director", "")),
                            "name": data.get("name", fname),
                            "description": data.get("description", ""),
                            "author": data.get("author", ""),
                            "version": data.get("version", "1.0.0"),
                            "tags": data.get("tags", []),
                            "expertise": data.get("personality", {}).get("expertise_areas", []),
                            "focus": data.get("personality", {}).get("analysis_focus", []),
                        })
                    except Exception as e:
                        log.warning(f"Failed to load director file {fpath}: {e}")

        self.directors = directors_data
        directors_json = json.dumps(directors_data)
        self.directorsLoaded.emit(directors_json)
        log.info(f"Loaded {len(directors_data)} directors from local files")

    @pyqtSlot(str)
    def selectDirectors(self, director_ids_json: str):
        try:
            director_ids = json.loads(director_ids_json)
            log.info(f"Starting analysis with directors: {director_ids}")

            self.directors_selected.emit(director_ids_json)

            self._trigger_director_analysis(director_ids)

        except Exception as e:
            log.error(f"Failed to start director analysis: {e}", exc_info=True)

    def _trigger_director_analysis(self, director_ids):
        try:
            from classes.app import get_app
            app = get_app()

            directors_str = ", ".join(director_ids)
            message = (
                f"Run director analysis with directors: {directors_str}. "
                "Analyze the current video project and suggest improvements."
            )

            if hasattr(app, 'window') and hasattr(app.window, 'dockAIChat'):
                app.window.dockAIChat.send_message(message)
                log.info(
                    f"Sent director analysis request to backend for {len(director_ids)} directors"
                )
            else:
                log.warning("AI Chat dock not available for sending director analysis request")

        except Exception as e:
            log.error(f"Failed to trigger director analysis: {e}", exc_info=True)

    @pyqtSlot()
    def openMarketplace(self):
        log.info("Opening marketplace")
        self.open_marketplace.emit()


class DirectorPanelDockWidget(QDockWidget):
    """Dock widget for selecting directors (WebEngine or WebKit embedded HTML)."""

    directors_selected = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__("Directors", parent)

        self.bridge = None
        self.web_view = None
        self.channel = None
        self._embed_backend = web_embed_backend()

        self.setObjectName("director_panel_dock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._setup_ui()

    def _setup_ui(self):
        if self._embed_backend is None:
            label = QLabel(
                "Qt WebEngine and Qt WebKit are unavailable.\n"
                "Director panel cannot display."
            )
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        self.bridge = DirectorPanelBridge()
        self.bridge.directors_selected.connect(self._on_directors_selected)
        self.bridge.open_marketplace.connect(self._on_open_marketplace)

        from classes import info
        html_path = os.path.join(info.PATH, "timeline", "directors", "panel.html")

        if self._embed_backend == "webengine":
            from PyQt5.QtWebEngineWidgets import QWebEngineView
            from PyQt5.QtWebChannel import QWebChannel

            self.web_view = QWebEngineView()
            self.channel = QWebChannel()
            self.channel.registerObject("directorPanelBridge", self.bridge)
            self.web_view.page().setWebChannel(self.channel)

            if os.path.exists(html_path):
                self.web_view.load(QUrl.fromLocalFile(html_path))
                log.info(f"Loaded director panel UI from {html_path}")
            else:
                log.error(f"Director panel HTML not found: {html_path}")
                self.web_view.setHtml(
                    "<html><body><p>Director panel HTML not found.</p></body></html>"
                )
        else:
            from PyQt5.QtWebKitWidgets import QWebView

            self.web_view = QWebView()
            attach_webkit_window_object(self.web_view, "directorPanelBridge", self.bridge)
            if os.path.exists(html_path):
                self.web_view.load(QUrl.fromLocalFile(html_path))
                log.info(f"Loaded director panel UI (WebKit) from {html_path}")
            else:
                log.error(f"Director panel HTML not found: {html_path}")
                self.web_view.setHtml(
                    "<html><body><p>Director panel HTML not found.</p></body></html>"
                )

        self.setWidget(self.web_view)
        self.setMinimumSize(320, 240)

    def _on_directors_selected(self, director_ids_json: str):
        try:
            director_ids = json.loads(director_ids_json)
            self.directors_selected.emit(director_ids)
        except Exception as e:
            log.error(f"Failed to parse selected directors: {e}", exc_info=True)

    def _on_open_marketplace(self):
        try:
            from windows.director_marketplace_ui import show_marketplace_dialog
            show_marketplace_dialog(self)
            log.info("Opened marketplace dialog")
        except Exception as e:
            log.error(f"Failed to open marketplace: {e}", exc_info=True)


_director_panel_dock = None


def get_director_panel_dock(parent=None):
    global _director_panel_dock
    if _director_panel_dock is None:
        _director_panel_dock = DirectorPanelDockWidget(parent)
    return _director_panel_dock
