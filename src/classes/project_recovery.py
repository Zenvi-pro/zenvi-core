"""Recovery copies of a project (File > Recovery).

Every save zips the project into ``info.RECOVERY_PATH`` as
``<unix time>-<project name>.zip`` (older builds wrote the project file
itself). This lists the copies that belong to one project.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple


def recovery_files_for(project_path: str, recovery_dir: Optional[str] = None) -> List[Tuple[int, str]]:
    """(timestamp, path) of the recovery copies of *project_path*, newest first.

    Matches the project name exactly: "trip" does not pick up "trip-2" copies.
    """
    from classes import info

    if not project_path:
        return []
    recovery_dir = recovery_dir or info.RECOVERY_PATH
    if not os.path.isdir(recovery_dir):
        return []
    base = os.path.splitext(os.path.basename(project_path))[0]
    wanted = {base + ".zip"} | {base + ext for ext in info.ALL_PROJECT_EXTS}
    out = []
    for name in os.listdir(recovery_dir):
        stamp, sep, rest = name.partition("-")
        if not sep or rest not in wanted:
            continue
        try:
            out.append((int(stamp), os.path.join(recovery_dir, name)))
        except ValueError:
            continue
    out.sort(reverse=True)
    return out
