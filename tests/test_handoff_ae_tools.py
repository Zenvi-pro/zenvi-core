"""export_to_after_effects_tool / send_to_after_effects_tool through execute_tool (the editor harness)."""

import json
import os

import pytest

from classes.handoff import after_effects_export as X
from handoff_fakes import write_discovery
from test_handoff_ae_job import AeHost, fake_analyze, fake_render


@pytest.fixture
def ae(editor, tmp_path, monkeypatch):
    """The editor with two clips on real (tiny) media files, no Qt rendering, a temp user folder."""
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(info, "DOWNLOADS_PATH", str(tmp_path / "Downloads"))
    monkeypatch.setattr(X, "render_svg_png", fake_render)
    monkeypatch.setattr(X, "analyze_mask_image", fake_analyze)
    media = tmp_path / "media"
    media.mkdir()
    (media / "beach.mp4").write_bytes(b"beach")
    (media / "city.mp4").write_bytes(b"city")
    a = editor.add_file("video", path=str(media / "beach.mp4"))
    b = editor.add_file("video", path=str(media / "city.mp4"))
    editor.add_clip(a, position=0.0)
    editor.add_clip(b, position=30.0)
    editor.mark()
    editor.tmp = tmp_path
    return editor


def test_export_writes_the_script_and_changes_nothing(ae):
    before = json.dumps(ae.clips(), sort_keys=True)
    out = ae.tmp / "export"
    receipt = ae.call_receipt("export_to_after_effects_tool", output_dir=str(out))
    assert receipt["status"] == "applied" and receipt["undoSteps"] == 0
    data = receipt["data"]
    assert data["script"] == str(out / "Untitled.jsx") and os.path.isfile(data["script"])
    assert data["layers"] == 2 and data["footage"] == 2 and data["collected_media"] is True
    assert sorted(os.listdir(out / "media")) == ["beach.mp4", "city.mp4"]
    assert receipt["summary"].startswith("Exported 2 layer(s) for After Effects")
    assert ae.undo_steps_since_mark() == 0 and json.dumps(ae.clips(), sort_keys=True) == before


def test_export_defaults_next_to_downloads_for_an_unsaved_project_and_can_reveal(ae, monkeypatch):
    revealed = []
    monkeypatch.setattr(X, "reveal", revealed.append)
    receipt = ae.call_receipt("export_to_after_effects_tool", collect_media=False, open_folder=True)
    script = str(ae.tmp / "Downloads" / "Untitled_AfterEffects" / "Untitled.jsx")
    assert receipt["data"]["script"] == script and revealed == [script] and receipt["data"]["revealed"]
    assert not os.path.exists(ae.tmp / "Downloads" / "Untitled_AfterEffects" / "media")


def test_export_refuses_an_empty_timeline(editor):
    receipt = editor.call_receipt("export_to_after_effects_tool")
    assert receipt["status"] in ("error", "refused") and "no clips" in receipt["summary"]
    assert editor.undo_steps_since_mark() == 0


def test_export_reports_a_folder_it_cannot_use(ae):
    blocker = ae.tmp / "blocker"
    blocker.write_text("x")
    receipt = ae.call_receipt("export_to_after_effects_tool", output_dir=str(blocker))
    assert receipt["summary"].startswith("Error") and "is a file" in receipt["summary"]


def test_send_refuses_with_how_to_connect(ae):
    receipt = ae.call_receipt("send_to_after_effects_tool")
    assert receipt["summary"].startswith("Error") and "Zenvi Link" in receipt["summary"]
    assert "export_to_after_effects_tool" in receipt["summary"] and ae.undo_steps_since_mark() == 0


def test_send_builds_the_comp_in_after_effects(ae):
    host = AeHost()
    try:
        write_discovery(str(ae.tmp / "user"), host)
        receipt = ae.call_receipt("send_to_after_effects_tool")
    finally:
        host.stop()
    assert receipt["status"] == "applied" and receipt["undoSteps"] == 0
    data = receipt["data"]
    assert (data["comp"], data["comp_id"], data["layers"]) == ("Trip", 9, 3)
    assert data["script"].startswith(str(ae.tmp / "user" / "after_effects")) and os.path.isfile(data["script"])
    assert receipt["summary"].startswith("Built comp 'Trip' in After Effects with 3 layer(s)")
    tool_calls = [c for c in host.calls if c.get("method") == "tools/call"]
    assert tool_calls[-1]["params"]["name"] == "ae_run_jsx_file"
