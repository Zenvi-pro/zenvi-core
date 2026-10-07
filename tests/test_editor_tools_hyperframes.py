"""HyperFrames editor tools: author, run and render motion graphics on this computer."""

import json
import os
import urllib.request
from types import SimpleNamespace

import pytest

from classes import hyperframes_local as hf
from classes import tool_handlers
from classes.editor_tools import REGISTRY
from classes.editor_tools import ai_generation_hyperframes as tools


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hyperframes"
    monkeypatch.setattr(hf, "home", lambda: str(root))
    (root / ".agents" / "skills" / "motion-graphics").mkdir(parents=True)
    (root / ".agents" / "skills" / "motion-graphics" / "SKILL.md").write_text("line1\nline2\nline3\n")
    return root


def test_tools_are_registered_for_the_assistant_and_the_mcp_server():
    for name in ("hyperframes_setup_tool", "hyperframes_read_file_tool", "hyperframes_write_file_tool",
                 "hyperframes_edit_file_tool", "hyperframes_list_files_tool", "hyperframes_run_tool",
                 "render_motion_graphic_tool", "import_motion_graphic_tool"):
        assert name in REGISTRY and name in tool_handlers.AGENT_TOOL_HANDLERS
    # the server-render era is gone
    for gone in ("fetch_motion_graphics_video_tool", "fetch_remotion_video_from_supabase_tool"):
        assert gone not in tool_handlers.AGENT_TOOL_HANDLERS
    assert not hasattr(tool_handlers, "_motion_graphics_cleanup_storage")
    assert not hasattr(tool_handlers, "_download_motion_graphics_file")


def test_write_read_edit_list_round_trip(editor, home):
    r = _receipt(editor.call("hyperframes_write_file_tool", project="title", path="index.html",
                             content="<div id='a'>Hello</div>\n"))
    assert r["bytes"] == 24 and os.path.isfile(os.path.join(str(home), "projects", "title", "index.html"))

    out = editor.call("hyperframes_edit_file_tool", project="title", path="index.html",
                      old_text="Hello", new_text="Tokyo")
    assert not out.startswith("Error"), out
    assert "Tokyo" in editor.call("hyperframes_read_file_tool", project="title", path="index.html")

    # an edit that does not match is refused and changes nothing
    out = editor.call("hyperframes_edit_file_tool", project="title", path="index.html",
                      old_text="Paris", new_text="Rome")
    assert out.startswith("Error") and "Tokyo" in editor.call(
        "hyperframes_read_file_tool", project="title", path="index.html")

    # empty search text would match between every character and wreck the file
    before = editor.call("hyperframes_read_file_tool", project="title", path="index.html")
    assert editor.call("hyperframes_edit_file_tool", project="title", path="index.html", old_text="",
                       new_text="X", replace_all=True).startswith("Error")
    assert editor.call("hyperframes_read_file_tool", project="title", path="index.html") == before

    listing = editor.call("hyperframes_list_files_tool", project="title", pattern="**/*")
    assert "index.html" in listing
    # skills are readable (and paged), never writable
    page = editor.call("hyperframes_read_file_tool", project="title", path="skills/motion-graphics/SKILL.md",
                       offset=2, limit=1)
    assert "line2" in page and "line1" not in page and "offset=3" in page
    assert "motion-graphics/SKILL.md" in editor.call(
        "hyperframes_list_files_tool", project="title", pattern="skills/*/SKILL.md")
    assert editor.call("hyperframes_write_file_tool", project="title", path="skills/x.md",
                       content="x").startswith("Error")
    assert editor.call("hyperframes_write_file_tool", project="title", path="../escape.html",
                       content="x").startswith("Error")


def test_run_refuses_what_is_not_allowlisted_and_reports_exit_codes(editor, home, monkeypatch):
    assert editor.call("hyperframes_run_tool", project="title", program="node",
                       args=["-e", "1"]).startswith("Error")
    assert editor.call("hyperframes_run_tool", project="title", program="hyperframes",
                       args=["publish"]).startswith("Error")

    calls = []
    monkeypatch.setattr(hf, "run", lambda project, program, args, **kw: (calls.append((project, program, args)),
                                                                           (1, "lint: 2 errors"))[1])
    out = editor.call("hyperframes_run_tool", project="title", program="hyperframes", args=["lint", "."])
    assert out.startswith("Error") and "lint: 2 errors" in out and "exit code 1" in out
    assert calls == [("title", "hyperframes", ["lint", "."])]


def test_setup_reports_what_is_missing_and_installs_only_with_consent(editor, home, monkeypatch):
    monkeypatch.setattr(hf, "find_program", lambda name: None)
    out = editor.call("hyperframes_setup_tool")
    assert "not ready" in out.lower() and "install=true" in out

    ran = []
    monkeypatch.setattr(hf, "install", lambda **kw: ran.append(kw) or hf.status())
    monkeypatch.setattr(tools, "_ask_install_consent", lambda steps: False)
    out = editor.call("hyperframes_setup_tool", install=True)
    assert out.startswith("Error") and "declined" in out and ran == []

    monkeypatch.setattr(tools, "_ask_install_consent", lambda steps: True)
    out = editor.call("hyperframes_setup_tool", install=True)
    assert len(ran) == 1
    # this fake install changed nothing, and the tool says so instead of claiming success
    assert out.startswith("Error") and "still missing" in out


@pytest.fixture
def rendered(editor, home, monkeypatch):
    """A fake CLI that writes the output file, and an import that records what it was given."""
    os.makedirs(hf.project_dir("title"))
    with open(os.path.join(hf.project_dir("title"), "index.html"), "w") as fh:
        fh.write("<div></div>")
    state = SimpleNamespace(commands=[], imported=[], fail=False, check={"ok": True})

    def fake_run(project, program, args, **kw):
        if args[0] == "check":
            body = state.check if isinstance(state.check, str) else json.dumps(state.check)
            return 0, body + "\n[hyperframes] browser log line"
        state.commands.append((program, list(args)))
        if state.fail:
            return 1, "render failed: missing asset"
        out = os.path.join(hf.project_dir(project), args[args.index("-o") + 1])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(b"\x1a\x45\xdf\xa3" + b"0" * 2048)
        return 0, "rendered"

    def fake_import(path, *, preserve_alpha=None):
        state.imported.append((path, preserve_alpha))
        file_id = editor.add_file("video", path=path)
        from classes.query import File
        return File.get(id=file_id), None

    monkeypatch.setattr(hf, "run", fake_run)
    monkeypatch.setattr(tool_handlers, "_import_generated_video", fake_import)

    def no_network(*a, **k):
        raise AssertionError("a local render must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    return state


def test_render_imports_the_local_file_and_stamps_it(editor, rendered):
    r = _receipt(editor.call("render_motion_graphic_tool", project="title", label="Tokyo Day 1 lower third"))

    program, args = rendered.commands[-1]
    assert program == "hyperframes" and args[0] == "render" and args[args.index("--format") + 1] == "webm"
    assert rendered.imported and rendered.imported[0][1] is True          # alpha kept
    assert rendered.imported[0][0].startswith(hf.project_dir("title"))    # straight from disk
    f = editor.file(r["file_id"])
    assert f["ai_metadata"]["source"] == "hyperframes_motion_graphics"
    assert f["ai_metadata"]["short_summary"] == "Tokyo Day 1 lower third"
    assert f["ai_metadata"]["transparent"] is True
    assert "motion_graphics" in f["tags"] and "transparent_overlay" in f["tags"]
    assert r["transparent"] is True


def test_opaque_render_is_mp4(editor, rendered):
    r = _receipt(editor.call("render_motion_graphic_tool", project="title", label="Title card",
                             transparent=False))
    args = rendered.commands[-1][1]
    assert args[args.index("--format") + 1] == "mp4" and rendered.imported[0][1] is False
    assert r["transparent"] is False and "transparent_overlay" not in editor.file(r["file_id"])["tags"]


def test_failed_render_imports_nothing(editor, rendered):
    rendered.fail = True
    out = editor.call("render_motion_graphic_tool", project="title", label="x")
    assert out.startswith("Error") and "missing asset" in out and rendered.imported == []


def test_import_takes_a_file_a_skill_already_rendered(editor, rendered):
    final = os.path.join(hf.project_dir("title"), "final.mp4")
    with open(final, "wb") as fh:
        fh.write(b"0" * 4096)
    r = _receipt(editor.call("import_motion_graphic_tool", project="title", path="final.mp4",
                             label="Captioned interview", transparent=False))
    assert rendered.imported == [(final, False)] and rendered.commands == []
    assert editor.file(r["file_id"])["ai_metadata"]["short_summary"] == "Captioned interview"
    assert editor.call("import_motion_graphic_tool", project="title", path="missing.mp4",
                       label="x").startswith("Error")


def test_render_is_refused_while_the_layout_is_off_canvas(editor, rendered):
    rendered.check = {"ok": True, "layout": {"findings": [{
        "code": "panel_out_of_canvas", "selector": "#lower-third-panel", "time": 0.83,
        "overflow": {"top": 57.9}, "text": "Dialogue Scene", "sourceFile": "./compositions/lower-third.html",
        "fixHint": "Move the panel inward."}]}}
    out = editor.call("render_motion_graphic_tool", project="title", label="Lower third")
    assert out.startswith("Error") and "#lower-third-panel" in out and "top" in out and "Move the panel inward" in out
    assert rendered.commands == [] and rendered.imported == []      # nothing rendered, nothing imported


def test_render_is_refused_on_check_errors_and_proceeds_when_the_check_cannot_run(editor, rendered):
    rendered.check = {"ok": False, "runtime": {"findings": [{"code": "js_error", "severity": "error",
                                                             "message": "gsap is not defined"}]}}
    out = editor.call("render_motion_graphic_tool", project="title", label="x")
    assert out.startswith("Error") and "gsap is not defined" in out and rendered.commands == []

    rendered.check = "not json at all"
    _receipt(editor.call("render_motion_graphic_tool", project="title", label="x"))
    assert rendered.commands and rendered.commands[-1][1][0] == "render"


def test_a_render_that_lost_its_alpha_is_an_error_with_the_way_out(editor, rendered, monkeypatch):
    files_before = len(editor.get("files") or [])
    monkeypatch.setattr(tool_handlers, "_import_generated_video",
                        lambda path, *, preserve_alpha=None: (None, "alpha import failed (no opaque fallback)"))
    out = editor.call("render_motion_graphic_tool", project="title", label="Lower third")
    assert out.startswith("Error") and "alpha import failed" in out and "transparent=false" in out
    assert len(editor.get("files") or []) == files_before
