"""
Plan dock — Cursor-style markdown plan with todo checkboxes.
"""

import json
import os

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QDockWidget, QLabel

from classes.bridge_guard import guarded_slot
from classes.logger import log
from windows.embedded_web import attach_webkit_window_object, run_js, web_embed_backend


class PlanDockBridge(QObject):
    """Bridge between Python and the plan dock Web UI."""

    execute_requested = pyqtSignal(str)
    edit_plan_requested = pyqtSignal()

    @guarded_slot(str)
    def executePlan(self, plan_id: str = ""):
        self.execute_requested.emit(plan_id or "")

    @guarded_slot()
    def editPlanInPlanningMode(self):
        self.edit_plan_requested.emit()


class PlanDock(QDockWidget):
    """Single plan dock: full edit plan with markdown-style todos."""

    execute_requested = pyqtSignal(str)
    edit_plan_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Plan", parent)
        self.setObjectName("dockPlan")
        self.setAllowedAreas(Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self.current_plan = None
        self.bridge = None
        self.web_view = None
        self.channel = None
        self._embed_backend = web_embed_backend()

        self._setup_ui()

    def _html_path(self) -> str:
        from classes import info
        return os.path.join(info.PATH, "plan_ui", "plan.html")

    def _setup_ui(self):
        if self._embed_backend is None:
            label = QLabel(
                "Qt WebEngine and Qt WebKit are unavailable.\n"
                "Plan dock cannot display."
            )
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        self.bridge = PlanDockBridge()
        self.bridge.execute_requested.connect(self.execute_requested.emit)
        self.bridge.edit_plan_requested.connect(self.edit_plan_requested.emit)

        html_path = self._html_path()

        if self._embed_backend == "webengine":
            from PyQt5.QtWebEngineWidgets import QWebEngineView
            from PyQt5.QtWebChannel import QWebChannel

            self.web_view = QWebEngineView()
            self.channel = QWebChannel()
            self.channel.registerObject("planDockBridge", self.bridge)
            self.web_view.page().setWebChannel(self.channel)
        else:
            from PyQt5.QtWebKitWidgets import QWebView

            self.web_view = QWebView()
            attach_webkit_window_object(self.web_view, "planDockBridge", self.bridge)

        if os.path.exists(html_path):
            self.web_view.load(QUrl.fromLocalFile(html_path))
        else:
            log.error("Plan dock HTML not found: %s", html_path)
            self.web_view.setHtml("<html><body><p>Plan UI not found.</p></body></html>")

        self.setWidget(self.web_view)
        self.setMinimumSize(280, 200)

    def load_plan(self, plan) -> None:
        if not self.web_view or not self._embed_backend:
            return
        try:
            if hasattr(plan, "to_dict"):
                plan_data = plan.to_dict()
            elif isinstance(plan, dict):
                plan_data = plan
            else:
                plan_data = {"title": str(plan), "steps": []}
            self.current_plan = plan_data
            plan_json = json.dumps(plan_data)
            run_js(
                self.web_view,
                self._embed_backend,
                "if(window.loadPlan) window.loadPlan(%s);" % json.dumps(plan_json),
            )
            self.show()
            self.raise_()
        except Exception as e:
            log.error("Failed to load plan into dock: %s", e, exc_info=True)

    def update_step_status(self, step_id: str, status: str, error: str = "") -> None:
        if not self.web_view or not self._embed_backend or not step_id:
            return
        run_js(
            self.web_view,
            self._embed_backend,
            "if(window.updateStepStatus) window.updateStepStatus(%s, %s, %s);"
            % (json.dumps(step_id), json.dumps(status or ""), json.dumps(error or "")),
        )
