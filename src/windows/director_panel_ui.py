"""
Director Selection Panel UI

PyQt dock widget with HTML/CSS/JS overlay for selecting and managing directors.
"""

import os
import json
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtWidgets import QDockWidget, QWidget, QVBoxLayout
from classes.logger import log

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
    from PyQt5.QtWebChannel import QWebChannel
    _WEBENGINE_AVAILABLE = True
except ImportError:
    _WEBENGINE_AVAILABLE = False
    log.warning("QtWebEngine not available - Director Panel UI will not work")


class DirectorPanelBridge(QObject):
    """
    Bridge between Python and JavaScript for director panel UI.

    Exposed to JavaScript via QWebChannel as 'directorPanelBridge'.
    """

    # Signals to JavaScript
    directorsLoaded = pyqtSignal(str)  # directors JSON array

    # Signals to Python
    directors_selected = pyqtSignal(str)  # selected director IDs as JSON array
    open_marketplace = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.directors = []

    @pyqtSlot()
    def loadDirectors(self):
        """Load available directors (called from JavaScript on init)."""
        try:
            from classes.ai_directors.director_loader import get_director_loader

            loader = get_director_loader()
            directors = loader.list_available_directors()

            # Convert to JSON-serializable format
            directors_data = []
            for director in directors:
                directors_data.append({
                    'id': director.id,
                    'name': director.name,
                    'description': director.metadata.description,
                    'author': director.metadata.author,
                    'version': director.metadata.version,
                    'tags': director.metadata.tags,
                    'expertise': director.personality.expertise_areas,
                    'focus': director.personality.analysis_focus,
                })

            self.directors = directors_data
            directors_json = json.dumps(directors_data)
            self.directorsLoaded.emit(directors_json)

            log.info(f"Loaded {len(directors)} directors into panel")
        except Exception as e:
            log.error(f"Failed to load directors: {e}", exc_info=True)
            self.directorsLoaded.emit("[]")

    @pyqtSlot(str)
    def selectDirectors(self, director_ids_json: str):
        """
        Called from JavaScript when user selects directors.

        Args:
            director_ids_json: JSON array of director IDs
        """
        try:
            director_ids = json.loads(director_ids_json)
            log.info(f"Directors selected: {director_ids}")
            self.directors_selected.emit(director_ids_json)
        except Exception as e:
            log.error(f"Failed to parse selected directors: {e}", exc_info=True)

    @pyqtSlot()
    def openMarketplace(self):
        """Called from JavaScript when user clicks Browse Marketplace."""
        log.info("Opening marketplace")
        self.open_marketplace.emit()


class DirectorPanelDockWidget(QDockWidget):
    """
    Dock widget for selecting directors.

    Uses QWebEngineView with HTML/CSS/JS for modern, card-based UI.
    """

    # Signals
    directors_selected = pyqtSignal(list)  # List of director IDs

    def __init__(self, parent=None):
        super().__init__("Directors", parent)

        self.bridge = None
        self.web_view = None

        self.setObjectName("director_panel_dock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._setup_ui()

    def _setup_ui(self):
        """Setup the UI with web view."""
        if not _WEBENGINE_AVAILABLE:
            # Fallback: simple label
            from PyQt5.QtWidgets import QLabel
            label = QLabel("QtWebEngine not available.\nDirector Panel requires QtWebEngine.")
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        # Create web view
        self.web_view = QWebEngineView()

        # Create bridge for Python<->JavaScript communication
        self.bridge = DirectorPanelBridge()

        # Connect bridge signals
        self.bridge.directors_selected.connect(self._on_directors_selected)
        self.bridge.open_marketplace.connect(self._on_open_marketplace)

        # Setup web channel
        self.channel = QWebChannel()
        self.channel.registerObject("directorPanelBridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)

        # Load HTML
        from classes import info
        html_path = os.path.join(info.PATH, "timeline", "directors", "panel.html")

        if os.path.exists(html_path):
            url = QUrl.fromLocalFile(html_path)
            self.web_view.load(url)
            log.info(f"Loaded director panel UI from {html_path}")
        else:
            log.error(f"Director panel HTML not found: {html_path}")
            # Load placeholder
            self.web_view.setHtml("""
                <html>
                <body style="font-family: sans-serif; padding: 20px; text-align: center;">
                    <h2>Director Panel</h2>
                    <p>HTML file not found. UI components pending.</p>
                </body>
                </html>
            """)

        self.setWidget(self.web_view)

    def _on_directors_selected(self, director_ids_json: str):
        """Handle director selection."""
        try:
            director_ids = json.loads(director_ids_json)
            self.directors_selected.emit(director_ids)
        except Exception as e:
            log.error(f"Failed to parse selected directors: {e}", exc_info=True)

    def _on_open_marketplace(self):
        """Handle marketplace button click."""
        try:
            from windows.director_marketplace_ui import show_marketplace_dialog
            show_marketplace_dialog(self)
            log.info("Opened marketplace dialog")
        except Exception as e:
            log.error(f"Failed to open marketplace: {e}", exc_info=True)


# Global instance
_director_panel_dock = None


def get_director_panel_dock(parent=None):
    """Get or create global DirectorPanelDockWidget instance."""
    global _director_panel_dock
    if _director_panel_dock is None:
        _director_panel_dock = DirectorPanelDockWidget(parent)
    return _director_panel_dock
