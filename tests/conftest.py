"""Shared pytest setup: import path + a headless Qt (PyQt5 / qt_api) and openshot stub.

Every test module used to install its own copy of the Qt stub with
``sys.modules.setdefault(...)``, which made the stub *order-dependent*: the
first module imported won, so a file that ran second inherited another file's
``QThread`` and had to work around it per-test (see the ``_patched`` helper in
test_tool_handlers_undo.py).  Installing it once here, before collection, makes
the stub identical for every module regardless of collection order.

``QThread`` is ``None`` on purpose: ``tool_handlers._run_on_main_thread`` and
``execute_tool`` treat that as "no Qt event loop" and call straight through
instead of marshalling to a main thread that does not exist headlessly.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import pathlib
import re
import sys
import types
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _qtcore_stub():
    qt = MagicMock()
    # QObject must be a real base class -- classes in src/ subclass it.
    qt.QObject = object
    # None => "no Qt main thread"; see module docstring.
    qt.QThread = None
    qt.pyqtSignal = lambda *a, **k: MagicMock()
    qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
    qt.QEventLoop = MagicMock
    qt.QPointF = MagicMock
    qt.QTimer = MagicMock
    return qt


class _StubQtModule(types.ModuleType):
    """A PyQt5 submodule whose attributes appear on demand.

    Qt *class* names (the ``Q`` prefix convention) resolve to real, empty
    classes rather than MagicMocks, because src/ subclasses them
    (``class DockWindow(DockingMixin, QMainWindow)``) and a mock instance
    cannot be used as a base -- it raises a metaclass conflict at import.
    Everything else is a MagicMock, which is what enums, functions and
    constants need to be.
    """

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.startswith("Q") and name[1:2].isupper():
            value = type(name, (object,), {})
        else:
            value = MagicMock()
        setattr(self, name, value)
        return value


class _QClassMeta(type):
    """Metaclass for stub Qt classes: unknown *class* attributes (enum members
    such as ``QMessageBox.Yes`` or ``QSizePolicy.Expanding``) are MagicMocks."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock()


def _make_q_class(name):
    """A real class (so src/ can subclass it) whose instances are MagicMocks.

    ``qt_api`` flattens QtCore/QtGui/QtWidgets into one namespace, so the stub
    cannot tell a value type (``QSize(100, 65)``, added and compared) from a
    widget base class (``class DockWindow(QMainWindow)``).  A MagicMock
    *subclass* serves both: it is a class, and its instances accept any
    constructor arguments and answer every method and operator.
    """

    def __init__(self, *args, **kwargs):
        MagicMock.__init__(self)

    return _QClassMeta(name, (MagicMock,), {"__init__": __init__})


class _StubQtApiModule(_StubQtModule):
    """Headless stand-in for ``src/qt_api.py`` (the Qt binding shim).

    src/ imports Qt through ``from qt_api import ...``.  Under the stub the
    real loader must not run: it would bind to the MagicMock PyQt5 installed
    below and then try to patch enums on mocks.  So the module itself is
    replaced.  Names the QtCore/QtWidgets stubs define explicitly (``QObject``,
    ``QThread`` is ``None``, ``pyqtSignal``, ...) resolve to the very same
    objects, so ``tool_handlers.QThread`` keeps the "no Qt event loop" meaning
    described in the module docstring.  Other ``Q``-prefixed class names come
    from ``_make_q_class``; anything else is a MagicMock.
    """

    _SHARED_CORE_NAMES = (
        "QObject", "QThread", "pyqtSignal", "pyqtSlot", "QEventLoop", "QPointF", "QTimer",
    )

    def __init__(self, name, qtcore, qtwidgets, qtgui):
        super().__init__(name)
        self.__path__ = []
        self.QtCore = qtcore
        self.QtWidgets = qtwidgets
        self.QtGui = qtgui
        self.QtSvg = None
        self.QT_API = "pyqt5"
        self.QT_VERSION_STR = "5.15.0"
        self.PYQT_VERSION_STR = "5.15.0"
        self.BINDING_VERSION_STR = "5.15.0"
        for attr in self._SHARED_CORE_NAMES:
            setattr(self, attr, getattr(qtcore, attr))
        self.Signal = qtcore.pyqtSignal
        self.Slot = qtcore.pyqtSlot
        self.QApplication = qtwidgets.QApplication
        self.ensure_binding = lambda: "pyqt5"
        # Real semantics: an object that exists has not been deleted.
        self.isdeleted = lambda obj: False

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.startswith("Q") and name[1:2].isupper():
            value = _make_q_class(name)
        else:
            value = MagicMock()
        setattr(self, name, value)
        return value


class _StubPyQt5Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve any not-yet-stubbed ``PyQt5.*`` import to a MagicMock module.

    The repo imports ten different PyQt5 submodules (QtGui, QtSvg, QtTest,
    QtWebEngineWidgets, QtWinExtras, ...).  Enumerating them here would rot;
    this answers for the whole namespace instead.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PyQt5" or fullname.startswith("PyQt5."):
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        mod = _StubQtModule(spec.name)
        mod.__spec__ = spec
        mod.__loader__ = self
        mod.__path__ = []  # mark as a package so submodule imports resolve
        return mod

    def exec_module(self, module):
        pass


def _install_stubs():
    """Install the headless stub. Returns True when it was installed.

    Stubbing is the DEFAULT even where a real PyQt5 is installed, so the suite
    behaves the same on a developer laptop as in CI. Gating on "is PyQt5
    importable?" would be worse than either choice: with real Qt present,
    ``tool_handlers.QThread`` is a real class, so ``execute_tool`` compares
    ``QThread.currentThread()`` against a MagicMock ``app.thread()``, decides
    it is on the wrong thread, and marshals the call into a Qt event loop that
    no test ever starts -- the test then blocks until the 30s main-thread
    timeout. The unit tests want the no-Qt path.

    Set ZENVI_REAL_QT=1 to opt out and run the GUI tests against real Qt.
    """
    if os.environ.get("ZENVI_REAL_QT") == "1":
        if importlib.util.find_spec("PyQt5") is None:
            raise RuntimeError(
                "ZENVI_REAL_QT=1 but PyQt5 is not installed in this environment"
            )
        return False

    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.__path__ = []
    sys.modules["PyQt5"] = pyqt5

    qtcore = _qtcore_stub()
    sys.modules["PyQt5.QtCore"] = qtcore
    pyqt5.QtCore = qtcore

    qtwidgets = MagicMock(QApplication=MagicMock)
    sys.modules["PyQt5.QtWidgets"] = qtwidgets
    pyqt5.QtWidgets = qtwidgets

    sys.meta_path.insert(0, _StubPyQt5Finder())

    # The Qt binding shim: replaced wholesale so its loader never runs on mocks.
    qtgui = _StubQtModule("PyQt5.QtGui")
    qtgui.__path__ = []
    sys.modules["PyQt5.QtGui"] = qtgui
    pyqt5.QtGui = qtgui
    sys.modules["qt_api"] = _StubQtApiModule("qt_api", qtcore, qtwidgets, qtgui)

    # libopenshot is a compiled extension; absent in headless CI.
    sys.modules.setdefault("openshot", types.ModuleType("openshot"))
    return True


_STUBBED = _install_stubs()


# Modules that ask for the *real* Qt with ``pytest.importorskip("PyQt5...")``
# would silently bind to the stub instead and then fail on things a mock cannot
# do (subclassing QMainWindow, running an event loop).  They used to skip only
# by collection-order luck -- they happened to be collected before whichever
# test module installed the stub.  Skip them explicitly while the stub is in
# place, so the reason is visible rather than accidental.
collect_ignore = []

if _STUBBED:
    _NEEDS_REAL_QT = re.compile(r"""importorskip\(\s*["']PyQt5""")
    for _path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        try:
            if _NEEDS_REAL_QT.search(_path.read_text(encoding="utf-8")):
                collect_ignore.append(_path.name)
        except OSError:
            continue
