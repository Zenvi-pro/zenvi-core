"""Writing an After Effects export to disk and running it in After Effects (classes.handoff.after_effects_export).

Everything runs against temp folders; title / wipe rendering is a fake (Qt
is stubbed here), Zenvi Link is the loopback ``FakeHost`` from
test_handoff_adobe_link.
"""

import json
import os
import shutil
import subprocess

import pytest

from ae_project import ProjectBuilder
from classes.handoff import adobe_link
from classes.handoff import after_effects_export as X
from handoff_fakes import FakeHost, write_discovery

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


def fake_render(svg, png, width, height):
    with open(png, "wb") as fh:
        fh.write(("png %s %dx%d" % (os.path.basename(svg), width, height)).encode())


def fake_analyze(path):
    if "fade" in os.path.basename(path):
        return X.MaskInfo(uniform=True, gray=0, alpha=255, width=720, height=576)
    return X.MaskInfo(uniform=False, width=1920, height=1080)


def _file(folder, name, body=b"media"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _project(tmp_path, *, titles=True, transitions=True):
    media = str(tmp_path / "media")
    b = ProjectBuilder()
    beach = b.add_file("video", path=_file(media, "beach.mp4", b"beach"))
    city = b.add_file("video", path=_file(media, "city.mp4", b"city"))
    b.add_clip(beach, track=1, position=0.0, start=0.0, end=5.0)
    b.add_clip(city, track=1, position=4.0, start=0.0, end=4.0)
    if transitions:
        fade = shutil.copy(os.path.join(SRC, "transitions", "common", "fade.svg"), tmp_path / "fade.svg")
        wipe = shutil.copy(os.path.join(SRC, "transitions", "common", "wipe_left_to_right.svg"), tmp_path / "wipe.svg")
        b.add_transition(track=1, position=4.0, duration=1.0, mask=str(fade))
        b.add_clip(beach, track=1, position=7.5, start=0.0, end=3.0)
        b.add_transition(track=1, position=7.5, duration=0.5, mask=str(wipe))
    if titles:
        simple = shutil.copy(os.path.join(SRC, "titles", "Standard_1.svg"), tmp_path / "Standard_1.svg")
        gold = shutil.copy(os.path.join(SRC, "titles", "Gold_1.svg"), tmp_path / "Gold_1.svg")
        b.add_clip(b.add_title(str(simple)), track=2, position=1.0, start=0.0, end=3.0)
        b.add_clip(b.add_title(str(gold)), track=3, position=2.0, start=0.0, end=3.0)
    return b


def _export(b, folder, **kw):
    kw.setdefault("render_svg", fake_render)
    kw.setdefault("analyze_mask", fake_analyze)
    kw.setdefault("created", "2026-10-04 21:00")
    kw.setdefault("generator", "Zenvi test")
    return X.export_after_effects(b.snapshot(), str(folder), **kw)


def _data(script_path):
    with open(script_path, encoding="utf-8") as fh:
        jsx = fh.read()
    return json.loads(jsx.split("    var DATA = ", 1)[1].split(";\n", 1)[0])


def test_export_writes_script_readme_media_titles_and_wipes(tmp_path):
    b = _project(tmp_path)
    out = tmp_path / "out"
    result = _export(b, out)
    assert result.script_path == str(out / "Trip.jsx") and os.path.isfile(result.script_path)
    assert sorted(os.listdir(out)) == ["README.txt", "Trip.jsx", "masks", "media", "titles"]
    assert sorted(os.listdir(out / "media")) == ["beach.mp4", "city.mp4"]
    assert (out / "media" / "beach.mp4").read_bytes() == b"beach"
    assert os.listdir(out / "titles") == ["Gold_1.png"] and os.listdir(out / "masks") == ["wipe.png"]
    assert [t["mode"] for t in result.titles] == ["native", "png"]
    data = _data(result.script_path)
    assert {f["rel"] for f in data["footage"]} == {"media/beach.mp4", "media/city.mp4", "titles/Gold_1.png",
                                                    "masks/wipe.png"}
    with open(result.readme_path, encoding="utf-8") as fh:
        readme = fh.read()
    assert "File > Scripts > Run Script File" in readme and "Gold_1: image (PNG)" in readme
    assert "Standard_1: editable text" in readme and "copied into media/" in readme
    assert not [n for n in os.listdir(out) if n.startswith(".zenvi-ae-")]


def test_reexports_reuse_identical_files_and_never_overwrite_other_content(tmp_path):
    b = _project(tmp_path, titles=False, transitions=False)
    out = tmp_path / "out"
    _export(b, out)
    first = os.stat(out / "media" / "beach.mp4").st_mtime
    _export(b, out)
    assert sorted(os.listdir(out / "media")) == ["beach.mp4", "city.mp4"]
    assert os.stat(out / "media" / "beach.mp4").st_mtime == first
    other = ProjectBuilder()
    clash = other.add_file("video", path=_file(str(tmp_path / "elsewhere"), "beach.mp4", b"another beach"))
    other.add_clip(clash, track=1, position=0.0, start=0.0, end=2.0)
    result = _export(other, out)
    assert sorted(os.listdir(out / "media")) == ["beach-2.mp4", "beach.mp4", "city.mp4"]
    assert (out / "media" / "beach.mp4").read_bytes() == b"beach"
    assert _data(result.script_path)["footage"][0]["rel"] == "media/beach-2.mp4"


def test_without_collecting_the_script_imports_media_in_place(tmp_path):
    b = _project(tmp_path, titles=False, transitions=False)
    result = _export(b, tmp_path / "out", collect_media=False)
    assert not os.path.exists(tmp_path / "out" / "media") and not result.collected
    entry = _data(result.script_path)["footage"][0]
    assert entry["abs"] == str(tmp_path / "media" / "beach.mp4") and entry["rel"] is None


def test_missing_media_and_image_sequences(tmp_path):
    b = ProjectBuilder()
    frames = tmp_path / "seq"
    for i in (1, 2, 3):
        _file(str(frames), "shot_%04d.png" % i, b"frame%d" % i)
    seq = b.add_file("video", path=str(frames / "shot_%04d.png"), duration=0.1)
    gone = b.add_file("video", path=str(tmp_path / "gone.mp4"))
    b.add_clip(seq, track=1, position=0.0, start=0.0, end=0.1)
    b.add_clip(gone, track=2, position=0.0, start=0.0, end=2.0)
    result = _export(b, tmp_path / "out")
    assert result.missing == [str(tmp_path / "gone.mp4")]
    assert sorted(os.listdir(tmp_path / "out" / "media" / "shot_04d")) == ["shot_0001.png", "shot_0002.png",
                                                                          "shot_0003.png"]
    footage = {f["name"]: f for f in _data(result.script_path)["footage"]}
    assert footage["shot_%04d.png"]["rel"] == "media/shot_04d/shot_0001.png" and footage["shot_%04d.png"]["seq"]
    assert footage["gone.mp4"]["missing"]
    assert X.sequence_frames(str(frames / "shot_%04d.png"))[0].endswith("shot_0001.png")


def test_cancelling_leaves_nothing_behind(tmp_path):
    b = _project(tmp_path)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(X.AeCancelled):
        _export(b, tmp_path / "out", should_cancel=cancel)
    assert os.listdir(tmp_path / "out") == []


def test_a_file_in_the_way_of_the_folder_is_refused(tmp_path):
    b = _project(tmp_path, titles=False, transitions=False)
    blocker = _file(str(tmp_path), "out")
    with pytest.raises(X.AeHandoffError, match="is a file"):
        _export(b, blocker)


def test_a_title_that_fails_to_render_is_noted_and_the_rest_exports(tmp_path):
    b = _project(tmp_path, transitions=False)

    def broken(svg, png, w, h):
        raise ValueError("invalid title SVG")

    result = _export(b, tmp_path / "out", render_svg=broken)
    assert os.path.isfile(result.script_path)
    assert any("Gold_1 could not be rendered" in w for w in result.warnings)
    assert [t["title"] for t in result.titles] == ["Standard_1"]


class _Result:
    def __init__(self, receipt, text=""):
        self.receipt, self.text, self.is_error = receipt, text, receipt.get("status") in ("error", "refused")


@pytest.mark.parametrize("receipt, text", [
    ({"status": "applied", "data": {"result": json.dumps({"zenvi_ae_import": 1, "comp": "A"})}}, ""),
    ({"status": "applied", "data": json.dumps({"zenvi_ae_import": 1, "comp": "A"})}, ""),
    ({"status": "applied", "data": {"zenvi_ae_import": 1, "comp": "A"}}, ""),
    ({"status": "applied", "summary": "Ran it: " + json.dumps({"zenvi_ae_import": 1, "comp": "A"})}, ""),
    ({"status": "applied"}, json.dumps({"zenvi_ae_import": 1, "comp": "A"})),
])
def test_the_import_summary_is_found_wherever_zenvi_link_puts_it(receipt, text):
    assert X.parse_import_summary(_Result(receipt, text))["comp"] == "A"


def test_no_summary_is_none():
    assert X.parse_import_summary(_Result({"status": "applied", "data": {"result": "done"}})) is None


class AeHost(FakeHost):
    """FakeHost that answers ae_run_jsx_file the way an export script returns its summary."""

    def __init__(self, summary=None):
        super().__init__()
        self.summary = summary or {"zenvi_ae_import": 1, "status": "ok", "comp": "Trip", "comp_id": 9, "layers": 3,
                                   "placeholders": [], "warnings": [], "summary": "Built comp \"Trip\""}

    def call(self, name, args):
        if name != "ae_run_jsx_file":
            return super().call(name, args)
        receipt = {"contract": 3, "status": "applied", "tool": name, "host": "aftereffects",
                   "summary": "Ran " + os.path.basename(args["path"]),
                   "data": {"result": json.dumps(self.summary)}, "warnings": [], "undoSteps": 1}
        return {"content": [{"type": "text", "text": json.dumps(receipt)}], "structuredContent": receipt,
                "isError": False}


@pytest.fixture
def ae_host(tmp_path):
    host = AeHost()
    yield host
    host.stop()


def test_send_exports_and_runs_the_script_through_zenvi_link(tmp_path, ae_host):
    base = write_discovery(str(tmp_path), ae_host)
    b = _project(tmp_path, titles=False)
    result = X.send_to_after_effects(b.snapshot(), base_dir=base, output_dir=str(tmp_path / "send"),
                                     render_svg=fake_render, analyze_mask=fake_analyze)
    assert result.summary["comp"] == "Trip" and result.message == "Built comp \"Trip\""
    call = [c for c in ae_host.calls if c.get("method") == "tools/call"][-1]
    assert call["params"] == {"name": "ae_run_jsx_file", "arguments": {"path": result.export.script_path}}
    with open(result.export.script_path, encoding="utf-8") as fh:
        assert '"interactive": false' in fh.read()
    assert not result.export.collected


def test_send_refuses_with_how_to_connect_when_after_effects_is_not_there(tmp_path):
    b = _project(tmp_path, titles=False, transitions=False)
    with pytest.raises(adobe_link.HostNotConnected) as err:
        X.send_to_after_effects(b.snapshot(), base_dir=str(tmp_path), output_dir=str(tmp_path / "send"))
    assert "Zenvi Link" in str(err.value) and not os.path.exists(tmp_path / "send")


def test_a_failed_import_in_after_effects_is_an_error(tmp_path):
    host = AeHost({"zenvi_ae_import": 1, "status": "error", "summary": "Error: the Zenvi import stopped: boom"})
    try:
        base = write_discovery(str(tmp_path), host)
        b = _project(tmp_path, titles=False, transitions=False)
        with pytest.raises(X.AeHandoffError, match="boom"):
            X.send_to_after_effects(b.snapshot(), base_dir=base, output_dir=str(tmp_path / "send"))
    finally:
        host.stop()


def test_running_the_script_waits_longer_than_the_link_default(monkeypatch, tmp_path):
    seen = {}

    def call_host_tool(app, tool, args=None, timeout=120.0, base_dir=None):
        seen.update(app=app, tool=tool, args=args, timeout=timeout)
        return _Result({"status": "applied", "data": {"result": json.dumps({"zenvi_ae_import": 1, "status": "ok"})}})

    monkeypatch.setattr(adobe_link, "call_host_tool", call_host_tool)
    X.run_in_after_effects("/x/Trip.jsx")
    assert seen == {"app": "aftereffects", "tool": "ae_run_jsx_file", "args": {"path": "/x/Trip.jsx"},
                    "timeout": X.AE_RUN_TIMEOUT}
    assert X.AE_RUN_TIMEOUT > adobe_link.DEFAULT_TIMEOUT


def test_folders(monkeypatch, tmp_path):
    from classes import info
    monkeypatch.setattr(info, "DOWNLOADS_PATH", str(tmp_path / "Downloads"))
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    assert X.default_output_dir("/work/Trip.zvn", "Trip") == "/work/Trip_AfterEffects"
    assert X.default_output_dir(None, "Untitled") == str(tmp_path / "Downloads" / "Untitled_AfterEffects")
    assert X.send_folder(None, "Untitled") == str(tmp_path / "user" / "after_effects" / "Untitled")
    saved = X.send_folder(str(tmp_path / "proj" / "Trip.zvn"), "Trip")
    assert saved.startswith(str(tmp_path / "proj")) and saved.endswith(os.path.join("after_effects", "Trip"))


def test_applescript_passes_the_script_as_an_argument():
    argv = X.applescript_command("/Applications/Adobe After Effects 2026/Adobe After Effects 2026.app",
                                 '/tmp/my "odd" folder/Trip.jsx')
    assert argv[0] == "osascript" and argv[-1] == '/tmp/my "odd" folder/Trip.jsx'
    lines = argv[2:-1:2]
    assert lines[0] == "on run argv" and 'tell application "Adobe After Effects 2026"' in lines
    assert "DoScriptFile (item 1 of argv)" in lines and all(a == "-e" for a in argv[1:-1:2])


def test_the_newest_after_effects_release_is_used(tmp_path):
    for name in ("Adobe After Effects 2025", "Adobe After Effects 2026", "Adobe After Effects (Beta)"):
        (tmp_path / name / (name + ".app")).mkdir(parents=True)
    found = X.find_after_effects_app([str(tmp_path)], platform="darwin")
    assert found.endswith("Adobe After Effects 2026.app")
    assert X.find_after_effects_app([str(tmp_path)], platform="win32") is None


def test_applescript_failures_say_what_to_allow(monkeypatch):
    def denied(*a, **k):
        return subprocess.CompletedProcess(a, 1, "", "execution error: Not authorized to send Apple events to "
                                                     "Adobe After Effects 2026. (-1743)")

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(X.AeHandoffError, match="Automation"):
        X.run_with_applescript("/x/Trip.jsx", "/Applications/Adobe After Effects 2026/Adobe After Effects 2026.app")

    def ok(*a, **k):
        return subprocess.CompletedProcess(a, 0, json.dumps({"zenvi_ae_import": 1, "comp": "Trip"}) + "\n", "")

    monkeypatch.setattr(subprocess, "run", ok)
    assert X.run_with_applescript("/x/Trip.jsx", "/Applications/AE.app")["comp"] == "Trip"
