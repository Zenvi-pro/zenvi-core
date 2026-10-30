"""
Zenvi login window.

Opens ZENVI_WEBSITE/login?state=<uuid> in the default browser, then polls
Supabase until the session is ready (POLL_TIMEOUT, the hand-off row's
lifetime). A browser that is already signed in to Zenvi only has to approve.

Usage:
    dlg = LoginWindow(parent=main_window)
    dlg.auth_completed.connect(on_signed_in)
    dlg.exec_()

    # A sign-in that ended on its own: explain, and open the browser only on a click.
    dlg = LoginWindow(parent=None, auto_open_browser=False,
                      message="Your Zenvi sign-in has ended. Sign in again to continue.")
"""

import logging
import sys

from qt_api import Qt, QTimer, pyqtSignal, pyqtSlot, isdeleted
from qt_api import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from classes.auth_manager import AuthManager, confirmation_code
from classes import info
from themes.cosmic.tokens import resolve
from classes.qt_main_thread import invoke_on_gui

log = logging.getLogger(__name__)

_DIALOG_QSS = resolve("""
QPushButton#ghost {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: {text_muted};
    font-size: 12px;
    padding: 7px 0;
}
QPushButton#ghost:hover {
    background: {hover};
    color: {text};
}
""")

# ── Login dialog ────────────────────────────────────────────────────────────


class LoginWindow(QDialog):
    auth_completed = pyqtSignal(dict)
    auth_cancelled = pyqtSignal()

    def __init__(self, parent=None, auto_open_browser: bool = True, message: str = "") -> None:
        super().__init__(parent)
        self._auth = AuthManager.instance()
        self._message = message
        # Cancel handle of the browser hand-off this dialog is waiting for.
        self._browser_poll = None

        self.setWindowTitle("Sign in to Zenvi")
        self.setFixedWidth(400)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        info.apply_application_icon(self)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(_DIALOG_QSS)

        self._build_ui()

        if auto_open_browser:
            QTimer.singleShot(0, self._start_browser_flow)
        else:
            self._show_browser_idle(message or "Sign in to continue")

    def showEvent(self, event):
        super().showEvent(event)
        info.schedule_application_icon(self)

    # ── UI ─────────────────────────────────────────────────────────────────

    def _brand_header(self) -> QWidget:
        """Zenvi logo + wordmark for login pages."""
        header = QWidget()
        layout = QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pixmap = info.application_logo_pixmap(80)
        if pixmap is not None and not pixmap.isNull():
            logo.setPixmap(pixmap)
            layout.addWidget(logo)

        wordmark = QLabel("Zenvi")
        wordmark.setAlignment(Qt.AlignCenter)
        wordmark.setStyleSheet(resolve("font-size: 22px; font-weight: 600;"))
        layout.addWidget(wordmark)
        return header

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_browser_page())

    def _build_browser_page(self) -> QFrame:
        page = QFrame()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 36)
        layout.setSpacing(0)

        layout.addWidget(self._brand_header())
        layout.addSpacing(4)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(resolve("margin: 14px 0 18px 0;"))
        layout.addWidget(sep)

        self._browser_status = QLabel("Opening browser…")
        self._browser_status.setAlignment(Qt.AlignCenter)
        self._browser_status.setStyleSheet("font-size: 14px; font-weight: 500; margin-bottom: 8px;")
        layout.addWidget(self._browser_status)

        self._browser_sub = QLabel(
            "Open the Authorize page in your browser.\n"
            "If you're already signed in, just click Continue.\n"
            "This window will close automatically."
        )
        self._browser_sub.setAlignment(Qt.AlignCenter)
        self._browser_sub.setWordWrap(True)
        self._browser_sub.setStyleSheet(resolve("font-size: 12px; color: {text_muted};"))
        layout.addWidget(self._browser_sub)

        layout.addSpacing(14)

        # URL copy row
        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        self._url_label = QLineEdit()
        self._url_label.setReadOnly(True)
        self._url_label.setPlaceholderText("Login URL…")
        self._url_label.setStyleSheet(resolve(
            "background: {sunken}; border: 1px solid {line_strong}; border-radius: 6px;"
            "color: {text_muted}; font-size: 11px; padding: 5px 8px;"
        ))
        url_row.addWidget(self._url_label)
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setFixedWidth(52)
        self._copy_btn.clicked.connect(self._copy_url)
        url_row.addWidget(self._copy_btn)
        self._url_row = QWidget()
        self._url_row.setLayout(url_row)
        layout.addWidget(self._url_row)

        layout.addStretch(1)

        # Shown when nothing is waiting on the browser: before a sign-in the user
        # didn't start, and after one timed out.
        self._browser_btn = QPushButton("Sign in with your browser")
        self._browser_btn.setObjectName("primary")
        self._browser_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._browser_btn.clicked.connect(self._start_browser_flow)
        self._browser_btn.hide()
        layout.addWidget(self._browser_btn)
        layout.addSpacing(8)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("ghost")
        cancel_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cancel_btn.clicked.connect(self._cancel)
        layout.addWidget(cancel_btn)

        return page

    # ── Browser flow ────────────────────────────────────────────────────────

    def _copy_url(self) -> None:
        url = self._url_label.text()
        if url:
            QApplication.clipboard().setText(url)
            self._copy_btn.setText("Copied!")
            QTimer.singleShot(1500, lambda: self._copy_btn.setText("Copy"))

    def _show_browser_idle(self, title: str, detail: str = "") -> None:
        """The browser page with nothing in flight: a button opens the browser."""
        self._stop_browser_thread()
        self._browser_status.setText(title)
        self._browser_sub.setText(
            detail or "Your browser opens the Zenvi website. If you're already\n"
                      "signed in there, you only have to approve."
        )
        self._url_row.hide()
        self._browser_btn.show()
        self._browser_btn.setFocus()
        self.adjustSize()

    def _start_browser_flow(self) -> None:
        self._stop_browser_thread()
        self._browser_btn.hide()
        self._url_row.show()
        self._browser_status.setText("Opening browser…")
        self._browser_sub.setText(
            "Open the Authorize page in your browser.\n"
            "If you're already signed in, just click Continue.\n"
            "This window will close automatically."
        )
        self._url_label.clear()
        self.adjustSize()

        login_url, state = self._auth.start_auth_flow()
        self._url_label.setText(login_url)
        self._code = confirmation_code(state)
        print(f"[zenvi-auth] browser flow started: {login_url}", file=sys.stderr)
        QTimer.singleShot(1400, self._show_waiting)

        # The poll calls back on its own thread, possibly after this dialog
        # is gone: hop to the GUI thread and check it still exists there.
        self._browser_poll = self._auth.poll_for_session(
            state=state,
            on_success=self._from_poll(self._on_success),
            on_timeout=self._from_poll(self._on_browser_timeout),
        )

    def _from_poll(self, slot):
        def call(*args):
            def run():
                if not isdeleted(self) and self._browser_poll is not None:
                    slot(*args)
            invoke_on_gui(run)
        return call

    def _show_waiting(self) -> None:
        if self._browser_poll is not None:
            self._browser_status.setText("Waiting for browser…")
            self._browser_sub.setText(
                "Open the Authorize page in your browser.\n"
                "If you're already signed in, just click Continue.\n"
                f"The code is {self._code}. This window will close automatically."
            )

    @pyqtSlot()
    def _on_browser_timeout(self) -> None:
        print("[zenvi-auth] browser sign-in timed out", file=sys.stderr)
        self._show_browser_idle(
            "Browser sign-in timed out",
            "The browser didn't finish signing in.\nTry again to get a new sign-in page.",
        )
        self._browser_btn.setText("Try again")

    def _stop_browser_thread(self) -> None:
        # Only this dialog's hand-off: the chat may be waiting on one of its own.
        if self._browser_poll is not None:
            self._auth.cancel_poll(self._browser_poll)
            self._browser_poll = None

    @pyqtSlot(dict)
    def _on_success(self, session: dict) -> None:
        print(f"[zenvi-auth] login successful: {session.get('user_email')}", file=sys.stderr)
        self._stop_browser_thread()
        self.auth_completed.emit(session)
        self.accept()

    def _cancel(self) -> None:
        self.reject()

    def done(self, result) -> None:
        # Every way out -- Cancel, Escape, the close button, success -- ends
        # here; WA_DeleteOnClose deletes the dialog right after, so nothing
        # may still be waiting to call it back.
        self._stop_browser_thread()
        if result != QDialog.Accepted:
            self.auth_cancelled.emit()
        super().done(result)
