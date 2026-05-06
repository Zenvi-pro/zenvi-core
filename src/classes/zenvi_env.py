"""
Load Zenvi environment files from the install / repo root.

Order: ``.env`` (local overrides), then ``.env.production`` (defaults for shipped
builds). Only sets keys that are not already present in ``os.environ``, and
skips empty values so ``KEY=`` lines do not wipe runtime defaults.
"""

import os
import sys

_loaded = False


def zenvi_install_root() -> str:
    """Directory where ``.env`` lives: next to ``zenvi.exe`` when frozen, else repo root in dev.

    Relying on ``__file__`` alone breaks under some cx_Freeze layouts (e.g. zip imports),
    so frozen builds use the executable directory — the same place ``freeze.py`` drops ``.env``.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def _merge_env_file(env_path: str) -> None:
    try:
        if not os.path.isfile(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if not key or not value:
                    continue
                if key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def load_zenvi_dotenv(force: bool = False) -> None:
    """Merge ``.env`` and ``.env.production`` from the install root into the process env."""
    global _loaded
    if _loaded and not force:
        return
    root = zenvi_install_root()
    _merge_env_file(os.path.join(root, ".env"))
    _merge_env_file(os.path.join(root, ".env.production"))
    _loaded = True


def reset_zenvi_dotenv_cache_for_tests() -> None:
    global _loaded
    _loaded = False
