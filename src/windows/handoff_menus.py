"""
 @file
 @brief File menu entries for the handoffs (Export / Import Project, Send To) and the linked-clips toolbar pill.

 ``install_handoff_menus(window)`` (called next to ``install_cloud_menu``):

 * loads the handoff packages present in this build (``classes.handoff.plugins``)
   and builds their entries from ``classes.handoff.ui_registry``: File >
   Export Project and File > Import Project get them after a separator; a
   File > Send To submenu lists the send entries, enabled only while their
   Adobe host is connected (Zenvi Link discovery is refreshed off the GUI
   thread each time the menu opens; disabled entries say how to connect);
 * creates the linked-clips pill the themes put on the main toolbar (the
   status bar is hidden by every theme): progress of handoff renders with
   Cancel, "N linked clips changed — Re-render" when a freshness check (on
   app activation and every 30 s while linked files exist, off the GUI
   thread) finds stale ones, and short results.
"""

from __future__ import annotations

import copy
import time
from typing import Dict, List, Optional

from qt_api import QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QObject, QProgressBar, QPushButton, Qt, QTimer

from classes.app import get_app
from classes.logger import log

CHECK_INTERVAL_MS = 30_000
MIN_CHECK_GAP_SECONDS = 5.0


def _tr(text: str) -> str:
    try:
        return get_app()._tr(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Re-rendering from the UI (one user intent = one undo step)
# ---------------------------------------------------------------------------

def notify(window, text: str) -> None:
    """A short result message: the toolbar pill (the themes hide the status bar)."""
    status = getattr(window, "handoff_status", None)
    if status is not None and hasattr(status, "show_message"):
        status.show_message(text)
    else:
        bar = getattr(window, "statusBar", None)
        if bar is not None and hasattr(bar, "showMessage"):
            bar.showMessage(text, 5000)


def rerender_files(window, file_ids: List[str], props: Optional[dict] = None, *, replace_props: bool = False):
    """Re-render linked files off the GUI thread; all their media swaps are ONE undo step.

    Progress shows in the toolbar pill (with Cancel). Errors are reported in a
    message box when it ends; files that failed keep their last render.
    """
    from classes.handoff import jobs, linked_media
    from classes.updates import nested_transaction
    ids = [str(f) for f in file_ids if f]
    if not ids:
        return None
    busy = [f for f in ids if jobs.job_for(f) is not None]
    if busy:
        QMessageBox.information(window, _tr("Linked Clips"), _tr("That linked clip is already rendering."))
        return None
    label = _tr("Re-rendering %d linked clips") % len(ids) if len(ids) > 1 else _tr("Re-rendering linked clip")

    def work(job):
        done, failed = [], []
        app = get_app()
        with nested_transaction(app.updates):  # this worker's tid: every swap joins one undo step
            for index, file_id in enumerate(ids):
                job.raise_if_cancelled()
                job.report(index / float(len(ids)), label)
                try:
                    linked_media.rerender_linked(file_id, props=props, replace_props=replace_props,
                                                 should_cancel=job.should_cancel)
                    done.append(file_id)
                except jobs.JobCancelled:
                    raise
                except Exception as exc:
                    log.warning("Re-render of linked file %s failed", file_id, exc_info=True)
                    failed.append((file_id, str(exc)))
        return done, failed

    def on_done(job):
        status = getattr(window, "handoff_status", None)
        if job.state == jobs.CANCELLED:
            notify(window, _tr("Re-render cancelled; nothing changed"))
        elif job.error is not None:
            QMessageBox.warning(window, _tr("Linked Clips"), _tr("Re-render failed: %s") % job.error)
        else:
            done, failed = job.result
            if failed:
                details = "\n".join("• %s" % msg for _fid, msg in failed[:6])
                QMessageBox.warning(window, _tr("Linked Clips"),
                                    _tr("%d linked clip(s) could not be re-rendered and keep their last "
                                        "render:\n%s") % (len(failed), details))
            if done:
                notify(window, _tr("Re-rendered %d linked clip(s)") % len(done))
        if status is not None:
            status.check_soon(force=True)

    return jobs.submit_job(work, label=label, kind="rerender", on_done=on_done)


# ---------------------------------------------------------------------------
# File menu entries
# ---------------------------------------------------------------------------

class HandoffMenus(QObject):
    """Export / Import Project entries and the Send To submenu, rebuilt when packages register."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._added: Dict[str, list] = {"export": [], "import": []}
        self._hosts: Dict[str, object] = {}
        self._refreshing = False
        self._last_refresh = 0.0
        self.send_menu: Optional[QMenu] = None
        self._build_send_menu()
        self.rebuild()
        from classes.handoff import ui_registry
        ui_registry.add_listener(self._registry_changed)

    def _registry_changed(self):
        # a package may register from any thread: rebuild on the GUI thread, after it returns
        from classes.qt_main_thread import invoke_on_gui
        invoke_on_gui(self.rebuild, defer=True)

    # -- Export / Import ----------------------------------------------------
    def _section(self, menu, kind: str, actions) -> None:
        if menu is None:
            return
        for item in self._added[kind]:
            menu.removeAction(item)
        self._added[kind] = []
        if not actions:
            return
        self._added[kind].append(menu.addSeparator())
        for entry in actions:
            action = menu.addAction(entry.text(_tr))
            action.setObjectName("actionHandoff_%s_%s" % (kind, entry.id))
            if entry.tooltip:
                action.setToolTip(_tr(entry.tooltip))
            action.triggered.connect(lambda checked=False, e=entry: self._run(e))
            self._added[kind].append(action)

    def rebuild(self) -> None:
        from classes.handoff import ui_registry
        self._section(getattr(self.window, "menuExport", None), "export", ui_registry.export_actions())
        self._section(getattr(self.window, "menuImport_Project", None), "import", ui_registry.import_actions())
        self._fill_send_menu()

    def _run(self, entry) -> None:
        try:
            entry.handler(self.window)
        except Exception as exc:
            log.error("Handoff action %s failed", entry.id, exc_info=True)
            QMessageBox.warning(self.window, _tr("Handoff"), _tr("%s failed: %s") % (entry.text(_tr), exc))

    # -- Send To --------------------------------------------------------------
    def _build_send_menu(self) -> None:
        menu_file = getattr(self.window, "menuFile", None)
        if menu_file is None:
            return
        menu = QMenu(_tr("Send To"), menu_file)
        menu.setObjectName("menuSendTo")
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self.refresh_hosts)
        export_menu = getattr(self.window, "menuExport", None)
        actions = list(menu_file.actions())
        anchor = None
        if export_menu is not None and export_menu.menuAction() in actions:
            index = actions.index(export_menu.menuAction())
            anchor = actions[index + 1] if index + 1 < len(actions) else None
        if anchor is not None:
            menu_file.insertMenu(anchor, menu)
        else:
            menu_file.addMenu(menu)
        self.send_menu = menu
        self.window.menuSendTo = menu

    def _fill_send_menu(self) -> None:
        from classes.handoff import adobe_link, ui_registry
        menu = self.send_menu
        if menu is None:
            return
        menu.clear()
        entries = ui_registry.send_actions()
        menu.menuAction().setVisible(bool(entries))
        for entry in entries:
            action = menu.addAction(entry.text(_tr))
            action.setObjectName("actionSendTo_%s" % entry.id)
            action.setData(entry.host_app)
            action.triggered.connect(lambda checked=False, e=entry: self._run(e))
            host = self._hosts.get(entry.host_app) if entry.host_app else None
            connected = entry.host_app is None or bool(getattr(host, "connected", False))
            action.setEnabled(connected)
            if not connected:
                reason = getattr(host, "reason", "") if host is not None else _tr("Checking…")
                action.setToolTip("%s\n%s" % (reason, adobe_link.connect_hint(entry.host_app)))
            elif entry.tooltip:
                action.setToolTip(_tr(entry.tooltip))

    def refresh_hosts(self, force: bool = False) -> None:
        """Re-read Zenvi Link discovery off the GUI thread, then update the Send To entries."""
        from classes.handoff import adobe_link, jobs
        if self._refreshing or (not force and time.monotonic() - self._last_refresh < 1.0):
            return
        self._refreshing = True

        def done(job):
            self._refreshing = False
            self._last_refresh = time.monotonic()
            if job.error is None and job.result is not None:
                self._hosts = {h.app: h for h in job.result}
            self._fill_send_menu()

        jobs.submit_job(lambda job: adobe_link.list_hosts(), label="Zenvi Link discovery", quick=True, on_done=done)

    def hosts(self) -> Dict[str, object]:
        return dict(self._hosts)


# ---------------------------------------------------------------------------
# Status bar: render progress + stale linked clips
# ---------------------------------------------------------------------------

PILL_STYLE = (
    "QFrame#handoffStatus { background-color: rgba(77,156,246,0.14); border: 1px solid rgba(77,156,246,0.45); "
    "border-radius: 8px; } "
    "QFrame#handoffStatus QLabel { color: #d9e6ff; background: transparent; border: none; } "
    "QFrame#handoffStatus QPushButton { color: #4d9cf6; background: transparent; border: none; padding: 0 4px; "
    "font-weight: 600; } "
    "QFrame#handoffStatus QPushButton:hover { color: #7fb8ff; } "
    "QFrame#handoffStatus QProgressBar { background: rgba(255,255,255,0.12); border: none; border-radius: 2px; } "
    "QFrame#handoffStatus QProgressBar::chunk { background: #4d9cf6; border-radius: 2px; }"
)
MESSAGE_MS = 6000


class LinkedClipsStatus(QFrame):
    """Main-toolbar pill: running handoff renders (with Cancel), stale linked clips (with Re-render),
    and short results ("Re-rendered 1 linked clip").

    The themes place it on the main toolbar next to the update pill (the
    status bar is hidden by every theme); ``is_active`` says whether it has
    anything to show, and it keeps its toolbar slot's visibility in step.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setObjectName("handoffStatus")
        self.setStyleSheet(PILL_STYLE)
        self.last_checks: Dict[str, object] = {}
        self._stale: List[str] = []
        self._checking = False
        self._last_check = 0.0
        self._message = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 6, 3)
        layout.setSpacing(6)
        self.label = QLabel("")
        self.label.setObjectName("handoffStatusLabel")
        self.progress = QProgressBar()
        self.progress.setFixedSize(70, 5)
        self.progress.setTextVisible(False)
        self.cancel_button = QPushButton(_tr("Cancel"))
        self.cancel_button.setObjectName("handoffCancel")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self._cancel_jobs)
        self.rerender_button = QPushButton(_tr("Re-render"))
        self.rerender_button.setObjectName("handoffRerender")
        self.rerender_button.setCursor(Qt.PointingHandCursor)
        self.rerender_button.clicked.connect(self._rerender_stale)
        for w in (self.label, self.progress, self.cancel_button, self.rerender_button):
            layout.addWidget(w)
        self.message_timer = QTimer(self)
        self.message_timer.setSingleShot(True)
        self.message_timer.timeout.connect(self._clear_message)
        self.setVisible(False)

        from classes.handoff import jobs
        jobs.add_listener(self._on_job)
        self.timer = QTimer(self)
        self.timer.setInterval(CHECK_INTERVAL_MS)
        self.timer.timeout.connect(lambda: self.check_soon())
        self.timer.start()
        try:
            get_app().applicationStateChanged.connect(self._app_state_changed)
        except Exception:
            log.debug("applicationStateChanged unavailable", exc_info=True)

    @property
    def is_active(self) -> bool:
        """True while there is something to show (themes read it when they rebuild the toolbar)."""
        from classes.handoff import jobs
        return bool(jobs.running_jobs() or self._stale or self._message)

    def show_message(self, text: str, ms: int = MESSAGE_MS) -> None:
        """A short result line in the pill (the status bar is hidden by the themes)."""
        self._message = str(text)
        self.message_timer.start(int(ms))
        self._refresh_view()

    def _clear_message(self) -> None:
        self._message = ""
        self._refresh_view()

    def _sync_visibility(self, visible: bool) -> None:
        self.setVisible(visible)
        toolbar = getattr(self.window, "toolBar", None)
        if toolbar is None or not hasattr(toolbar, "actions"):
            return
        for action in toolbar.actions():
            try:
                if toolbar.widgetForAction(action) is self:
                    action.setVisible(visible)
            except Exception:
                log.debug("toolbar slot lookup failed", exc_info=True)

    # -- jobs -----------------------------------------------------------------
    def _on_job(self, _job) -> None:
        self._refresh_view()

    def _cancel_jobs(self) -> None:
        from classes.handoff import jobs
        for job in jobs.running_jobs():
            job.cancel()

    # -- freshness ------------------------------------------------------------
    def _app_state_changed(self, state) -> None:
        if state == Qt.ApplicationActive:
            self.check_soon()

    def _linked_files(self) -> List[dict]:
        from classes.handoff.linked_media import LINK_KEY
        from classes.query import File
        return [copy.deepcopy(f.data) for f in File.filter() if isinstance(f.data.get(LINK_KEY), dict)]

    def check_soon(self, force: bool = False) -> None:
        """Recompute linked files' freshness off the GUI thread (skipped while one check runs)."""
        from classes.handoff import jobs, linked_media
        if self._checking or (not force and time.monotonic() - self._last_check < MIN_CHECK_GAP_SECONDS):
            return
        try:
            files = self._linked_files()
        except Exception:
            log.debug("linked file scan skipped", exc_info=True)
            return
        if not files:
            self.last_checks = {}
            self._stale = []
            self._refresh_view()
            return
        self._checking = True

        def work(job):
            return [linked_media.check_link(data) for data in files]

        def done(job):
            self._checking = False
            self._last_check = time.monotonic()
            if job.error is not None:
                log.warning("Linked clip freshness check failed: %s", job.error)
                return
            checks = [c for c in (job.result or []) if c is not None]
            self.last_checks = {c.file_id: c for c in checks}
            self._stale = [c.file_id for c in checks if c.state == "stale"]
            self._refresh_view()

        jobs.submit_job(work, label="Linked clip freshness", quick=True, on_done=done)

    def _rerender_stale(self) -> None:
        if self._stale:
            rerender_files(self.window, list(self._stale))

    # -- view -----------------------------------------------------------------
    def _refresh_view(self) -> None:
        from classes.handoff import jobs
        running = jobs.running_jobs()
        self.progress.setVisible(bool(running))
        self.cancel_button.setVisible(bool(running))
        self.rerender_button.setVisible(not running and bool(self._stale))
        if running:
            job = running[-1]
            self.label.setText(job.message or job.label)
            if job.progress is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(job.progress * 1000))
        elif self._stale:
            n = len(self._stale)
            self.label.setText(_tr("%d linked clip changed") % n if n == 1 else _tr("%d linked clips changed") % n)
        else:
            self.label.setText(self._message)
        self._sync_visibility(self.is_active)

    def stale_file_ids(self) -> List[str]:
        return list(self._stale)


def install_handoff_menus(window):
    """Load the handoff packages, add their File menu entries and the linked-clips status widget."""
    from classes.handoff import plugins
    plugins.load_plugins()
    menus = HandoffMenus(window)
    window.handoff_menus = menus
    window.handoff_status = LinkedClipsStatus(window)
    QTimer.singleShot(2000, lambda: window.handoff_status.check_soon(force=True))
    return menus
