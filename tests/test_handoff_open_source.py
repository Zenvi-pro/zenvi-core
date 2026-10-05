"""classes.handoff.open_source: which editor opens a linked clip's code, and how."""

import json
import os
import sys

import pytest

from classes.handoff import open_source as osrc


def _exists_only(*paths):
    wanted = set(paths)
    return lambda p: p in wanted


def test_auto_prefers_cursor_then_vscode_then_system():
    env = {"HOME": "/Users/me", "PATH": ""}
    cursor = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
    code = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    choice = osrc.resolve_editor("auto", platform="darwin", env=env, exists=_exists_only(cursor, code))
    assert choice.name == "Cursor" and choice.argv[0] == cursor
    assert osrc.build_command(choice, "/p/src/A.tsx", 12) == [cursor, "-g", "/p/src/A.tsx:12"]
    choice = osrc.resolve_editor("", platform="darwin", env=env, exists=_exists_only(code))
    assert choice.name == "VS Code"
    choice = osrc.resolve_editor("auto", platform="darwin", env=env, exists=_exists_only())
    assert choice.is_system and osrc.build_command(choice, "/p/a.tsx", 3) is None


def test_named_editor_that_is_missing_says_how_to_fix_it():
    with pytest.raises(osrc.EditorError, match="Code Editor"):
        osrc.resolve_editor("vscode", platform="linux", env={"HOME": "/h", "PATH": ""}, exists=_exists_only())


def test_windows_launchers_are_found_in_their_install_folders():
    env = {"USERPROFILE": "C:\\Users\\me", "LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local", "PATH": ""}
    code = os.path.join(env["LOCALAPPDATA"], "Programs", "Microsoft VS Code", "bin", "code.cmd")
    choice = osrc.resolve_editor("vscode", platform="win32", env=env, exists=_exists_only(code))
    assert choice.argv[0] == code


def test_custom_templates_get_file_line_and_folder():
    choice = osrc.resolve_editor("subl {file}:{line}", platform="darwin")
    assert osrc.build_command(choice, "/p/a.tsx", 7) == ["subl", "/p/a.tsx:7"]
    choice = osrc.resolve_editor("idea --line {line}", platform="darwin")
    assert osrc.build_command(choice, "/p/a.tsx", None) == ["idea", "--line", "1", "/p/a.tsx"]
    choice = osrc.resolve_editor("open -a Xcode {folder}", platform="darwin")
    assert osrc.build_command(choice, "/p/a.tsx", 2) == ["open", "-a", "Xcode", "/p", "/p/a.tsx"]
    with pytest.raises(osrc.EditorError):
        osrc.resolve_editor('code "unterminated', platform="darwin")


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell quoting of the test command")
def test_open_in_editor_runs_the_command_off_thread(tmp_path):
    target = tmp_path / "src" / "Intro.tsx"
    target.parent.mkdir()
    target.write_text("export {}")
    record = tmp_path / "args.json"
    script = tmp_path / "fake_editor.py"
    script.write_text("import json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n" % str(record))
    template = "%s %s {file}:{line}" % (sys.executable, script)
    out = osrc.open_in_editor(str(target), 12, setting=template)
    assert out["editor"] == os.path.basename(sys.executable)
    assert json.loads(record.read_text()) == [str(target) + ":12"]


def test_failing_launcher_and_missing_file_are_errors(tmp_path):
    target = tmp_path / "a.tsx"
    target.write_text("x")
    with pytest.raises(osrc.EditorError, match="exited with code 3"):
        osrc.open_in_editor(str(target), 1, setting='%s -c "import sys; sys.exit(3)" {file}' % sys.executable)
    with pytest.raises(osrc.EditorError, match="does not exist"):
        osrc.open_in_editor(str(tmp_path / "gone.tsx"), 1, setting="auto")


def test_relative_files_resolve_against_the_project_and_folder_is_the_root(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Intro.tsx").write_text("x")
    record = tmp_path / "args.json"
    script = tmp_path / "fake_editor.py"
    script.write_text("import json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n" % str(record))
    out = osrc.open_in_editor("src/Intro.tsx", 3, folder=str(project),
                              setting="%s %s {folder} {file}:{line}" % (sys.executable, script))
    assert out["file"] == str(project / "src" / "Intro.tsx")
    assert json.loads(record.read_text()) == [str(project), str(project / "src" / "Intro.tsx") + ":3"]


def test_windows_cmd_shims_run_the_electron_cli_directly_or_refuse_dangerous_names():
    root = "C:\\Users\\me\\AppData\\Local\\Programs\\Microsoft VS Code"
    shim = root + "\\bin\\code.cmd"
    import ntpath
    have = {ntpath.join(root, "resources", "app", "out", "cli.js"), ntpath.join(root, "Code.exe")}
    argv, env = osrc.windows_launcher([shim, "-g", "C:\\a&b\\x.tsx:1"], exists=lambda p: p in have)
    assert argv[0] == ntpath.join(root, "Code.exe") and argv[1].endswith("cli.js")
    assert env == {"ELECTRON_RUN_AS_NODE": "1"} and argv[-1] == "C:\\a&b\\x.tsx:1"  # no cmd.exe in between
    with pytest.raises(osrc.EditorError, match="cmd"):
        osrc.windows_launcher(["C:\\tools\\edit.cmd", "C:\\a&calc.tsx"], exists=lambda p: False)
    assert osrc.windows_launcher(["C:\\tools\\edit.cmd", "C:\\plain.tsx"], exists=lambda p: False)[0][0].endswith(".cmd")
    assert osrc.windows_launcher(["/usr/bin/code", "x"])[1] == {}
