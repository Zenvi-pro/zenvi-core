"""Install a Remotion project's dependencies (``npm install``), off the GUI thread.

Used by File > Import Project > Remotion Project... ("Install dependencies"
when ``node_modules`` is missing) and by ``export_to_remotion_tool(install=true)``.
npm comes with Node, so it is the default; a project with a pnpm / yarn /
bun lockfile uses that tool when it is installed (npm otherwise, with a
warning). Output lines stream to ``on_progress(None, line)``; a cancel kills
the process tree (``jobs.JobCancelled``).
"""

from __future__ import annotations

import os
import shutil
from typing import Callable, List, Optional

from classes.handoff import node_runtime
from classes.handoff.linked_media import LinkError
from classes.handoff.remotion import detect

INSTALL_TIMEOUT = 30 * 60.0
_ARGS = {"npm": ("install", "--no-audit", "--no-fund"), "pnpm": ("install",), "yarn": ("install",),
         "bun": ("install",)}


def install_argv(runtime: node_runtime.NodeRuntime, manager: str, which=shutil.which) -> tuple:
    """(argv, manager actually used, warnings) for installing with *manager*."""
    warnings: List[str] = []
    if manager != "npm":
        exe = which(manager, path=runtime.env().get("PATH"))
        if exe:
            return [exe] + list(_ARGS[manager]), manager, warnings
        warnings.append(f"the project uses {manager} (its lockfile is there) but {manager} is not installed; "
                        "installed with npm instead")
    return runtime.npm_argv(*_ARGS["npm"]), "npm", warnings


def install_dependencies(project_dir: str, *, on_progress: Optional[Callable[[Optional[float], str], None]] = None,
                         should_cancel: Optional[Callable[[], bool]] = None, timeout: float = INSTALL_TIMEOUT,
                         runtime: Optional[node_runtime.NodeRuntime] = None) -> dict:
    """Run the project's package manager install in *project_dir*. Blocking; LinkError on failure."""
    from classes.handoff.jobs import JobCancelled
    if not os.path.isfile(os.path.join(project_dir, "package.json")):
        raise LinkError(f"{project_dir} has no package.json to install")
    try:
        runtime = runtime or node_runtime.find_node(18)
    except node_runtime.NodeNotFound as exc:
        raise LinkError(f"installing Remotion needs Node.js: {exc}") from None
    manager = detect.package_manager(project_dir)
    argv, used, warnings = install_argv(runtime, manager)
    report = on_progress or (lambda _f, _m: None)
    report(None, "Installing dependencies with %s" % used)

    def _line(line: str) -> None:
        text = line.strip()
        if text:
            report(None, text[:200])

    try:
        code, tail = node_runtime.run_node(argv, cwd=project_dir, on_line=_line, should_cancel=should_cancel,
                                           timeout=timeout, runtime=runtime)
    except node_runtime.NodeCancelled:
        raise JobCancelled("installing dependencies cancelled") from None
    except node_runtime.NodeTimeout as exc:
        raise LinkError(f"`{used} install` did not finish within {int(timeout / 60)} minutes: "
                        f"{' '.join(exc.tail.splitlines()[-3:])}") from None
    except node_runtime.NodeRunError as exc:
        raise LinkError(f"could not run {used}: {exc}") from None
    if should_cancel is not None and should_cancel():
        raise JobCancelled("installing dependencies cancelled")
    if code != 0:
        last = " ".join(line.strip() for line in tail.splitlines()[-6:] if line.strip())[:600]
        raise LinkError(f"`{used} install` failed (exit {code}) in {project_dir}: {last}")
    after = detect.inspect_project(project_dir)
    if after.missing:
        raise LinkError(f"`{used} install` finished but {', '.join(after.missing)} are still missing; check the "
                        "project's package.json lists remotion and @remotion/cli")
    return {"manager": used, "warnings": warnings, "remotion_version": after.version}


__all__ = ["install_dependencies", "install_argv", "INSTALL_TIMEOUT"]
