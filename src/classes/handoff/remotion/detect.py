"""Is a folder a Remotion project, where is its entry point, and is it installed?

Reads ``package.json``, ``remotion.config.*`` and ``node_modules/*/package.json``
only (nothing here runs Node). It touches the disk, so call it off the GUI
thread like everything else in the handoff.

* A Remotion project has a ``package.json`` that depends on ``remotion`` or
  ``@remotion/cli``. A folder picked inside one (``src/``) resolves to it.
* The entry point comes from ``Config.setEntryPoint(...)`` in
  ``remotion.config.*``, then a ``remotion studio|render ... <entry>`` script
  in ``package.json``, then the CLI's own defaults (``src/index.ts`` ...).
* "Installed" means Node can resolve ``remotion``, ``@remotion/renderer`` and
  ``@remotion/bundler`` from the project (hoisted, nested under
  ``@remotion/cli``, pnpm's layout, or a workspace root above it). Zenvi never
  ships Remotion: it runs the project's own copy.
* A project Zenvi generated (``export_to_remotion_tool``) carries
  ``src/zenvi/timeline.json``; importing it can restore native clips.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from classes.handoff.linked_media import LinkError, SourceMissing

REMOTION_DEPENDENCIES = ("remotion", "@remotion/cli")
REQUIRED_PACKAGES = ("remotion", "@remotion/renderer", "@remotion/bundler")
CONFIG_FILES = ("remotion.config.ts", "remotion.config.js", "remotion.config.mjs", "remotion.config.cjs")
# @remotion/cli/dist/entry-point.js, in its order
DEFAULT_ENTRIES = (
    "src/index.ts", "src/index.tsx", "src/index.js", "src/index.mjs",
    "remotion/index.tsx", "remotion/index.ts", "remotion/index.js", "remotion/index.mjs",
    "src/remotion/index.tsx", "src/remotion/index.ts", "src/remotion/index.js", "src/remotion/index.mjs",
)
LOCKFILES = (("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"), ("pnpm-lock.yaml", "pnpm"),
             ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun"))
ZENVI_TIMELINE = "src/zenvi/timeline.json"
ZENVI_COMPOSITION = "ZenviTimeline"
MIN_MAJOR = 4
ROOT_SEARCH_DEPTH = 3

_ENTRY_CONFIG_RE = re.compile(r"""Config\s*\.\s*setEntryPoint\s*\(\s*(['"`])([^'"`]+?)\1\s*\)""")
_ENTRY_SCRIPT_RE = re.compile(
    r"""\bremotion\s+(?:studio|preview|render|still|compositions|bundle|benchmark)\s+"""
    r"""(?:(?:--?[\w-]+(?:=\S+)?)\s+)*['"]?([^\s'"-][^\s'"]*\.(?:tsx|ts|jsx|js|mjs|cjs))\b""")
_PUBLIC_DIR_RE = re.compile(r"""Config\s*\.\s*setPublicDir\s*\(\s*(['"`])([^'"`]+?)\1\s*\)""")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class NotRemotionProject(LinkError):
    """The folder is not a Remotion project; the message says why."""


@dataclass(frozen=True)
class RemotionProject:
    """What Zenvi knows about a Remotion project folder."""

    root: str                         # absolute folder holding package.json
    name: str                         # package.json name (or the folder name)
    entry: Optional[str]              # entry file, relative posix path (None: none found)
    entry_reason: str                 # "remotion.config" | "package.json script" | "default" | "none"
    config_file: Optional[str]        # remotion.config.* (relative), if any
    public_dir: str                   # relative ("public" unless the config sets another)
    declared_version: Optional[str]   # from package.json dependencies (range stripped)
    installed_version: Optional[str]  # node_modules/remotion/package.json
    missing: Tuple[str, ...]          # REQUIRED_PACKAGES Node cannot resolve
    package_manager: str              # npm | pnpm | yarn | bun (from the lockfile; npm by default)
    zenvi_timeline: Optional[str]     # absolute path of src/zenvi/timeline.json when Zenvi made it

    @property
    def installed(self) -> bool:
        return not self.missing

    @property
    def version(self) -> Optional[str]:
        return self.installed_version or self.declared_version

    @property
    def is_zenvi_generated(self) -> bool:
        return self.zenvi_timeline is not None

    @property
    def entry_path(self) -> Optional[str]:
        return os.path.join(self.root, *self.entry.split("/")) if self.entry else None

    @property
    def major(self) -> Optional[int]:
        m = _VERSION_RE.search(self.version or "")
        return int(m.group(1)) if m else None

    def install_command(self) -> str:
        return {"pnpm": "pnpm install", "yarn": "yarn", "bun": "bun install"}.get(self.package_manager,
                                                                                  "npm install")

    def as_dict(self) -> dict:
        return {"project_dir": self.root, "name": self.name, "entry": self.entry, "entry_reason": self.entry_reason,
                "config_file": self.config_file, "public_dir": self.public_dir,
                "remotion_version": self.version, "installed": self.installed, "missing": list(self.missing),
                "package_manager": self.package_manager, "zenvi_generated": self.is_zenvi_generated}


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_text(path: str, limit: int = 512 * 1024) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _dependencies(package: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = package.get(key)
        if isinstance(deps, dict):
            for name, spec in deps.items():
                out.setdefault(str(name), str(spec))
    return out


def depends_on_remotion(package: Optional[dict]) -> bool:
    deps = _dependencies(package or {})
    return any(name in deps for name in REMOTION_DEPENDENCIES)


def find_project_root(path: str, depth: int = ROOT_SEARCH_DEPTH) -> Optional[str]:
    """The Remotion project folder at or above *path* (a folder picked inside one), else None."""
    current = os.path.abspath(os.path.expanduser(str(path or "")))
    if os.path.isfile(current):
        current = os.path.dirname(current)
    for _ in range(depth + 1):
        package = _read_json(os.path.join(current, "package.json"))
        if package is not None and depends_on_remotion(package):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _strip_range(spec: Optional[str]) -> Optional[str]:
    m = _VERSION_RE.search(str(spec or ""))
    return "%s.%s.%s" % m.groups() if m else None


def parse_config_entry(text: str) -> Optional[str]:
    """The path given to ``Config.setEntryPoint(...)`` in a remotion.config file."""
    m = _ENTRY_CONFIG_RE.search(_strip_line_comments(text))
    return m.group(2).strip() if m else None


def parse_config_public_dir(text: str) -> Optional[str]:
    m = _PUBLIC_DIR_RE.search(_strip_line_comments(text))
    return m.group(2).strip() if m else None


def _strip_line_comments(text: str) -> str:
    return "\n".join(line for line in str(text or "").splitlines() if not line.lstrip().startswith("//"))


def parse_script_entries(scripts: Optional[dict]) -> List[str]:
    """Entry files named in ``remotion studio|render|... <entry>`` package.json scripts, in order."""
    out: List[str] = []
    if not isinstance(scripts, dict):
        return out
    for value in scripts.values():
        for m in _ENTRY_SCRIPT_RE.finditer(str(value or "")):
            entry = m.group(1)
            if entry not in out:
                out.append(entry)
    return out


def _normalize_rel(root: str, rel: str) -> Optional[str]:
    rel = str(rel or "").strip()
    if not rel:
        return None
    full = os.path.normpath(os.path.join(root, rel)) if not os.path.isabs(rel) else os.path.normpath(rel)
    if not os.path.isfile(full):
        return None
    out = os.path.relpath(full, root)
    if out.startswith(".."):
        return None
    return out.replace(os.sep, "/")


def find_entry(root: str, package: Optional[dict] = None, config_text: str = "") -> Tuple[Optional[str], str]:
    """(entry relative to *root*, reason) -- config, then package.json scripts, then the CLI defaults."""
    configured = parse_config_entry(config_text) if config_text else None
    if configured:
        found = _normalize_rel(root, configured)
        if found:
            return found, "remotion.config"
    for scripted in parse_script_entries((package or {}).get("scripts")):
        found = _normalize_rel(root, scripted)
        if found:
            return found, "package.json script"
    for candidate in DEFAULT_ENTRIES:
        if os.path.isfile(os.path.join(root, *candidate.split("/"))):
            return candidate, "default"
    return None, "none"


def package_dir(root: str, name: str) -> Optional[str]:
    """Where Node would resolve package *name* from *root* (hoisted, a workspace root above, or under @remotion/cli)."""
    parts = name.split("/")
    current = os.path.abspath(root)
    while True:
        candidate = os.path.join(current, "node_modules", *parts)
        if os.path.isfile(os.path.join(candidate, "package.json")):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    cli = package_dir(root, "@remotion/cli") if name != "@remotion/cli" else None
    if cli:
        real = os.path.realpath(cli)
        for candidate in (os.path.join(real, "node_modules", *parts),                    # npm nested
                          os.path.join(os.path.dirname(os.path.dirname(real)), *parts)):  # pnpm siblings
            if os.path.isfile(os.path.join(candidate, "package.json")):
                return candidate
    return None


def installed_version(root: str, name: str = "remotion") -> Optional[str]:
    folder = package_dir(root, name)
    if not folder:
        return None
    data = _read_json(os.path.join(folder, "package.json")) or {}
    version = data.get("version")
    return str(version) if version else None


def package_manager(root: str) -> str:
    for lockfile, manager in LOCKFILES:
        if os.path.isfile(os.path.join(root, lockfile)):
            return manager
    return "npm"


def zenvi_timeline_path(root: str) -> Optional[str]:
    """``src/zenvi/timeline.json`` of a Zenvi-generated project (checked by its header), else None."""
    path = os.path.join(root, *ZENVI_TIMELINE.split("/"))
    if not os.path.isfile(path):
        return None
    head = _read_text(path, limit=4096)
    return path if re.search(r'"zenvi_timeline"\s*:\s*\d+', head) else None


_SOURCE_PROJECT_RE = re.compile(r'"source_project"\s*:\s*("(?:[^"\\]|\\.)*")')


def zenvi_source_project(root: str) -> Optional[str]:
    """The Zenvi project name a Zenvi export came from (``source_project``, read from the file's first 4 KB)."""
    path = os.path.join(root, *ZENVI_TIMELINE.split("/"))
    m = _SOURCE_PROJECT_RE.search(_read_text(path, limit=4096))
    if not m:
        return None
    try:
        name = json.loads(m.group(1))
    except ValueError:
        return None
    return str(name).strip() or None if isinstance(name, str) else None


def config_file(root: str) -> Optional[str]:
    for name in CONFIG_FILES:
        if os.path.isfile(os.path.join(root, name)):
            return name
    return None


def inspect_project(path: str) -> RemotionProject:
    """Everything Zenvi needs to know about the Remotion project at (or just above) *path*.

    Raises SourceMissing when the folder is gone and NotRemotionProject when
    it is not a Remotion project (with what to pick instead).
    """
    folder = os.path.abspath(os.path.expanduser(str(path or "")))
    if not path or not os.path.exists(folder):
        raise SourceMissing(f"the folder {folder} does not exist")
    root = find_project_root(folder)
    if root is None:
        if not os.path.isfile(os.path.join(folder, "package.json")):
            raise NotRemotionProject(f"{folder} is not a Remotion project: it has no package.json. Pick the folder "
                                     "that holds the project's package.json (the one with remotion in its "
                                     "dependencies)")
        raise NotRemotionProject(f"{folder} is not a Remotion project: its package.json does not depend on "
                                 "remotion or @remotion/cli")
    package = _read_json(os.path.join(root, "package.json")) or {}
    config = config_file(root)
    config_text = _read_text(os.path.join(root, config)) if config else ""
    entry, reason = find_entry(root, package, config_text)
    public = (parse_config_public_dir(config_text) if config_text else None) or "public"
    deps = _dependencies(package)
    declared = _strip_range(deps.get("remotion") or deps.get("@remotion/cli"))
    missing = tuple(name for name in REQUIRED_PACKAGES if package_dir(root, name) is None)
    return RemotionProject(
        root=root,
        name=str(package.get("name") or os.path.basename(root)),
        entry=entry,
        entry_reason=reason,
        config_file=config,
        public_dir=public.strip("/").replace("\\", "/") or "public",
        declared_version=declared,
        installed_version=installed_version(root),
        missing=missing,
        package_manager=package_manager(root),
        zenvi_timeline=zenvi_timeline_path(root),
    )


def require_ready(project: RemotionProject) -> RemotionProject:
    """*project* when Zenvi can render it; LinkError saying what to do otherwise."""
    if not project.entry:
        raise NotRemotionProject(
            f"{project.root} has no Remotion entry point (looked for Config.setEntryPoint in remotion.config, a "
            f"'remotion studio <entry>' script in package.json and {', '.join(DEFAULT_ENTRIES[:2])} ...). "
            "Add src/index.ts that calls registerRoot()")
    if project.missing:
        raise LinkError(
            f"the Remotion project {project.name} is not installed ({', '.join(project.missing)} missing from "
            f"node_modules). Run `{project.install_command()}` in {project.root}, or use File > Import Project > "
            "Remotion Project..., which offers to install it")
    major = project.major
    if major is not None and major < MIN_MAJOR:
        raise LinkError(f"{project.name} uses Remotion {project.version}; Zenvi needs Remotion {MIN_MAJOR} or newer "
                        "(npx remotion upgrade)")
    return project


__all__ = [
    "RemotionProject", "NotRemotionProject", "inspect_project", "require_ready", "find_project_root", "find_entry",
    "parse_config_entry", "parse_config_public_dir", "parse_script_entries", "package_dir", "installed_version",
    "package_manager", "zenvi_timeline_path", "zenvi_source_project", "depends_on_remotion", "DEFAULT_ENTRIES",
    "REQUIRED_PACKAGES",
    "ZENVI_TIMELINE", "ZENVI_COMPOSITION",
]
