"""Shared pytest setup: put the app's ``src`` directory on the import path."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
