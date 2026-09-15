"""Detect a *real* PyQt5, not a stub left in sys.modules by another test.

Headless test modules install MagicMock stubs under "PyQt5.QtCore" /
"PyQt5.QtWidgets" so they can import tool_handlers. That makes
pytest.importorskip("PyQt5.QtWidgets") succeed in any module collected
afterwards, which then fails for real on the next PyQt5 import. Checking the
import system instead of sys.modules is order-independent.
"""

import importlib.util

import pytest


def has_real_pyqt5() -> bool:
    try:
        return importlib.util.find_spec("PyQt5") is not None
    except (ImportError, ValueError):
        return False


def skip_without_pyqt5(*, allow_module_level: bool = True) -> None:
    if not has_real_pyqt5():
        pytest.skip("PyQt5 is not installed", allow_module_level=allow_module_level)
