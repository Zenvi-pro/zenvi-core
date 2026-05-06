"""
Load Zenvi environment files from the install / repo root.

Order: ``.env`` (local overrides), then ``.env.production`` (defaults for shipped
builds). Only sets keys that are not already present in ``os.environ``, and
skips empty values so ``KEY=`` lines do not wipe runtime defaults.
"""

import logging
import os
import sys

log = logging.getLogger(__name__)

_loaded = False


def _candidate_search_roots() -> list[str]:
    """Directories that may contain ``.env`` / ``.env.production`` (frozen + dev)."""
    roots: list[str] = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(exe_dir)
        roots.append(os.path.join(exe_dir, "lib"))
        parent = os.path.dirname(exe_dir)
        mac_res = os.path.join(parent, "Resources")
        if os.path.isdir(mac_res):
            roots.append(mac_res)
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.abspath(os.path.join(here, "..", "..")))
    # setuptools ``data_files`` (see setup.py): share/zenvi/.env.production — after
    # package root so a repo or install-local .env wins over system templates.
    _sys_share = os.path.join(sys.prefix, "share", "zenvi")
    if os.path.isdir(_sys_share):
        roots.append(_sys_share)
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        r = os.path.normpath(r)
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def zenvi_install_root() -> str:
    """Primary install directory (exe folder when frozen, else repo root in dev)."""
    roots = _candidate_search_roots()
    return roots[0]


def _first_env_path(filename: str) -> str | None:
    for root in _candidate_search_roots():
        p = os.path.join(root, filename)
        if os.path.isfile(p):
            return p
    return None


def _merge_env_file(env_path: str) -> None:
    try:
        if not os.path.isfile(env_path):
            return
        # utf-8-sig: Notepad / some editors write a BOM on Windows
        with open(env_path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1].strip()
                if not key or not value:
                    continue
                if key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        log.debug("Could not read env file %s: %s", env_path, exc)


def load_zenvi_dotenv(force: bool = False) -> None:
    """Merge ``.env`` then ``.env.production`` into the process environment."""
    global _loaded
    if _loaded and not force:
        return
    path_env = _first_env_path(".env")
    path_prod = _first_env_path(".env.production")
    if path_env:
        _merge_env_file(path_env)
    if path_prod:
        _merge_env_file(path_prod)
    if not path_env and not path_prod:
        log.debug(
            "No .env or .env.production found (searched under %s)",
            _candidate_search_roots(),
        )
    _loaded = True


def reset_zenvi_dotenv_cache_for_tests() -> None:
    global _loaded
    _loaded = False
