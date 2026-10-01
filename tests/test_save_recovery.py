"""Saving to a new path must not leave an empty recovery zip behind.

Testing RC v1.2.0 (#216): Save As to a new path logged "Failed to create zipped
recovery file" and left a 22-byte, empty zip in recovery/. ``save_project()``
takes the recovery snapshot *before* it writes the project -- the zip backs up
the version the save is about to overwrite -- and a new path has no such
version. File > Recovery then offered the empty zip as a previous version, and
restoring it moved the real project aside and restored nothing.

The methods are compiled from main_window.py's source, so the test runs without
Qt or libopenshot (importing the window module needs both).
"""

import ast
import datetime
import os
import time
import zipfile
from types import SimpleNamespace

from classes import info

_MAIN_WINDOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "windows", "main_window.py",
)


def _recovery_methods(recovery_dir, errors):
    """The real save_recovery/manage_recovery_files, bound to a stand-in window."""
    with open(_MAIN_WINDOW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _MAIN_WINDOW)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    names = ("save_recovery", "manage_recovery_files")
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    settings = SimpleNamespace(get=lambda key: {"recovery-limit": 10}[key])
    ns = {
        "os": os,
        "zipfile": zipfile,
        "time": time.time,
        "datetime": datetime.datetime,
        "info": SimpleNamespace(
            RECOVERY_PATH=recovery_dir, ALL_PROJECT_EXTS=info.ALL_PROJECT_EXTS
        ),
        "log": SimpleNamespace(
            error=lambda msg, *a, **k: errors.append(msg),
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
        ),
        "get_app": lambda: SimpleNamespace(get_settings=lambda: settings),
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), _MAIN_WINDOW, "exec"), ns)
    return type("RecoveryMethods", (), {name: ns[name] for name in names})()


def test_save_as_to_a_new_path_leaves_no_recovery_zip(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    errors = []
    window = _recovery_methods(str(recovery), errors)

    window.save_recovery(str(tmp_path / "Brand New.zvn"))

    assert os.listdir(str(recovery)) == []
    assert errors == []


def test_overwriting_a_saved_project_zips_the_previous_version(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    project = tmp_path / "Edit.zvn"
    project.write_text('{"version": "previous"}', encoding="utf-8")
    errors = []
    window = _recovery_methods(str(recovery), errors)

    window.save_recovery(str(project))

    zips = os.listdir(str(recovery))
    assert len(zips) == 1 and zips[0].endswith("-Edit.zip")
    with zipfile.ZipFile(str(recovery / zips[0])) as zf:
        assert zf.namelist() == ["Edit.zvn"]
        assert zf.read("Edit.zvn") == b'{"version": "previous"}'
    assert errors == []
