"""
Director Plan Review UI

PyQt dock widget with HTML/CSS/JS overlay for reviewing and approving director plans.
In the split architecture, plan approval/rejection is communicated back to the backend.
"""

import os
import json
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtWidgets import QDockWidget, QLabel

from classes.logger import log
from windows.embedded_web import attach_webkit_window_object, web_embed_backend


class PlanReviewBridge(QObject):
    """
    Bridge between Python and JavaScript for plan review UI.

    Exposed as 'planReviewBridge' (WebChannel or WebKit window object).
    """

    planLoaded = pyqtSignal(str)

    plan_approved = pyqtSignal(str)
    plan_rejected = pyqtSignal(str)
    plan_modified = pyqtSignal(str, str)
    step_toggled = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        self.current_plan = None

    @pyqtSlot(str)
    def loadPlan(self, plan_json: str):
        try:
            plan_data = json.loads(plan_json)
            self.current_plan = plan_data
            self.planLoaded.emit(plan_json)
            log.info(f"Loaded plan into UI: {plan_data.get('plan_id')}")
        except Exception as e:
            log.error(f"Failed to load plan: {e}", exc_info=True)

    @pyqtSlot(str)
    def approvePlan(self, plan_id: str):
        log.info(f"Plan approved: {plan_id}")
        self.plan_approved.emit(plan_id)

    @pyqtSlot(str)
    def rejectPlan(self, plan_id: str):
        log.info(f"Plan rejected: {plan_id}")
        self.plan_rejected.emit(plan_id)

    @pyqtSlot(str, str)
    def modifyPlan(self, plan_id: str, modifications_json: str):
        log.info(f"Plan modified: {plan_id}")
        self.plan_modified.emit(plan_id, modifications_json)

    @pyqtSlot(str, bool)
    def toggleStep(self, step_id: str, enabled: bool):
        log.info(f"Step toggled: {step_id} -> {enabled}")
        self.step_toggled.emit(step_id, enabled)


class PlanReviewDockWidget(QDockWidget):
    """Dock widget for reviewing and approving director plans."""

    plan_approved = pyqtSignal(str)
    plan_rejected = pyqtSignal(str)
    plan_modified = pyqtSignal(str, dict)

    def __init__(self, parent=None):
        super().__init__("Director Plan Review", parent)

        self.current_plan = None
        self.bridge = None
        self.web_view = None
        self.channel = None
        self._embed_backend = web_embed_backend()

        self.setObjectName("director_plan_review_dock")
        self.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.RightDockWidgetArea)

        self._setup_ui()

    def _setup_ui(self):
        if self._embed_backend is None:
            label = QLabel(
                "Qt WebEngine and Qt WebKit are unavailable.\n"
                "Plan review cannot display."
            )
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        self.bridge = PlanReviewBridge()
        self.bridge.plan_approved.connect(self._on_plan_approved)
        self.bridge.plan_rejected.connect(self._on_plan_rejected)
        self.bridge.plan_modified.connect(self._on_plan_modified)

        from classes import info
        html_path = os.path.join(info.PATH, "timeline", "directors", "plan_review.html")

        if self._embed_backend == "webengine":
            from PyQt5.QtWebEngineWidgets import QWebEngineView
            from PyQt5.QtWebChannel import QWebChannel

            self.web_view = QWebEngineView()
            self.channel = QWebChannel()
            self.channel.registerObject("planReviewBridge", self.bridge)
            self.web_view.page().setWebChannel(self.channel)

            if os.path.exists(html_path):
                self.web_view.load(QUrl.fromLocalFile(html_path))
                log.info(f"Loaded plan review UI from {html_path}")
            else:
                log.error(f"Plan review HTML not found: {html_path}")
                self.web_view.setHtml(
                    "<html><body><p>Plan review HTML not found.</p></body></html>"
                )
        else:
            from PyQt5.QtWebKitWidgets import QWebView

            self.web_view = QWebView()
            attach_webkit_window_object(self.web_view, "planReviewBridge", self.bridge)
            if os.path.exists(html_path):
                self.web_view.load(QUrl.fromLocalFile(html_path))
                log.info(f"Loaded plan review UI (WebKit) from {html_path}")
            else:
                log.error(f"Plan review HTML not found: {html_path}")
                self.web_view.setHtml(
                    "<html><body><p>Plan review HTML not found.</p></body></html>"
                )

        self.setWidget(self.web_view)
        self.setMinimumSize(400, 220)

    def show_plan(self, plan):
        if not self.bridge:
            log.error("Bridge not initialized")
            return

        try:
            self.current_plan = plan
            if hasattr(plan, "to_dict"):
                plan_data = plan.to_dict()
            elif isinstance(plan, dict):
                plan_data = plan
            else:
                plan_data = {"title": str(plan), "steps": []}
            plan_json = json.dumps(plan_data)
            self.bridge.loadPlan(plan_json)

            self.show()
            self.raise_()
            log.info(f"Showing plan: {plan_data.get('title', 'Untitled')}")
        except Exception as e:
            log.error(f"Failed to show plan: {e}", exc_info=True)

    def _on_plan_approved(self, plan_id: str):
        self.plan_approved.emit(plan_id)
        self.hide()

    def _on_plan_rejected(self, plan_id: str):
        self.plan_rejected.emit(plan_id)
        self.hide()

    def _on_plan_modified(self, plan_id: str, modifications_json: str):
        try:
            modifications = json.loads(modifications_json)
            self.plan_modified.emit(plan_id, modifications)
        except Exception as e:
            log.error(f"Failed to parse modifications: {e}", exc_info=True)


_plan_review_dock = None


def get_plan_review_dock(parent=None):
    global _plan_review_dock
    if _plan_review_dock is None:
        _plan_review_dock = PlanReviewDockWidget(parent)
    return _plan_review_dock
