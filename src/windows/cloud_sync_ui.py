"""
 @file
 @brief File → Zenvi Cloud: push the project, open it in the web editor, open a cloud project.

 Qt glue for classes/cloud_sync.py. Hashing, uploads, downloads and every
 network call run in a QThread worker. The GUI thread only snapshots small
 state (the project file path, file ids and their paths), shows progress,
 asks questions, and applies results through the update system with
 ``update_untracked``, so a push or pull never adds an undo step.
"""

import os
import threading
import webbrowser
from datetime import datetime

from qt_api import (
    QApplication,
    QCoreApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QIcon,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QObject,
    QPixmap,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QThread,
    QTimer,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    Qt,
    pyqtSignal,
    pyqtSlot,
)

from classes import cloud_sync, info
from classes.app import get_app
from classes.logger import log
from classes.qt_main_thread import invoke_on_gui


class AuthManagerTokens:
    """Supabase session of the desktop sign-in (classes/auth_manager.py).

    Both calls may hit the network (a refresh), so they only run on the worker.
    """

    def access_token(self):
        from classes.auth_manager import AuthManager
        return AuthManager.instance().get_access_token()

    def refresh(self):
        from classes.auth_manager import AuthManager
        return AuthManager.instance().refresh_access_token()


# ── Text helpers (pure; tested headlessly) ────────────────────────────────────

def describe_progress(progress, _=lambda s: s):
    """(label, fraction or None for a busy indicator) for a cloud_sync.Progress."""
    phase = progress.phase
    fraction = (min(1.0, progress.done / progress.total) if progress.total > 0 else None)
    amounts = {
        "name": progress.detail,
        "done": cloud_sync.format_bytes(progress.done),
        "total": cloud_sync.format_bytes(progress.total),
    }
    if phase == "prepare":
        return _("Preparing “%s”…") % progress.detail, None
    if phase == "hash":
        return _("Checking media: %(name)s (%(done)s of %(total)s)") % amounts, fraction
    if phase == "check":
        return _("Asking Zenvi Cloud which media it already has…"), None
    if phase == "upload":
        return _("Uploading %(name)s — %(done)s of %(total)s") % amounts, fraction
    if phase == "project":
        return _("Saving the project to Zenvi Cloud…"), None
    if phase == "fetch":
        return _("Getting the project from Zenvi Cloud…"), None
    if phase == "verify":
        return _("Checking %(name)s on this computer…") % amounts, fraction
    if phase == "download":
        return _("Downloading %(name)s — %(done)s of %(total)s") % amounts, fraction
    if phase == "write":
        return _("Writing the project file…"), None
    return _("Working…"), None


def push_summary(result, _=lambda s: s):
    """One or two sentences about what a push did."""
    parts = []
    if result.uploaded_files:
        parts.append(_("Uploaded %(count)d media file(s) (%(size)s).") % {
            "count": result.uploaded_files, "size": cloud_sync.format_bytes(result.uploaded_bytes)})
        if result.already_in_cloud:
            parts.append(_("%d were already in Zenvi Cloud.") % result.already_in_cloud)
    elif result.already_in_cloud:
        parts.append(_("Nothing new to upload: the media was already in Zenvi Cloud."))
    if result.created:
        parts.append(_("Created a new cloud project."))
    elif result.overwritten:
        parts.append(_("The cloud copy was replaced with this version."))
    else:
        parts.append(_("Updated the cloud copy."))
    return " ".join(parts)


def format_duration(seconds):
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_updated(value):
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def open_url(url):
    try:
        webbrowser.open(url, new=1)
        return True
    except Exception:
        log.warning("Could not open %s in the browser", url, exc_info=True)
        return False


def _same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ── Worker ────────────────────────────────────────────────────────────────────

class _CloudJob(QObject):
    """Runs one cloud_sync call on its QThread and reports back by signal."""

    progress = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, work):
        QObject.__init__(self)
        self._work = work

    @pyqtSlot()
    def run(self):
        try:
            result = self._work(cloud_sync.ThrottledProgress(self.progress.emit))
        except Exception as exc:
            # Not swallowed: the controller explains it to the person on the GUI thread.
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)


class CloudSyncController(QObject):
    """Owns File → Zenvi Cloud. Lives on the GUI thread and runs one job at a time.

    ``start_job`` and ``tokens`` exist for tests: ``start_job(work)`` receives
    the function the worker would run, ``tokens`` replaces the desktop sign-in.
    """

    def __init__(self, window, start_job=None, tokens=None):
        QObject.__init__(self)
        self._window = window
        self._start_job = start_job
        self._tokens = tokens
        self._actions = []
        self._threads = []
        self._cancel = None
        self._progress = None
        self._on_success = None
        self._on_failure = None

    # -- public actions (GUI thread)

    @property
    def busy(self):
        return self._on_success is not None

    def set_actions(self, actions):
        self._actions = list(actions)

    def push(self, open_web=False):
        """Push to Zenvi Cloud (and open the web editor afterwards when ``open_web``)."""
        _ = get_app()._tr
        if self._refuse_while_busy():
            return
        request = self._push_request()
        if request is None:
            return
        cloud_url, website = self._urls()
        tokens = self._token_source()
        cancel = cloud_sync.CancelToken()
        user_agent = self._user_agent()

        def work(progress):
            client = cloud_sync.CloudClient(cloud_url, tokens, cancel=cancel, user_agent=user_agent)
            return cloud_sync.push_project(
                client, request, website=website, progress=progress,
                confirm_overwrite=lambda conflict: self._confirm_overwrite(conflict, cancel))

        self._run(
            work,
            title=_("Open in Web Editor") if open_web else _("Push to Zenvi Cloud"),
            cancel=cancel,
            on_success=lambda result: self._push_finished(result, request, open_web),
            on_failure=lambda error: self._show_failure(error, retry=lambda: self.push(open_web)),
        )

    def open_in_web(self):
        """Push (only what changed), then open the project in the web editor."""
        self.push(open_web=True)

    def open_from_cloud(self):
        """Pick a cloud project, download it into ~/Zenvi/Cloud and open it."""
        app = get_app()
        _ = app._tr
        if self._refuse_while_busy():
            return
        project = app.project
        discard_version = None
        if project.needs_save():
            # Same question as File → Open, asked before the download starts.
            ret = QMessageBox.question(
                self._window,
                _("Unsaved Changes"),
                _("Save changes to project first?"),
                QMessageBox.Cancel | QMessageBox.No | QMessageBox.Yes)
            if ret == QMessageBox.Yes:
                self._window.actionSave_trigger()
                if project.needs_save():
                    return
            elif ret == QMessageBox.No:
                discard_version = app.updates.data_version
            else:
                return

        dialog = CloudProjectsDialog(self._window)
        dialog.refresh_requested.connect(lambda: self._list_projects(dialog))
        self._list_projects(dialog)
        accepted = dialog.exec_() == QDialog.Accepted
        chosen = dialog.selected_project() if accepted else None
        if not accepted and self.busy and self._cancel is not None:
            self._cancel.cancel()
        if chosen:
            self._pull(chosen, discard_version)

    def shutdown(self):
        """App is quitting: cancel the running job and give it a moment to stop."""
        if self._cancel is not None:
            self._cancel.cancel()
        for thread, _job in list(self._threads):
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(5000)
            except RuntimeError:
                continue

    # -- snapshot and results (GUI thread)

    def _push_request(self):
        """Save first when needed, then capture what the worker needs (no I/O here)."""
        app = get_app()
        _ = app._tr
        project = app.project
        if not project.current_filepath:
            ret = QMessageBox.question(
                self._window,
                _("Push to Zenvi Cloud"),
                _("Save this project first. The cloud copy stays linked to the project file, "
                  "so pushing again updates it."),
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Save)
            if ret != QMessageBox.Save:
                return None
            self._window.actionSave_trigger()
        elif project.needs_save():
            # Push what is on screen: the same save as File → Save.
            self._window.actionSave_trigger()
        if not project.current_filepath or project.needs_save():
            # Save cancelled or failed; save_project already said why.
            return None
        local_paths = {}
        for entry in project._data.get("files") or []:
            if isinstance(entry, dict) and entry.get("id"):
                local_paths[str(entry["id"])] = str(entry.get("path") or "")
        return cloud_sync.PushRequest(project_file=project.current_filepath, local_paths=local_paths)

    def _push_finished(self, result, request, open_web):
        _ = get_app()._tr
        self._store_link(result, request)
        if open_web:
            if open_url(result.editor_url):
                self._status(_("Opened “%s” in the web editor.") % result.name)
            else:
                QMessageBox.information(self._window, _("Open in Web Editor"), result.editor_url)
            if result.warnings:
                self._show_warnings(_("Pushed to Zenvi Cloud"), result.warnings)
            return
        PushResultDialog(self._window, result).exec_()

    def _store_link(self, result, request):
        """Remember the cloud project and media hashes in the project (not undoable)."""
        app = get_app()
        project = app.project
        if not _same_path(project.current_filepath, request.project_file):
            log.warning("Zenvi Cloud: a different project is open now; not storing the cloud link")
            return
        updates = app.updates
        for file_id, ref in result.cloud_refs.items():
            updates.update_untracked(["files", {"id": file_id}], {"cloud": ref})
        updates.update_untracked(["zenvi_cloud"], dict(result.link))
        # Persist the link like autosave does (background save), so pushing
        # again updates this cloud project instead of creating another one.
        save = getattr(self._window, "save_project", None)
        if callable(save):
            threading.Thread(target=save, args=(project.current_filepath,), daemon=True,
                             name="zenvi-cloud-save-link").start()

    def _list_projects(self, dialog):
        _ = get_app()._tr
        if self.busy:
            return
        cloud_url, _website = self._urls()
        tokens = self._token_source()
        cancel = cloud_sync.CancelToken()
        user_agent = self._user_agent()

        def work(_progress):
            client = cloud_sync.CloudClient(cloud_url, tokens, cancel=cancel, user_agent=user_agent)
            projects = client.list_projects()
            thumbnails = cloud_sync.fetch_thumbnails(client.session, projects, cancel=cancel)
            return {"projects": projects, "thumbnails": thumbnails}

        def failed(error):
            if isinstance(error, cloud_sync.CloudCancelled):
                return
            if isinstance(error, cloud_sync.CloudAuthError):
                dialog.reject()
                self._show_failure(error, retry=self.open_from_cloud)
                return
            dialog.set_error(self._message_for(error))

        dialog.set_loading()
        self._run(work, title=_("Open from Zenvi Cloud"), cancel=cancel, show_progress=False,
                  on_success=lambda data: dialog.set_projects(data["projects"], data["thumbnails"]),
                  on_failure=failed)

    def _pull(self, chosen, discard_version):
        _ = get_app()._tr
        if self._refuse_while_busy():
            return
        project_id = str(chosen.get("id") or "")
        cloud_url, _website = self._urls()
        tokens = self._token_source()
        cancel = cloud_sync.CancelToken()
        user_agent = self._user_agent()
        dest_root = cloud_sync.default_pull_root()
        # An older local copy is zipped into File → Recovery before it is replaced.
        backup = getattr(self._window, "save_recovery", None)

        def work(progress):
            client = cloud_sync.CloudClient(cloud_url, tokens, cancel=cancel, user_agent=user_agent)
            return cloud_sync.pull_project(client, project_id, dest_root=dest_root, progress=progress,
                                           backup=backup if callable(backup) else None)

        self._run(
            work,
            title=_("Open from Zenvi Cloud"),
            cancel=cancel,
            on_success=lambda result: self._pull_finished(result, discard_version),
            on_failure=lambda error: self._show_failure(error, retry=lambda: self._pull(chosen, discard_version)),
        )

    def _pull_finished(self, result, discard_version):
        app = get_app()
        _ = app._tr
        project = app.project
        if (discard_version is not None and project.needs_save()
                and app.updates.data_version == discard_version):
            # "No" was already chosen for exactly these changes; don't ask twice.
            project.has_unsaved_changes = False
        self._window.open_project(result.project_file)
        if not _same_path(project.current_filepath, result.project_file):
            QMessageBox.information(
                self._window, _("Open from Zenvi Cloud"),
                _("The project was downloaded to %s. Open it any time with File → Open.") % result.project_file)
            return
        notes = list(result.warnings)
        if result.replaced_existing:
            notes.append(_("Your previous local copy of this project is under File → Recovery."))
        if notes:
            self._show_warnings(_("Opened from Zenvi Cloud"), notes)
        self._status(_("Opened “%(name)s” from Zenvi Cloud (%(count)d file(s) downloaded).") % {
            "name": result.name, "count": result.downloaded_files})

    # -- questions from the worker

    def _confirm_overwrite(self, conflict, cancel):
        """Worker thread: ask on the GUI thread whether to overwrite the cloud copy."""
        answer = {}
        done = threading.Event()

        def ask():
            try:
                answer["overwrite"] = self._ask_overwrite(conflict)
            except Exception:
                log.error("Zenvi Cloud: could not ask about the conflict", exc_info=True)
                answer["overwrite"] = False
            finally:
                done.set()

        invoke_on_gui(ask, context=self)
        while not done.wait(0.25):
            cancel.check()
        return bool(answer.get("overwrite"))

    def _ask_overwrite(self, conflict):
        _ = get_app()._tr
        box = QMessageBox(self._window)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(_("Changed in Zenvi Cloud"))
        box.setText(_("This project was changed in Zenvi Cloud after it was last pushed from here "
                      "(for example in the web editor)."))
        box.setInformativeText(_("Replace the cloud copy with the version on this computer? The changes "
                                 "made in the cloud will be lost. To keep them, cancel and use "
                                 "File → Zenvi Cloud → Open from Zenvi Cloud."))
        # Standard buttons with our own text: their enums exist in every Qt binding qt_api supports.
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        overwrite = box.button(QMessageBox.Save)
        overwrite.setText(_("Replace Cloud Copy"))
        box.exec_()
        return box.clickedButton() is overwrite

    # -- errors

    def _message_for(self, error):
        _ = get_app()._tr
        if isinstance(error, cloud_sync.CloudQuotaError):
            text = error.message
            if error.used_bytes is not None and error.quota_bytes is not None:
                text += " " + _("%(used)s of %(quota)s is in use.") % {
                    "used": cloud_sync.format_bytes(error.used_bytes),
                    "quota": cloud_sync.format_bytes(error.quota_bytes)}
            return text + " " + _("Delete cloud projects you no longer need, then try again.")
        if isinstance(error, cloud_sync.CloudError):
            return error.message
        return _("Something went wrong: %s") % error

    def _show_failure(self, error, retry=None):
        _ = get_app()._tr
        if isinstance(error, cloud_sync.CloudCancelled):
            if error.code == "conflict_cancelled":
                self._status(_("Push cancelled; the cloud copy was left as it is."))
            else:
                self._status(_("Cancelled."))
            return
        if isinstance(error, cloud_sync.CloudAuthError):
            self._offer_sign_in(error.message, retry)
            return
        if not isinstance(error, cloud_sync.CloudError):
            log.error("Zenvi Cloud failed", exc_info=(type(error), error, error.__traceback__))
        QMessageBox.warning(self._window, _("Zenvi Cloud"), self._message_for(error))

    def _offer_sign_in(self, message, retry):
        _ = get_app()._tr
        box = QMessageBox(self._window)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(_("Sign In to Zenvi"))
        box.setText(_("Sign in to your Zenvi account to use Zenvi Cloud."))
        box.setInformativeText(message)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        sign_in = box.button(QMessageBox.Ok)
        sign_in.setText(_("Sign In…"))
        box.exec_()
        if box.clickedButton() is not sign_in:
            return
        from windows.login_window import LoginWindow
        login = LoginWindow(parent=self._window)
        if login.exec_() == LoginWindow.Accepted and retry is not None:
            QTimer.singleShot(0, retry)

    def _show_warnings(self, title, notes):
        _ = get_app()._tr
        box = QMessageBox(self._window)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(title)
        box.setText(_("Some media needs your attention:"))
        box.setDetailedText("\n".join(notes))
        box.setInformativeText("\n".join(notes[:5]) + ("\n…" if len(notes) > 5 else ""))
        box.exec_()

    # -- job plumbing

    def _refuse_while_busy(self):
        if not self.busy:
            return False
        self._status(get_app()._tr("Zenvi Cloud is still working; try again in a moment."))
        return True

    def _run(self, work, *, title, cancel, on_success, on_failure, show_progress=True):
        self._cancel = cancel
        self._on_success = on_success
        self._on_failure = on_failure
        self._set_actions_enabled(False)
        self._progress = self._open_progress(title) if show_progress else None
        if self._start_job is not None:
            self._start_job(work)
        else:
            self._start_thread(work)

    def _start_thread(self, work):
        self._threads = [(t, j) for (t, j) in self._threads if not t.isFinished()]
        thread = QThread()
        job = _CloudJob(work)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.progress.connect(self._job_progress)
        job.succeeded.connect(self._job_succeeded)
        job.failed.connect(self._job_failed)
        job.succeeded.connect(thread.quit)
        job.failed.connect(thread.quit)
        self._threads.append((thread, job))
        thread.start()

    @pyqtSlot(object)
    def _job_progress(self, progress):
        dialog = self._progress
        if dialog is None:
            return
        label, fraction = describe_progress(progress, get_app()._tr)
        dialog.setLabelText(label)
        if fraction is None:
            dialog.setRange(0, 0)
        else:
            dialog.setRange(0, 1000)
            dialog.setValue(int(fraction * 1000))

    @pyqtSlot(object)
    def _job_succeeded(self, result):
        callback = self._on_success
        self._finish()
        if callback is not None:
            callback(result)

    @pyqtSlot(object)
    def _job_failed(self, error):
        callback = self._on_failure
        self._finish()
        if callback is not None:
            callback(error)

    def _finish(self):
        dialog = self._progress
        self._progress = None
        self._on_success = None
        self._on_failure = None
        self._cancel = None
        self._set_actions_enabled(True)
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def _cancel_clicked(self):
        if self._cancel is not None:
            self._cancel.cancel()
            self._status(get_app()._tr("Cancelling…"))

    def _open_progress(self, title):
        _ = get_app()._tr
        dialog = QProgressDialog(_("Starting…"), _("Cancel"), 0, 0, self._window)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(480)
        dialog.canceled.connect(self._cancel_clicked)
        dialog.show()
        return dialog

    def _set_actions_enabled(self, enabled):
        for action in self._actions:
            action.setEnabled(enabled)

    # -- environment

    def _urls(self):
        settings = get_app().get_settings()
        setting = settings.get("zenvi-cloud-url") if settings is not None else ""
        return cloud_sync.resolve_cloud_url(setting if isinstance(setting, str) else ""), cloud_sync.resolve_website()

    def _token_source(self):
        return self._tokens if self._tokens is not None else AuthManagerTokens()

    def _user_agent(self):
        return f"Zenvi-Desktop/{getattr(info, 'VERSION', '')}".rstrip("/")

    def _status(self, text):
        bar = getattr(self._window, "statusBar", None)
        try:
            if bar is not None and not callable(getattr(bar, "showMessage", None)):
                bar = bar()
            if bar is not None:
                bar.showMessage(text, 8000)
        except Exception:
            log.debug("Could not show a status message", exc_info=True)


# ── Dialogs ───────────────────────────────────────────────────────────────────

class PushResultDialog(QDialog):
    """Where the project is now: its web editor link, with Copy and Open."""

    def __init__(self, parent, result):
        QDialog.__init__(self, parent)
        _ = get_app()._tr
        self._url = result.editor_url
        self.setWindowTitle(_("Pushed to Zenvi Cloud"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        headline = QLabel(_("“%s” is in Zenvi Cloud.") % result.name)
        headline.setStyleSheet("font-weight: 600;")
        layout.addWidget(headline)
        summary = QLabel(push_summary(result, _))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        link = QLineEdit(self._url)
        link.setReadOnly(True)
        link.setCursorPosition(0)
        layout.addWidget(link)
        if result.warnings:
            layout.addWidget(QLabel(_("Some media was left out or needs attention:")))
            notes = QPlainTextEdit("\n".join(result.warnings))
            notes.setReadOnly(True)
            notes.setMaximumHeight(120)
            layout.addWidget(notes)
        row = QHBoxLayout()
        self._copy_button = QPushButton(_("Copy Link"))
        self._copy_button.clicked.connect(self._copy_link)
        open_button = QPushButton(_("Open in Web Editor"))
        open_button.clicked.connect(self._open)
        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.accept)
        close_button.setDefault(True)
        row.addWidget(self._copy_button)
        row.addWidget(open_button)
        row.addStretch(1)
        row.addWidget(close_button)
        layout.addLayout(row)

    def _copy_link(self):
        QApplication.clipboard().setText(self._url)
        self._copy_button.setText(get_app()._tr("Copied"))

    def _open(self):
        open_url(self._url)
        self.accept()


class CloudProjectsDialog(QDialog):
    """Pick a cloud project: name, last change, duration and size."""

    refresh_requested = pyqtSignal()

    def __init__(self, parent):
        QDialog.__init__(self, parent)
        _ = get_app()._tr
        self._projects = []
        self.setWindowTitle(_("Open from Zenvi Cloud"))
        self.resize(680, 440)
        layout = QVBoxLayout(self)
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([_("Name"), _("Updated"), _("Duration"), _("Size")])
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.itemSelectionChanged.connect(self._selection_changed)
        self._tree.itemDoubleClicked.connect(lambda _item, _column: self._accept_if_selected())
        layout.addWidget(self._tree, 1)
        row = QHBoxLayout()
        self._refresh_button = QPushButton(_("Refresh"))
        self._refresh_button.clicked.connect(self.refresh_requested.emit)
        cancel_button = QPushButton(_("Cancel"))
        cancel_button.clicked.connect(self.reject)
        self._open_button = QPushButton(_("Open"))
        self._open_button.setDefault(True)
        self._open_button.setEnabled(False)
        self._open_button.clicked.connect(self._accept_if_selected)
        row.addWidget(self._refresh_button)
        row.addStretch(1)
        row.addWidget(cancel_button)
        row.addWidget(self._open_button)
        layout.addLayout(row)

    def set_loading(self):
        self._status_label.setText(get_app()._tr("Loading your Zenvi Cloud projects…"))
        self._refresh_button.setEnabled(False)
        self._tree.clear()
        self._open_button.setEnabled(False)

    def set_error(self, message):
        self._status_label.setText(message)
        self._refresh_button.setEnabled(True)

    def set_projects(self, projects, thumbnails=None):
        _ = get_app()._tr
        self._projects = list(projects)
        self._refresh_button.setEnabled(True)
        self._tree.clear()
        thumbnails = thumbnails or {}
        for project in self._projects:
            stats = project.get("stats") if isinstance(project.get("stats"), dict) else {}
            width, height = stats.get("width"), stats.get("height")
            size = f"{width}×{height}" if width and height else ""
            item = QTreeWidgetItem([
                str(project.get("name") or _("Untitled project")),
                format_updated(project.get("updatedAt")),
                format_duration(stats.get("duration")),
                size,
            ])
            item.setData(0, Qt.UserRole, project.get("id"))
            image = thumbnails.get(project.get("id"))
            if image:
                pixmap = QPixmap()
                if pixmap.loadFromData(image):
                    item.setIcon(0, QIcon(pixmap))
            self._tree.addTopLevelItem(item)
        if self._projects:
            self._status_label.setText(_("Choose a project to download and open. It is saved in %s.")
                                       % cloud_sync.default_pull_root())
            self._tree.setCurrentItem(self._tree.topLevelItem(0))
        else:
            self._status_label.setText(_("You have no projects in Zenvi Cloud yet. Use File → Zenvi Cloud → "
                                         "Push to Zenvi Cloud to add this one."))

    def selected_project(self):
        item = self._tree.currentItem()
        if item is None:
            return None
        project_id = item.data(0, Qt.UserRole)
        return next((p for p in self._projects if p.get("id") == project_id), None)

    def _selection_changed(self):
        self._open_button.setEnabled(self._tree.currentItem() is not None)

    def _accept_if_selected(self):
        if self.selected_project() is not None:
            self.accept()


# ── Menu ──────────────────────────────────────────────────────────────────────

def install_cloud_menu(window):
    """Add File → Zenvi Cloud under Save As… and return its controller."""
    menu_file = getattr(window, "menuFile", None)
    if menu_file is None:
        return None
    _ = get_app()._tr
    controller = CloudSyncController(window)
    menu = QMenu(_("Zenvi Cloud"), menu_file)
    menu.setObjectName("menuZenviCloud")
    push_action = menu.addAction(_("Push to Zenvi Cloud"))
    push_action.setObjectName("actionCloudPush")
    push_action.setToolTip(_("Save the project and upload it, with any media Zenvi Cloud doesn't have yet"))
    push_action.triggered.connect(lambda checked=False: controller.push())
    web_action = menu.addAction(_("Open in Web Editor"))
    web_action.setObjectName("actionCloudOpenWeb")
    web_action.triggered.connect(lambda checked=False: controller.open_in_web())
    menu.addSeparator()
    open_action = menu.addAction(_("Open from Zenvi Cloud..."))
    open_action.setObjectName("actionCloudOpen")
    open_action.triggered.connect(lambda checked=False: controller.open_from_cloud())
    controller.set_actions([push_action, web_action, open_action])

    anchor = None
    save_as = getattr(window, "actionSaveAs", None)
    actions = list(menu_file.actions())
    if save_as in actions:
        index = actions.index(save_as)
        anchor = actions[index + 1] if index + 1 < len(actions) else None
    if anchor is not None:
        menu_file.insertMenu(anchor, menu)
    else:
        menu_file.addMenu(menu)
    window.menuZenviCloud = menu

    app = QCoreApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(controller.shutdown)
    return controller
