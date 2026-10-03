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
    from qt_api import QCoreApplication, QObject, QThread, QTimer, pyqtSignal, pyqtSlot
except ImportError:
    QCoreApplication = None
    QObject = object
    QThread = None
    QTimer = None
    pyqtSignal = None
    pyqtSlot = None


# QTimer.singleShot(0, fn) from a background thread creates the timer on THAT
# thread (and the 3-arg context overload is missing from some Qt5 binding builds).
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


# run_off_gui keeps the editor painting by pumping events, and a queued GUI
# call is an event: an agent tool's edit would run in the middle of the GUI
# flow that is waiting (and that may hold a snapshot of the project). Calls
# from other threads are held until no run_off_gui is waiting; its own worker's
# calls run at once (it is what the GUI thread waits for).
_off_gui_waits = []     # one entry per run_off_gui in progress (GUI thread only)
_off_gui_workers = set()
_held_calls = []        # GUI thread only


def _gated(call):
    sender = threading.get_ident()

    def _run():
        if _off_gui_waits and sender not in _off_gui_workers:
            _held_calls.append(_run)
        else:
            call()

    return _run


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

    _get_dispatcher()._dispatch.emit(_gated(_call))
    return None


def call_on_gui(func, *args, timeout=30, context=None, **kwargs):
    """Run *func* on the GUI thread and block until it finishes.

    Must not be used from the GUI thread while holding a lock the GUI work
    also needs. Raises ``TimeoutError`` when the call has not started within
    *timeout* (busy GUI thread, or held behind run_off_gui) -- it is then
    dropped and never runs -- or, once started, has not finished within
    another *timeout* (it is still running and will complete).
    """
    if is_gui_thread():
        return func(*args, **kwargs)

    if QCoreApplication is None or QCoreApplication.instance() is None:
        return func(*args, **kwargs)

    result_box = [None]
    error_box = [None]
    done = threading.Event()

    # A call that reported a timeout must not run afterwards (the caller may
    # retry): starting it and giving up on it exclude each other.
    state_lock = threading.Lock()
    state = {"started": False, "dropped": False}

    def _call():
        with state_lock:
            if state["dropped"]:
                return
            state["started"] = True
        try:
            result_box[0] = func(*args, **kwargs)
        except Exception as exc:
            error_box[0] = exc
        finally:
            done.set()

    _get_dispatcher()._dispatch.emit(_gated(_call))
    if not done.wait(timeout=timeout):
        with state_lock:
            if not state["started"]:
                # Still queued (a busy GUI thread, or held behind run_off_gui).
                state["dropped"] = True
                raise TimeoutError("GUI-thread call did not start within %ss" % timeout)
        if not done.wait(timeout=timeout):
            raise TimeoutError("GUI-thread call started but is still running after %ss" % timeout)
    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


def _pump_events():
    """Repaint and serve timers/sockets, but hold user input until the work is done."""
    try:
        from qt_api import QEventLoop

        QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents, 50)
    except Exception:
        pass  # no usable event loop (stubbed Qt in tests); the join still waits


def run_off_gui(func, *args, **kwargs):
    """Run *func* on a worker thread and return its result.

    Called on the GUI thread, this keeps the editor painting (without taking
    clicks or keys) until *func* finishes, for file I/O a synchronous GUI flow
    has to wait for. GUI calls other threads queue meanwhile (agent tools) run
    once the GUI flow is back in the event loop. Anywhere else *func* simply
    runs inline.
    """
    try:
        on_gui = QCoreApplication is not None and is_gui_thread()
    except Exception:
        on_gui = False  # no usable Qt (stubbed in tests): nothing to keep responsive
    if not on_gui:
        return func(*args, **kwargs)
    box = {}

    def _run():
        ident = threading.get_ident()
        _off_gui_workers.add(ident)
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as exc:
            box["error"] = exc
        finally:
            _off_gui_workers.discard(ident)

    _off_gui_waits.append(func)
    try:
        running = _start_worker(_run)
        while running():
            _pump_events()
    finally:
        _off_gui_waits.pop()
        if not _off_gui_waits and _held_calls:
            held, _held_calls[:] = list(_held_calls), []
            for call in held:
                # From the event loop (after the caller's GUI flow), and gated
                # again: a later run_off_gui pumps timers too.
                QTimer.singleShot(0, call)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _start_worker(target):
    """Start *target*; returns a callable that waits ~20 ms and says if it still runs.

    A QThread when real Qt is loaded: libopenshot readers and Qt image/SVG
    decoding are only safe there, not on a plain threading.Thread (see
    editor_tools.project_export_render.run_on_qthread).
    """
    if isinstance(QThread, type):
        class _Job(QThread):
            def run(self):
                target()

        job = _Job()
        job.start()
        return lambda: not job.wait(20)
    worker = threading.Thread(target=target, name="zenvi-off-gui", daemon=True)
    worker.start()

    def _running():
        worker.join(0.02)
        return worker.is_alive()

    return _running
