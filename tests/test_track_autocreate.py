"""Placement tools add tracks an agent names just past the top of the stack.

track="6" with 5 tracks means "a new track on top" (the most common refusal in
the W28 transcript replay). Mirrors zenvi-web
packages/engine/src/tools/new-tracks.test.ts.
"""

import json

import pytest

from classes import track_ops
from classes.track_display import (
    MAX_NEW_TRACKS_ON_TOP,
    new_tracks_note,
    normalize_track_or_layer_arg,
    resolve_placement_track,
)
from timeline_edit_fakes import FakeTimeline, fake_openshot
from titles_text_fakes import receipt, tt  # noqa: F401  (fixture)

LAYERS = [{"id": f"L{n}", "number": n * 1000000, "label": ""} for n in range(1, 6)]
TOP = 5000000


def _numbers(editor):
    return sorted(int(t["number"]) for t in editor.get("layers"))


# ---------------------------------------------------------------------------
# resolve_placement_track
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, count", [("6", 1), ("track 7", 2), ("Track #8", 3)])
def test_one_to_three_past_the_top_names_new_tracks(raw, count):
    assert resolve_placement_track(raw, LAYERS) == (TOP + count * 1000000, None, count)


@pytest.mark.parametrize("raw", ["1", "5", "3000000", "L2", "", "track 2"])
def test_existing_tracks_resolve_as_before(raw):
    assert resolve_placement_track(raw, LAYERS)[:2] == normalize_track_or_layer_arg(raw, LAYERS)
    assert resolve_placement_track(raw, LAYERS)[2] == 0


@pytest.mark.parametrize("raw", ["9", "0", "-1", "music", "6.5", "track 6.5", "inf"])
def test_further_past_the_top_is_refused_as_before(raw):
    layer, err, new = resolve_placement_track(raw, LAYERS)
    assert layer is None and new == 0 and err.startswith("Error: Unknown track or layer")
    assert MAX_NEW_TRACKS_ON_TOP == 3


def test_a_numeric_label_wins_over_a_new_track():
    labelled = [dict(LAYERS[0], label="6")] + LAYERS[1:]
    assert resolve_placement_track("6", labelled) == (1000000, None, 0)


def test_first_tracks_of_a_project_with_none():
    assert resolve_placement_track("2", []) == (2000000, None, 2)
    assert normalize_track_or_layer_arg("2", []) == (None, "Error: No tracks in project.")


def test_note_wording():
    assert new_tracks_note(0) == ""
    assert new_tracks_note(1) == " on a new track"
    assert new_tracks_note(3) == " on a new track (3 tracks added on top)"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@pytest.fixture
def ed(editor, monkeypatch):
    from classes import clip_utils, timeline_ops
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    editor.window.selected_tracks = []
    return editor


def test_ensure_track_adds_the_tracks_in_between(editor):
    editor.mark()
    with track_ops._one_undo_step():
        assert track_ops.ensure_track(TOP + 3000000, "Titles") == TOP + 3000000
    assert _numbers(editor)[5:] == [6000000, 7000000, 8000000]
    labels = {int(t["number"]): t["label"] for t in editor.get("layers")}
    assert labels[8000000] == "Titles" and labels[7000000] == ""
    assert editor.undo_steps_since_mark() == 1
    assert track_ops.ensure_track(2000000) == 2000000 and len(_numbers(editor)) == 8


def test_add_clips_to_timeline_places_on_the_new_track_in_one_undo_step(ed):
    files = [ed.add_file("video", duration=d) for d in (4.0, 6.0)]
    ed.mark()
    out = ed.call("add_clips_to_timeline_tool", file_ids=files, track="6")
    head, _, body = out.partition("\n")
    assert head.startswith("Placed 2 clip(s) on track 6 on a new track from 0.00 s"), out
    r = json.loads(body)
    assert r["new_tracks"] == 1 and {c["layer"] for c in r["clips"]} == {6000000}
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert _numbers(ed) == [n * 1000000 for n in range(1, 6)]


def test_add_clips_four_past_the_top_is_refused_without_history(ed):
    f = ed.add_file("video", duration=4.0)
    ed.mark()
    out = ed.call("add_clips_to_timeline_tool", file_ids=[f], track="9")
    assert out.startswith("Error: Unknown track or layer '9'"), out
    assert ed.undo_steps_since_mark() == 0 and len(_numbers(ed)) == 5


def test_add_title_on_a_track_past_the_top(tt):
    tt.mark()
    out = tt.call("add_title_tool", text="Hi", track="7")
    assert out.startswith("Added title 'Hi' (Bar_1) on track 7 at 0.00-5.00s on a new track."), out
    rec = receipt(out)
    assert rec["new_track"] is True and rec["layer"] == 7000000
    assert _numbers(tt)[5:] == [6000000, 7000000]
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert len(_numbers(tt)) == 5


def test_blank_title_with_a_background_colour_is_a_solid_colour_card(tt):
    from classes import title_svg
    tt.mark()
    rec = receipt(tt.call("add_title_tool", text=" ", background_color="#FFFFFF", duration_seconds=0.1))
    assert rec["template"] == "Solid_Color" and rec["background_color"] == "#ffffff"
    assert title_svg.background_color(title_svg.load(rec["path"])) == ("#ffffff", 1.0)
    assert tt.undo_steps_since_mark() == 1


def test_blank_named_text_template_with_a_background_colour_blanks_its_fields(tt):
    from classes import title_svg
    rec = receipt(tt.call("add_title_tool", template="Standard_1", background_color="black"))
    assert all(not t.strip() or t == title_svg.BLANK for t in title_svg.line_texts(title_svg.load(rec["path"])))


class _Point:
    def __init__(self, x, y):
        self._x = x

    def x(self):
        return self._x


def test_add_clip_to_timeline_adds_the_tracks_and_the_clip_in_one_undo_step(tt, monkeypatch):
    from classes import tool_handlers
    monkeypatch.setattr(tool_handlers, "QPointF", _Point)
    f = tt.add_file("video", duration=4.0)
    tt.mark()
    out = tt.call("add_clip_to_timeline_tool", file_id=f, track="7", position_seconds="2", full_file="true")
    assert "on track 7000000 (stack_from_bottom=7, z_from_bottom=6) on a new track (2 tracks added on top)" in out, out
    assert _numbers(tt)[5:] == [6000000, 7000000]
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert len(_numbers(tt)) == 5


def test_add_clip_to_timeline_four_past_the_top_is_refused_without_history(tt):
    f = tt.add_file("video", duration=4.0)
    tt.mark()
    out = tt.call("add_clip_to_timeline_tool", file_id=f, track="9", position_seconds="2", full_file="true")
    assert out.startswith("Error: Unknown track or layer '9'"), out
    assert tt.undo_steps_since_mark() == 0 and len(_numbers(tt)) == 5
