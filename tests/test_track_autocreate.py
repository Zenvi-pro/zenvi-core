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


# ---------------------------------------------------------------------------
# A busy named track: the overlay goes to the first free track above, with a warning
# ---------------------------------------------------------------------------

def test_title_for_a_busy_explicit_track_goes_to_the_first_free_track_above(tt):
    video = tt.add_file("video", duration=10.0)
    tt.add_clip(video, position=0.0, layer=2000000)
    tt.mark()
    out = tt.call("add_title_tool", text="X", track="2", position_seconds=5)
    rec = receipt(out)
    assert rec["layer"] == 3000000 and rec["track"] == 3 and rec["new_track"] is False
    assert len(rec["warnings"]) == 1
    assert rec["warnings"][0].startswith("track 2 already has ") and rec["warnings"][0].endswith(
        "during 5.00-10.00s; placed on track 3 instead")
    assert f" Warning: {rec['warnings'][0]}." in out.partition("\n")[0]
    assert tt.undo_steps_since_mark() == 1


def test_title_for_a_busy_track_opens_a_new_track_when_all_above_are_busy_too(tt):
    video = tt.add_file("video", duration=30.0)
    for n in range(1, 6):
        tt.add_clip(video, position=0.0, layer=n * 1000000)
    tt.mark()
    rec = receipt(tt.call("add_title_tool", text="X", track="5"))
    assert rec["layer"] == 6000000 and rec["new_track"] is True
    assert "placed on a new track on top (track 6) instead" in rec["warnings"][0]
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert len(_numbers(tt)) == 5


def test_title_for_a_busy_track_skips_a_locked_free_one(tt):
    video = tt.add_file("video", duration=10.0)
    tt.add_clip(video, position=0.0, layer=2000000)
    tt.lock_track(3000000)
    rec = receipt(tt.call("add_title_tool", text="X", track="2"))
    assert rec["layer"] == 4000000 and len(rec["warnings"]) == 1


def test_a_free_named_track_has_no_warning(tt):
    rec = receipt(tt.call("add_title_tool", text="X", track="2"))
    assert "warnings" not in rec and rec["layer"] == 2000000


def test_timer_for_a_busy_explicit_track_goes_above_with_a_warning(tt):
    video = tt.add_file("video", duration=30.0)
    tt.add_clip(video, position=0.0, layer=1000000)
    tt.mark()
    out = tt.call("add_timer_tool", mode="count_up", duration_seconds=3, track="1")
    rec = receipt(out)
    assert rec["layer"] == 2000000 and rec["warnings"][0].endswith("placed on track 2 instead")
    assert " Warning: track 1 already has" in out.partition("\n")[0]
    assert tt.undo_steps_since_mark() == 1


# ---------------------------------------------------------------------------
# Text on a colour card
# ---------------------------------------------------------------------------

def test_text_on_solid_color_is_drawn_with_a_text_template_over_the_colour(tt):
    from classes import title_svg
    tt.mark()
    out = tt.call("add_title_tool", template="Solid_Color", text="DOUBT", background_color="#d4141c",
                  text_color="#ffffff")
    rec = receipt(out)
    assert rec["template"] == "Text_Card" and rec["background_color"] == "#d4141c"
    doc = title_svg.load(rec["path"])
    assert title_svg.background_color(doc) == ("#d4141c", 1.0)
    assert title_svg.line_texts(doc)[0] == "DOUBT"
    assert rec["warnings"] == ["Solid_Color has no text; drew the text with Text_Card over the colour #d4141c"]
    assert " Warning: Solid_Color has no text; drew the text with Text_Card" in out.partition("\n")[0]
    assert tt.undo_steps_since_mark() == 1


def test_text_on_solid_color_keeps_the_cards_own_colour_without_a_background_colour(tt):
    from classes import title_svg
    rec = receipt(tt.call("add_title_tool", template="Solid_Color", text="Hi"))
    colour, alpha = title_svg.background_color(title_svg.load(rec["path"]))
    assert alpha == 1.0 and rec["background_color"] == colour


# ---------------------------------------------------------------------------
# duplicate_clips_tool / move_clips_tool to a track past the top
# ---------------------------------------------------------------------------

def _logo_and_clip(ed, layer=3000000):
    f = ed.add_file("video", duration=10.0)
    return ed.add_clip(f, position=0.0, end=4.0, layer=layer)


def test_duplicate_to_a_track_past_the_top_adds_it_in_one_undo_step(ed):
    a = _logo_and_clip(ed)
    ed.mark()
    out = ed.call("duplicate_clips_tool", timeline_clip_ids=[a], to_track="6")
    head, _, body = out.partition("\n")
    assert "on track 6 on a new track" in head, out
    r = json.loads(body)
    assert r["new_tracks"] == 1
    assert [c["layer"] for c in ed.clips() if c.get("layer") == 6000000] == [6000000]
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert _numbers(ed) == [n * 1000000 for n in range(1, 6)]


def test_duplicate_keeps_the_stacking_and_adds_the_tracks_between(ed):
    a = _logo_and_clip(ed, 1000000)
    b = _logo_and_clip(ed, 3000000)
    ed.mark()
    receipt(ed.call("duplicate_clips_tool", timeline_clip_ids=[a, b], to_track="8"))
    assert _numbers(ed)[5:] == [6000000, 7000000, 8000000]
    copies = sorted(c["layer"] for c in ed.clips() if c["id"] not in (a, b))
    assert copies == [6000000, 8000000]
    assert ed.undo_steps_since_mark() == 1


def test_move_to_a_track_past_the_top_adds_it_in_one_undo_step(ed):
    a = _logo_and_clip(ed)
    ed.mark()
    out = ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="7")
    head, _, body = out.partition("\n")
    assert "to track 7 on a new track (2 tracks added on top)" in head, out
    assert json.loads(body)["new_tracks"] == 2
    assert _numbers(ed)[5:] == [6000000, 7000000] and ed.clip(a)["layer"] == 7000000
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert len(_numbers(ed)) == 5 and ed.clip(a)["layer"] == 3000000


@pytest.mark.parametrize("tool", ["duplicate_clips_tool", "move_clips_tool"])
def test_four_past_the_top_is_refused_without_history(ed, tool):
    a = _logo_and_clip(ed)
    ed.mark()
    out = ed.call(tool, timeline_clip_ids=[a], to_track="9")
    assert out.startswith("Error: Unknown track or layer '9'"), out
    assert ed.undo_steps_since_mark() == 0 and len(_numbers(ed)) == 5


def test_move_to_an_existing_track_adds_none(ed):
    a = _logo_and_clip(ed)
    out = ed.call("move_clips_tool", timeline_clip_ids=[a], to_track="4")
    assert "new track" not in out and len(_numbers(ed)) == 5


def test_text_card_is_one_centred_line_on_a_full_frame_rect():
    from classes import title_svg
    doc = title_svg.load(title_svg.find_template("Text_Card"))
    slots = title_svg.fields(doc)
    w, h = title_svg.artboard_size(doc)
    assert [s.text for s in slots] == ["The Title"] and title_svg.text_zone(slots, h) == "center"
    assert title_svg.background_color(doc) == ("#000000", 1.0)


def test_text_on_a_translucent_card_keeps_its_opacity(tt, tmp_path):
    from classes import title_svg
    card = tmp_path / "Glass_Card.svg"
    card.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><g>'
                    '<rect style="opacity:0.5;fill:#112233" width="1920" height="1080" x="0" y="0"/></g></svg>')
    rec = receipt(tt.call("add_title_tool", template=str(card), text="Hi"))
    assert rec["template"] == "Text_Card" and rec["background_color"] == "#112233"
    assert title_svg.background_color(title_svg.load(rec["path"])) == ("#112233", 0.502)
    assert rec["warnings"] == ["Glass_Card has no text; drew the text with Text_Card over the colour #11223380"]


@pytest.mark.parametrize("tool, needle", [
    ("move_clips_tool", "Moved 2 clip(s) to track 5 at 0.00 s; 2 tracks added on top."),
    ("duplicate_clips_tool", "on track 5; 2 tracks added on top."),
])
def test_a_lower_source_landing_on_an_existing_track_does_not_call_it_new(ed, tool, needle):
    a = _logo_and_clip(ed, 1000000)
    b = _logo_and_clip(ed, 3000000)
    ed.mark()
    out = ed.call(tool, timeline_clip_ids=[a, b], to_track="7")
    head = out.partition("\n")[0]
    assert needle in head and "new track" not in head, out
    assert _numbers(ed)[5:] == [6000000, 7000000]
    assert ed.undo_steps_since_mark() == 1


def test_text_on_a_card_stays_vertically_centred_when_the_font_is_scaled(tt):
    from classes import title_svg
    rec = receipt(tt.call("add_title_tool", template="Solid_Color", text="DOUBT", font_scale=2.5))
    doc = title_svg.load(rec["path"])
    line = title_svg.line_nodes(doc)[0]
    size = float(line.getAttribute("style").split("font-size:")[1].split("px")[0])
    assert size == 400 and abs(float(line.getAttribute("y")) - (540 + 0.36 * size)) < 1e-6
    assert float(line.parentNode.getAttribute("y")) == float(line.getAttribute("y"))
