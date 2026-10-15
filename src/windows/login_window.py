"""
Zenvi login window.

Primary flow  — browser OAuth:
  Opens ZENVI_WEBSITE/login?state=<uuid> in the default browser, then polls
  Supabase until the session is ready (POLL_TIMEOUT, the hand-off row's
  lifetime). A browser that is already signed in to Zenvi only has to approve.

Fallback flow — email / password form:
  Uses Supabase Auth REST API directly; no browser required. Only shown when
  the user asks for it.

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

from qt_api import Qt, QThread, QObject, QTimer, pyqtSignal, pyqtSlot
from qt_api import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from classes.auth_manager import AuthManager, AuthError
from classes import info

log = logging.getLogger(__name__)

_DARK = "#0A0A0A"
_SURFACE = "#141414"
_BORDER = "rgba(255,255,255,0.09)"
_TEXT = "#ffffff"
_MUTED = "rgba(255,255,255,0.38)"
_ACCENT = "#7C6FF7"

_DIALOG_QSS = f"""
QDialog {{
    background-color: {_DARK};
}}
QLabel {{
    background: transparent;
    border: none;
    color: {_TEXT};
}}
QLineEdit {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    color: {_TEXT};
    font-size: 13px;
    padding: 8px 10px;
    selection-background-color: {_ACCENT};
}}
QLineEdit:focus {{
    border-color: {_ACCENT};
}}
QPushButton#primary {{
    background: {_ACCENT};
    border: none;
    border-radius: 8px;
    color: {_TEXT};
    font-size: 13px;
    font-weight: 600;
    padding: 9px 0;
}}
QPushButton#primary:hover {{
    background: #8D88F8;
}}
QPushButton#primary:pressed {{
    background: #6B65E8;
}}
QPushButton#primary:disabled {{
    background: rgba(124,111,247,0.35);
    color: rgba(255,255,255,0.35);
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {_BORDER};
    border-radius: 8px;
    color: {_MUTED};
    font-size: 12px;
    padding: 7px 0;
}}
QPushButton#ghost:hover {{
    border-color: rgba(255,255,255,0.2);
    color: rgba(255,255,255,0.7);
}}
QPushButton#link {{
    background: transparent;
    border: none;
    color: {_MUTED};
    font-size: 11px;
    padding: 0;
    text-decoration: underline;
}}
QPushButton#link:hover {{
    color: rgba(255,255,255,0.65);
}}
"""

# ── Background workers ──────────────────────────────────────────────────────


class _PasswordWorker(QObject):
    """Calls sign_in or sign_up in a QThread."""
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, auth: AuthManager, email: str, password: str, signup: bool) -> None:
        super().__init__()
        self._auth = auth
        self._email = email
        self._password = password
        self._signup = signup

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self._signup:
                session = self._auth.sign_up_with_password(self._email, self._password)
            else:
                session = self._auth.sign_in_with_password(self._email, self._password)
            print(f"[zenvi-auth] success: {session.get('user_email')}", file=sys.stderr)
            self.succeeded.emit(session)
        except AuthError as exc:
            print(f"[zenvi-auth] failed: {exc}", file=sys.stderr)
            self.failed.emit(str(exc))
        except Exception as exc:
            print(f"[zenvi-auth] unexpected: {exc}", file=sys.stderr)
            self.failed.emit(f"Unexpected error: {exc}")


# ── Login dialog ────────────────────────────────────────────────────────────


class LoginWindow(QDialog):
    auth_completed = pyqtSignal(dict)
    auth_cancelled = pyqtSignal()

    # Thread-safe signals from background → main thread
    _sig_browser_success = pyqtSignal(dict)
    _sig_browser_timeout = pyqtSignal()

    _PAGE_BROWSER = 0
    _PAGE_PASSWORD = 1

    def __init__(self, parent=None, auto_open_browser: bool = True, message: str = "") -> None:
        super().__init__(parent)
        self._auth = AuthManager.instance()
        self._signup_mode = False
        self._message = message
        # Cancel handle of the browser hand-off this dialog is waiting for.
        self._browser_poll = None
        self._pw_thread: QThread | None = None
        self._pw_worker: _PasswordWorker | None = None

        self.setWindowTitle("Sign in to Zenvi")
        self.setFixedWidth(400)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        info.apply_application_icon(self)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(_DIALOG_QSS)

        self._build_ui()
        self._sig_browser_success.connect(self._on_success)
        self._sig_browser_timeout.connect(self._on_browser_timeout)

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
        wordmark.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(wordmark)
        return header

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)
        self._stack.addWidget(self._build_browser_page())
        self._stack.addWidget(self._build_password_page())

    def _build_browser_page(self) -> QFrame:
        page = QFrame()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 36)
        layout.setSpacing(0)

        layout.addWidget(self._brand_header())
        layout.addSpacing(4)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {_BORDER}; margin: 14px 0 18px 0;")
        layout.addWidget(sep)

        self._browser_status = QLabel("Opening browser…")
        self._browser_status.setAlignment(Qt.AlignCenter)
        self._browser_status.setStyleSheet("font-size: 14px; font-weight: 500; margin-bottom: 8px;")
        layout.addWidget(self._browser_status)

        self._browser_sub = QLabel(
            "Complete sign-in in your browser.\nThis window will close automatically."
        )
        self._browser_sub.setAlignment(Qt.AlignCenter)
        self._browser_sub.setWordWrap(True)
        self._browser_sub.setStyleSheet(f"font-size: 12px; color: {_MUTED}; line-height: 1.7;")
        layout.addWidget(self._browser_sub)

        layout.addSpacing(14)

        # URL copy row
        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        self._url_label = QLineEdit()
        self._url_label.setReadOnly(True)
        self._url_label.setPlaceholderText("Login URL…")
        self._url_label.setStyleSheet(
            f"background: {_SURFACE}; border: 1px solid {_BORDER}; border-radius: 6px;"
            f"color: {_MUTED}; font-size: 10px; padding: 5px 8px;"
        )
        url_row.addWidget(self._url_label)
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setObjectName("ghost")
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

        self._use_password_btn = QPushButton("Sign in with email instead")
        self._use_password_btn.setObjectName("ghost")
        self._use_password_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._use_password_btn.clicked.connect(self._show_password_page)
        layout.addWidget(self._use_password_btn)
        layout.addSpacing(8)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("ghost")
        cancel_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cancel_btn.clicked.connect(self._cancel)
        layout.addWidget(cancel_btn)

        return page

    def _build_password_page(self) -> QFrame:
        page = QFrame()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 36)
        layout.setSpacing(0)

        layout.addWidget(self._brand_header())

        self._pw_subtitle = QLabel("Sign in to your account")
        self._pw_subtitle.setAlignment(Qt.AlignCenter)
        self._pw_subtitle.setStyleSheet(f"font-size: 12px; color: {_MUTED}; margin-bottom: 24px;")
        layout.addWidget(self._pw_subtitle)

        self._email_input = QLineEdit()
        self._email_input.setPlaceholderText("Email")
        self._email_input.returnPressed.connect(self._on_password_submit)
        layout.addWidget(self._email_input)
        layout.addSpacing(8)

        self._password_input = QLineEdit()
        self._password_input.setPlaceholderText("Password")
        self._password_input.setEchoMode(QLineEdit.Password)
        self._password_input.returnPressed.connect(self._on_password_submit)
        layout.addWidget(self._password_input)
        layout.addSpacing(10)

        self._pw_error = QLabel("")
        self._pw_error.setAlignment(Qt.AlignCenter)
        self._pw_error.setWordWrap(True)
        self._pw_error.setStyleSheet(
            "font-size: 12px; color: #FF6B6B; background: rgba(255,107,107,0.08);"
            "border-radius: 6px; padding: 6px 8px; min-height: 0px;"
        )
        self._pw_error.hide()
        layout.addWidget(self._pw_error)
        layout.addSpacing(10)

        self._pw_submit_btn = QPushButton("Sign in")
        self._pw_submit_btn.setObjectName("primary")
        self._pw_submit_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pw_submit_btn.clicked.connect(self._on_password_submit)
        layout.addWidget(self._pw_submit_btn)
        layout.addSpacing(8)

        self._pw_toggle_btn = QPushButton("Create account")
        self._pw_toggle_btn.setObjectName("ghost")
        self._pw_toggle_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pw_toggle_btn.clicked.connect(self._toggle_signup)
        layout.addWidget(self._pw_toggle_btn)
        layout.addSpacing(8)

        back_btn = QPushButton("← Try browser sign-in again")
        back_btn.setObjectName("link")
        back_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        back_btn.clicked.connect(self._start_browser_flow)
        layout.addWidget(back_btn, alignment=Qt.AlignCenter)

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
        self._stack.setCurrentIndex(self._PAGE_BROWSER)
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
        self._stack.setCurrentIndex(self._PAGE_BROWSER)
        self._browser_btn.hide()
        self._url_row.show()
        self._browser_status.setText("Opening browser…")
        self._browser_sub.setText(
            "Complete sign-in in your browser.\nThis window will close automatically."
        )
        self._url_label.clear()
        self.adjustSize()

        login_url, state = self._auth.start_auth_flow()
        self._url_label.setText(login_url)
        print(f"[zenvi-auth] browser flow started: {login_url}", file=sys.stderr)
        QTimer.singleShot(1400, self._show_waiting)

        # The poll runs on its own thread; emitting these signals from there
        # queues the slots onto this dialog's (GUI) thread.
        self._browser_poll = self._auth.poll_for_session(
            state=state,
            on_success=self._sig_browser_success.emit,
            on_timeout=self._sig_browser_timeout.emit,
        )

    def _show_waiting(self) -> None:
        if self._browser_poll is not None:
            self._browser_status.setText("Waiting for authentication…")

    @pyqtSlot()
    def _on_browser_timeout(self) -> None:
        # Stay on the browser page: switching to the password form here is what
        # asked people who were signed in on the website for their password.
        print("[zenvi-auth] browser sign-in timed out", file=sys.stderr)
        self._show_browser_idle(
            "Browser sign-in timed out",
            "The browser didn't finish signing in.\nTry again, or sign in with your email.",
        )
        self._browser_btn.setText("Try again")

    def _show_password_page(self) -> None:
        self._stop_browser_thread()
        self._stack.setCurrentIndex(self._PAGE_PASSWORD)
        self.adjustSize()
        self._email_input.setFocus()

    # ── Password form ───────────────────────────────────────────────────────

    def _toggle_signup(self) -> None:
        self._signup_mode = not self._signup_mode
        self._pw_error.hide()
        if self._signup_mode:
            self._pw_subtitle.setText("Create your account")
            self._pw_submit_btn.setText("Create account")
            self._pw_toggle_btn.setText("Already have an account? Sign in")
        else:
            self._pw_subtitle.setText("Sign in to your account")
            self._pw_submit_btn.setText("Sign in")
            self._pw_toggle_btn.setText("Create account")
        self.adjustSize()

    def _on_password_submit(self) -> None:
        email = self._email_input.text().strip()
        password = self._password_input.text()
        self._pw_error.hide()

        if not email or not password:
            self._show_pw_error("Please enter your email and password.")
            return

        self._set_pw_loading(True)
        print(f"[zenvi-auth] attempting {'signup' if self._signup_mode else 'signin'} for {email}", file=sys.stderr)

        self._pw_worker = _PasswordWorker(self._auth, email, password, self._signup_mode)
        self._pw_thread = QThread(self)
        self._pw_worker.moveToThread(self._pw_thread)
        self._pw_worker.succeeded.connect(self._on_success)
        self._pw_worker.failed.connect(self._on_pw_failure)
        self._pw_thread.started.connect(self._pw_worker.run)
        self._pw_thread.start()

    def _set_pw_loading(self, loading: bool) -> None:
        self._pw_submit_btn.setEnabled(not loading)
        self._pw_submit_btn.setText(
            ("Creating account…" if self._signup_mode else "Signing in…") if loading
            else ("Create account" if self._signup_mode else "Sign in")
        )
        self._email_input.setEnabled(not loading)
        self._password_input.setEnabled(not loading)
        self._pw_toggle_btn.setEnabled(not loading)

    def _show_pw_error(self, msg: str) -> None:
        self._pw_error.setText(msg)
        self._pw_error.show()
        self.adjustSize()

    @pyqtSlot(str)
    def _on_pw_failure(self, message: str) -> None:
        self._stop_pw_thread()
        self._set_pw_loading(False)
        self._show_pw_error(message)

    # ── Shared ──────────────────────────────────────────────────────────────

    def _stop_browser_thread(self) -> None:
        # Only this dialog's hand-off: the chat may be waiting on one of its own.
        if self._browser_poll is not None:
            self._auth.cancel_poll(self._browser_poll)
            self._browser_poll = None

    def _stop_pw_thread(self) -> None:
        if self._pw_thread and self._pw_thread.isRunning():
            self._pw_thread.quit()
            self._pw_thread.wait(1000)
        self._pw_thread = None
        self._pw_worker = None

    @pyqtSlot(dict)
    def _on_success(self, session: dict) -> None:
        print(f"[zenvi-auth] login successful: {session.get('user_email')}", file=sys.stderr)
        # Fully stop (quit + wait) both QThreads before the dialog is destroyed.
        # WA_DeleteOnClose makes Qt delete this dialog right after accept(), and
        # any QThread still running as its child would trigger
        # "QThread: Destroyed while thread is still running" → SIGABRT.
        self._stop_browser_thread()
        self._stop_pw_thread()
        self.auth_completed.emit(session)
        self.accept()

    def _cancel(self) -> None:
        self._stop_browser_thread()
        self._stop_pw_thread()
        self.auth_cancelled.emit()
        self.reject()

    def closeEvent(self, event) -> None:
        self._stop_browser_thread()
        self._stop_pw_thread()
        self.auth_cancelled.emit()
        event.accept()
