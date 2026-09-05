"""
 @file
 @brief Marshal callables onto the Qt GUI thread (macOS, Windows, Linux).
 @author Zenvi

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of OpenShot Video Editor
 (http://www.openshot.org). Licensed under the GPLv3 or later.
 """

import threading

try:
    from PyQt5.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtSignal, pyqtSlot
except ImportError:
    QCoreApplication = None
    QObject = object
    QThread = None
    QTimer = None
    pyqtSignal = None
    pyqtSlot = None


# QTimer.singleShot(0, fn) from a background thread creates the timer on THAT
# thread (and the 3-arg context overload is missing from some PyQt5 builds).
# A QObject that lives on the GUI thread receives a QueuedConnection instead.

if pyqtSignal is not None:

    class _Dispatcher(QObject):
        _dispatch = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._dispatch.connect(self._on_dispatch)

        @pyqtSlot(object)
        def _on_dispatch(self, func):
            func()

else:

    class _Dispatcher:
        def _on_dispatch(self, func):
            func()


_dispatcher = None
_dispatcher_lock = threading.Lock()


def _get_dispatcher():
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher
    with _dispatcher_lock:
        if _dispatcher is not None:
            return _dispatcher
        d = _Dispatcher()
        app = QCoreApplication.instance() if QCoreApplication is not None else None
        if app is not None and hasattr(d, "moveToThread"):
            d.moveToThread(app.thread())
        _dispatcher = d
        return d


def is_gui_thread():
    """True when there is no Qt app, or we are already on its thread."""
    if QThread is None or QCoreApplication is None:
        return True
    app = QCoreApplication.instance()
    if app is None:
        return True
    try:
        return QThread.currentThread() is app.thread()
    except Exception:
        return True


def invoke_on_gui(func, *args, context=None, defer=False, **kwargs):
    """Run *func* on the GUI thread.

    On the GUI thread this calls *func* immediately (unless *defer* is True,
    which queues a 2-arg QTimer.singleShot — safe only on the GUI thread).
    Off the GUI thread it emits a signal to a dispatcher that lives on the
    application thread.
    """
    def _call():
        return func(*args, **kwargs)

    if is_gui_thread():
        if defer and QTimer is not None:
            QTimer.singleShot(0, _call)
            return None
        return _call()

    if QCoreApplication is None or QCoreApplication.instance() is None:
        return _call()

    _get_dispatcher()._dispatch.emit(_call)
    return None


def call_on_gui(func, *args, timeout=30, context=None, **kwargs):
    """Run *func* on the GUI thread and block until it finishes.

    Must not be used from the GUI thread while holding a lock the GUI work
    also needs. Timeouts raise ``TimeoutError``.
    """
    if is_gui_thread():
        return func(*args, **kwargs)

    if QCoreApplication is None or QCoreApplication.instance() is None:
        return func(*args, **kwargs)

    result_box = [None]
    error_box = [None]
    done = threading.Event()

    def _call():
        try:
            result_box[0] = func(*args, **kwargs)
        except Exception as exc:
            error_box[0] = exc
        finally:
            done.set()

    _get_dispatcher()._dispatch.emit(_call)
    if not done.wait(timeout=timeout):
        raise TimeoutError("GUI-thread call did not finish within %ss" % timeout)
    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]
