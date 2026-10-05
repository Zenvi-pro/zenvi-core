"""Open a source file at a line in the user's code editor (linked clips' "Open Code").

The ``handoff-code-editor`` preference picks the editor:

* ``auto`` (default): Cursor's or VS Code's command-line launcher (on PATH,
  or inside the macOS app bundle / Windows install folder), else the
  system's default app for the file;
* ``cursor`` / ``vscode``: that editor (an error when it is not installed);
* ``system``: the default app for the file type (no line);
* anything else: a command template, e.g. ``subl {file}:{line}`` or
  ``idea --line {line} {file}`` (``{file}``, ``{line}``, ``{folder}``).

:func:`open_in_editor` blocks briefly (it starts a program and waits up to a
few seconds for a launcher that fails at once): call it off the GUI thread.
The system-default route hops to the GUI thread for ``QDesktopServices``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from classes.logger import log

SETTING = "handoff-code-editor"
CHOICES = ("auto", "cursor", "vscode", "system")
LAUNCH_WAIT_SECONDS = 5.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


class EditorError(RuntimeError):
    """The editor could not be started; the message says what to change."""


@dataclass(frozen=True)
class EditorChoice:
    """How to open a file: ``argv`` with ``{file}``/``{line}``/``{folder}`` placeholders, or None = system app."""

    name: str
    argv: Optional[Sequence[str]]

    @property
    def is_system(self) -> bool:
        return self.argv is None


def _cli_candidates(editor: str, platform: str, env: dict) -> List[str]:
    """Launcher executables for *editor* ('cursor' or 'code'), most specific first."""
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    out = []
    on_path = shutil.which(editor, path=env.get("PATH") or None)
    if on_path:
        out.append(on_path)
    if platform == "darwin":
        bundles = {"cursor": ["Cursor.app"], "code": ["Visual Studio Code.app", "Visual Studio Code - Insiders.app"]}
        for root in ("/Applications", os.path.join(home, "Applications")):
            for bundle in bundles[editor]:
                out.append(os.path.join(root, bundle, "Contents", "Resources", "app", "bin", editor))
    elif platform == "win32":
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        program_files = env.get("ProgramFiles") or r"C:\Program Files"
        if editor == "cursor":
            out.append(os.path.join(local, "Programs", "cursor", "resources", "app", "bin", "cursor.cmd"))
        else:
            out.append(os.path.join(local, "Programs", "Microsoft VS Code", "bin", "code.cmd"))
            out.append(os.path.join(program_files, "Microsoft VS Code", "bin", "code.cmd"))
    else:
        if editor == "cursor":
            out += ["/usr/bin/cursor", "/opt/cursor/resources/app/bin/cursor", os.path.join(home, ".local", "bin",
                                                                                          "cursor")]
        else:
            out += ["/usr/bin/code", "/snap/bin/code", "/usr/share/code/bin/code"]
    return out


def find_cli(editor: str, platform: Optional[str] = None, env: Optional[dict] = None,
             exists: Callable[[str], bool] = os.path.isfile) -> Optional[str]:
    """Path of the ``cursor`` / ``code`` launcher, or None."""
    platform = platform or sys.platform
    env = dict(os.environ if env is None else env)
    for path in _cli_candidates(editor, platform, env):
        if path and exists(path):
            return path
    return None


def resolve_editor(setting: Optional[str] = None, *, platform: Optional[str] = None, env: Optional[dict] = None,
                   exists: Callable[[str], bool] = os.path.isfile) -> EditorChoice:
    """The editor *setting* means on this machine (EditorError for a named editor that is missing)."""
    value = str(setting or "auto").strip()
    key = value.lower()
    if key in ("", "auto"):
        for editor, name in (("cursor", "Cursor"), ("code", "VS Code")):
            cli = find_cli(editor, platform, env, exists)
            if cli:
                return EditorChoice(name, (cli, "-g", "{file}:{line}"))
        return EditorChoice("system", None)
    if key in ("cursor", "vscode", "code"):
        editor, name = ("cursor", "Cursor") if key == "cursor" else ("code", "VS Code")
        cli = find_cli(editor, platform, env, exists)
        if not cli:
            raise EditorError(f"{name} is not installed (no `{editor}` launcher found). Install it, or set "
                              "Preferences > General > Code Editor to auto or system")
        return EditorChoice(name, (cli, "-g", "{file}:{line}"))
    if key == "system":
        return EditorChoice("system", None)
    try:
        argv = shlex.split(value, posix=(platform or sys.platform) != "win32")
    except ValueError as exc:
        raise EditorError(f"the code editor command {value!r} is not valid: {exc}") from None
    if not argv:
        return EditorChoice("system", None)
    if not any("{file}" in a for a in argv):
        argv.append("{file}")
    return EditorChoice(os.path.basename(argv[0]), tuple(argv))


def build_command(choice: EditorChoice, file: str, line: Optional[int] = None) -> Optional[List[str]]:
    """The argv to run (None = open with the system app)."""
    if choice.argv is None:
        return None
    line_no = str(int(line)) if line else "1"
    folder = os.path.dirname(file)
    return [a.replace("{file}", file).replace("{line}", line_no).replace("{folder}", folder) for a in choice.argv]


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": _NO_WINDOW | _DETACHED}
    return {"start_new_session": True}


def _open_with_system(path: str) -> bool:
    from classes.qt_main_thread import call_on_gui

    def _open():
        from qt_api import QDesktopServices, QUrl
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))

    return bool(call_on_gui(_open, timeout=15))


def editor_setting() -> str:
    """The ``handoff-code-editor`` preference (``auto`` when unset)."""
    try:
        from classes.app import get_app
        value = get_app().get_settings().get(SETTING)
    except Exception:
        log.debug("code editor preference unavailable", exc_info=True)
        value = None
    return str(value or "auto")


def open_in_editor(file: str, line: Optional[int] = None, *, setting: Optional[str] = None,
                   folder: Optional[str] = None) -> dict:
    """Open *file* (at 1-based *line*) in the configured editor; returns ``{editor, command}``.

    *folder* (a project root) is opened instead when *file* is empty.
    Raises EditorError with what to change when nothing could be opened.
    Blocking (up to a few seconds): call off the GUI thread.
    """
    target = os.path.abspath(os.path.expanduser(str(file or folder or "")))
    if not target or not os.path.exists(target):
        raise EditorError(f"{target or 'the source'} does not exist")
    choice = resolve_editor(editor_setting() if setting is None else setting)
    argv = build_command(choice, target, line)
    if argv is None:
        if not _open_with_system(target):
            raise EditorError(f"the system could not open {os.path.basename(target)}; set Preferences > General > "
                              "Code Editor to an editor command")
        return {"editor": "system", "command": None, "file": target, "line": line}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, **_popen_kwargs())
    except OSError as exc:
        raise EditorError(f"could not start {choice.name}: {exc}") from None
    try:
        code = proc.wait(timeout=LAUNCH_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        code = None  # still running: an editor that stays in the foreground
    if code not in (None, 0):
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()[-400:]
        raise EditorError(f"{choice.name} exited with code {code}: {err or 'no message'}")
    return {"editor": choice.name, "command": argv, "file": target, "line": line}
