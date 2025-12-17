"""
Timeline backend package helpers.

Ensures legacy ``classes`` imports work when OpenShot is installed under
the ``openshot_qt`` package.
"""

import os
import sys

try:
    import classes  # noqa: F401
except ImportError:
    try:
        import openshot_qt

        # Prefer OPENSHOT_PATH if upstream defines it, else use package dir
        pkg_dir = getattr(openshot_qt, "OPENSHOT_PATH", None) or os.path.dirname(
            openshot_qt.__file__
        )
        if pkg_dir and pkg_dir not in sys.path:
            sys.path.insert(0, pkg_dir)

        import classes  # noqa: F401
    except Exception:
        # Let the original ImportError surface from downstream imports
        pass
