"""
Plan Graph dock: shows the hierarchy of what the AI did during an edit run.
"""

import json
import os

from PyQt5.QtCore import QFileInfo, Qt, QUrl, pyqtSlot
from PyQt5.QtWidgets import QDockWidget, QLabel, QSizePolicy

from windows.embedded_web import web_embed_backend, run_js


class PlanGraphDock(QDockWidget):
    """Dock that displays the edit plan graph (root -> branches -> steps)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dockPlanGraph")
        self.setWindowTitle("Plan Graph")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        self._view = None
        self._embed_backend = web_embed_backend()

        if self._embed_backend is None:
            label = QLabel(
                "Qt WebEngine and Qt WebKit are unavailable.\n"
                "Plan Graph cannot display (install PyQt5 WebEngine or WebKit)."
            )
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        if self._embed_backend == "webengine":
            from PyQt5.QtWebEngineWidgets import QWebEngineView

            self._view = QWebEngineView(self)
        else:
            from PyQt5.QtWebKitWidgets import QWebView

            self._view = QWebView(self)

        self._view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setWidget(self._view)
        self.setMinimumSize(240, 160)

        ui_dir = os.path.join(os.path.dirname(__file__), "ui")
        index_path = os.path.join(ui_dir, "index.html")
        if os.path.isfile(index_path):
            base_url = QUrl.fromLocalFile(QFileInfo(index_path).absoluteFilePath())
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
            self._view.setHtml(html, base_url)
        else:
            self._view.setHtml(
                "<p>Plan graph UI not found (plan_graph/ui/index.html).</p>",
                QUrl(),
            )

    @pyqtSlot(str)
    def set_plan_json(self, json_str: str) -> None:
        """Update the graph with new plan JSON."""
        if not json_str or self._view is None or self._embed_backend is None:
            return
        try:
            escaped = json.dumps(json_str)
        except Exception:
            escaped = json.dumps("null")
        run_js(self._view, self._embed_backend, "setPlanGraph(%s);" % escaped)
