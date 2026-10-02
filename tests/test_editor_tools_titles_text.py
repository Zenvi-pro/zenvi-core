"""Titles: classes.title_svg (the Title Editor's rules) and list/add/edit_title_tool."""

import os

import pytest

from classes import title_svg
from titles_text_fakes import receipt, tt  # noqa: F401  (fixture)


# ---------------------------------------------------------------------------
# title_svg: fields, text, font, colours, names
# ---------------------------------------------------------------------------

def _doc(name):
    return title_svg.load(title_svg.find_template(name))


def test_every_bundled_template_parses_and_names_resolve():
    paths = [p for p in title_svg.template_paths() if p.endswith(".svg")]
    assert len(paths) == 50
    for p in paths:
        doc = title_svg.load(p)
        title_svg.fields(doc)
        title_svg.artboard_size(doc)
    assert title_svg.template_display_name(title_svg.find_template("Gray_Box_4")) == "Gray box 4"
    assert title_svg.find_template("gray box 4") == title_svg.find_template("Gray_Box_4.svg")
    assert title_svg.find_template("nope") is None
    assert "Standard_1" in title_svg.suggest_templates("standrd_1")


def test_reflection_copies_share_one_field_and_roles_follow_font_size():
    bar = title_svg.fields(_doc("Bar_1"))
    assert len(bar) == 1 and len(bar[0].nodes) == 2 and bar[0].role == "title"
    std = title_svg.fields(_doc("Standard_1"))
    assert [(f.role, f.text) for f in std] == [("title", "The Title"), ("subtitle", "Sub-Title")]
    ribbon = title_svg.fields(_doc("Ribbon_1"))          # subtitle comes first in the document
    assert [f.role for f in ribbon] == ["subtitle", "title"]
    assert [f.text for f in title_svg.fields(_doc("Standard_3"))] == ["Line 1", "Line 2", "Line 3"]
    assert title_svg.fields(_doc("Solid_Color")) == []


def test_blanked_field_keeps_a_line_for_the_title_editor():
    doc = _doc("Standard_1")
    slots = title_svg.fields(doc)
    title_svg.set_field_text(doc, slots[1], "")
    assert title_svg.line_texts(doc) == ["The Title", title_svg.BLANK]
    title_svg.set_field_text(doc, slots[0], "Two\nlines")
    assert title_svg.line_texts(doc)[0] == "Two lines"


def test_font_is_written_to_style_and_attribute_sizes_scale():
    doc = _doc("Box")                       # font-size lives in an attribute, not style=""
    nodes = title_svg.text_nodes(doc) + title_svg.tspan_nodes(doc)
    title_svg.apply_font(nodes, family="Georgia", bold=True, italic=False, font_size_ratio=0.5)
    style = title_svg.node_style(title_svg.text_nodes(doc)[0])
    assert style["font-family"] == "'Georgia'" and style["font-weight"] == "bold"
    assert style["font-size"] == "60px"     # 120px attribute * 0.5
    std = _doc("Standard_1")
    title_svg.apply_font(title_svg.tspan_nodes(std), font_size_ratio=2.0)
    assert title_svg.node_style(title_svg.tspan_nodes(std)[0])["font-size"].startswith("262.5")


def test_text_opacity_is_applied_once_not_squared():
    doc = _doc("Standard_1")
    title_svg.set_text_color(title_svg.text_nodes(doc), title_svg.tspan_nodes(doc), "#ff0000", 0.5)
    for text in title_svg.text_nodes(doc):
        assert title_svg.node_style(text)["opacity"] == "1"
    for tspan in title_svg.tspan_nodes(doc):
        assert title_svg.node_style(tspan)["fill"] == "#ff0000"
        assert title_svg.node_style(tspan)["opacity"] == "0.5"
    assert title_svg.text_color(doc) == ("#ff0000", 0.5)


def test_background_and_colour_read_back():
    doc = _doc("Standard_1")
    assert title_svg.background_color(doc)[1] == 0.0          # transparent full-frame rect
    title_svg.set_background(title_svg.rect_nodes(doc), "#000000", 0.75)
    assert title_svg.background_color(doc) == ("#000000", 0.75)
    fill, _ = title_svg.text_color(_doc("Box"))
    assert fill.lower() == "#0fffff"                           # presentation attribute


def test_names_follow_the_duplicate_rule(tmp_path):
    folder = str(tmp_path)
    assert title_svg.unique_title_path(folder, "Tokyo: Day 1") == os.path.join(folder, "Tokyo Day 1.svg")
    open(os.path.join(folder, "Tokyo Day 1.svg"), "w").close()
    assert title_svg.unique_title_path(folder, "Tokyo Day 1").endswith("Tokyo Day 1 (1).svg")
    assert title_svg.duplicate_pattern("Title (3).svg") == ("Title (%d)", 3)
    assert title_svg.duplicate_pattern("Title.svg") == ("Title (%d)", 0)
    assert title_svg.free_name("TitleFileName (%d)", 0, folder) == "TitleFileName (1)"


def test_fit_shrinks_text_that_would_run_off_the_artboard():
    doc = _doc("Standard_1")
    slot = title_svg.fields(doc)[0]
    title_svg.set_field_text(doc, slot, "An extremely long title that cannot possibly fit on one line")
    ratio = title_svg.fit_field(slot, 1920.0)
    assert 0.35 <= ratio < 0.8
    assert title_svg.fit_field(title_svg.fields(_doc("Standard_1"))[0], 1920.0) == 1.0


def test_write_is_staged_and_leaves_no_partial_files(tmp_path):
    doc = _doc("Standard_1")
    path = title_svg.write(doc, str(tmp_path / "t.svg"))
    assert os.path.exists(path) and os.listdir(tmp_path) == ["t.svg"]
    assert title_svg.line_texts(title_svg.load(path)) == ["The Title", "Sub-Title"]


# ---------------------------------------------------------------------------
# list_title_templates_tool
# ---------------------------------------------------------------------------

def test_list_static_templates_with_fields_and_query(tt):
    out = receipt(tt.call("list_title_templates_tool"))
    assert len(out["templates"]) == 50
    gray = next(t for t in out["templates"] if t["name"] == "Gray_Box_4")
    assert gray["zone"] == "bottom" and [f["role"] for f in gray["fields"]] == ["title", "subtitle"]
    lower = receipt(tt.call("list_title_templates_tool", query="lower third"))
    assert {t["name"] for t in lower["templates"]} == {"Bar_3", "Gray_Box_3", "Gray_Box_4"}


def test_list_animated_reports_blender_missing(tt):
    tt.settings["blender_command"] = "/definitely/not/blender"
    out = receipt(tt.call("list_title_templates_tool", kind="animated"))
    assert len(out["animated"]) == 19
    fly = next(a for a in out["animated"] if a["name"] == "fly_by_1")
    assert fly["title"] == "Fly Towards Camera" and any(p["name"] == "title" for p in fly["params"])
    assert not any(p["name"] in ("start_frame", "end_frame", "file_name") for p in fly["params"])
    assert out["blender"]["found"] is False and "Blender Command" in out["blender"]["how_to_enable"]


def test_list_is_read_only(tt):
    tt.mark()
    tt.call("list_title_templates_tool", kind="all")
    assert tt.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# add_title_tool
# ---------------------------------------------------------------------------

def _title_file(tt, rec):
    return tt.file(rec["file_id"])


def test_add_title_on_empty_timeline(tt):
    out = tt.call("add_title_tool", text="Tokyo Day 1")
    rec = receipt(out)
    assert out.startswith("Added title 'Tokyo Day 1' (Bar_1) on track 1 at 0.00-5.00s")
    clip = tt.clip(rec["timeline_clip_id"])
    assert clip["layer"] == 1000000 and clip["position"] == 0.0 and clip["end"] == 5.0
    assert rec["path"] == os.path.join(str(tt.tmp_path / "title"), "Tokyo Day 1.svg")
    assert title_svg.line_texts(title_svg.load(rec["path"])) == ["Tokyo Day 1", "Tokyo Day 1"]
    assert _title_file(tt, rec)["path"] == rec["path"]
    calls = tt.window.files_model.calls
    assert calls[-1]["skip_indexing"] is True and calls[-1]["quiet"] is True
    assert tt.window.timeline.add_calls[-1]["call_manual_move"] is False
    assert tt.undo_steps_since_mark() == 1


@pytest.mark.parametrize("template", ["Bar_1", "Oval_4", "Smoke_3", "Standard_2"])
def test_reflection_templates_show_the_title_on_the_visible_line_too(tt, template):
    """#249's case: the first tspan is the mirrored copy; the visible line must carry the text too."""
    rec = receipt(tt.call("add_title_tool", text="HELLO", template=template))
    assert title_svg.line_texts(title_svg.load(rec["path"]))[:2] == ["HELLO", "HELLO"]


def test_title_goes_above_the_video_it_overlaps(tt):
    video = tt.add_file("video", duration=20.0)
    tt.add_clip(video, position=0.0, layer=1000000)
    tt.add_clip(video, position=0.0, layer=2000000, end=8.0)
    rec = receipt(tt.call("add_title_tool", text="Hello", template="Standard_1", position_seconds=2.0))
    assert tt.clip(rec["timeline_clip_id"])["layer"] == 3000000 and rec["track"] == 3
    # after the upper video ends, the first free track above the remaining video is track 2
    rec2 = receipt(tt.call("add_title_tool", text="Later", position_seconds=12.0))
    assert tt.clip(rec2["timeline_clip_id"])["layer"] == 2000000


def test_new_top_track_when_every_track_is_busy_and_one_undo_removes_all(tt):
    video = tt.add_file("video", duration=30.0)
    for n in range(1, 6):
        tt.add_clip(video, position=0.0, layer=n * 1000000)
    before_layers = len(tt.get("layers"))
    rec = receipt(tt.call("add_title_tool", text="Top", template="Standard_1"))
    assert rec["new_track"] is True and rec["layer"] == 6000000
    assert any(t["number"] == 6000000 and t["label"] == "Titles" for t in tt.get("layers"))
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.clip(rec["timeline_clip_id"]) is None and tt.file(rec["file_id"]) is None
    assert len(tt.get("layers")) == before_layers
    tt.redo()
    assert tt.clip(rec["timeline_clip_id"]) is not None and tt.file(rec["file_id"]) is not None


def test_explicit_track_locked_or_busy_is_refused_without_history_or_files(tt):
    video = tt.add_file("video", duration=10.0)
    tt.add_clip(video, position=0.0, layer=2000000)
    tt.lock_track(3000000)
    tt.mark()
    out = tt.call("add_title_tool", text="X", track="3")
    assert out.startswith("Error") and "locked" in out
    out = tt.call("add_title_tool", text="X", track="2", position_seconds=5)
    assert out.startswith("Error") and "already has" in out
    assert tt.undo_steps_since_mark() == 0
    assert not os.path.isdir(tt.tmp_path / "title") or os.listdir(tt.tmp_path / "title") == []
    rec = receipt(tt.call("add_title_tool", text="Y", track="2", position_seconds=10.5))
    assert tt.clip(rec["timeline_clip_id"])["layer"] == 2000000


def test_lower_third_subtitle_and_newline_forms(tt):
    rec = receipt(tt.call("add_title_tool", text="Jane Doe", subtitle="Founder", template="Gray_Box_4"))
    assert [f["text"] for f in rec["fields"]] == ["Jane Doe", "Founder"]
    assert rec["screen_position"] == "bottom_center"
    assert tt.clip(rec["timeline_clip_id"])["gravity"] == 7
    rec = receipt(tt.call("add_title_tool", text="Jane Doe\nFounder", template="Ribbon_1"))
    by_role = {f["role"]: f["text"] for f in rec["fields"]}
    assert by_role == {"title": "Jane Doe", "subtitle": "Founder"}


def test_unfilled_placeholders_are_blanked(tt):
    rec = receipt(tt.call("add_title_tool", text="Only a title", template="Standard_1"))
    assert title_svg.line_texts(title_svg.load(rec["path"])) == ["Only a title", title_svg.BLANK]


def test_lines_fill_multi_line_templates_and_too_many_are_refused(tt):
    rec = receipt(tt.call("add_title_tool", template="Standard_4", lines=["Director", "Jane", "Music", "Bob"]))
    assert [f["text"] for f in rec["fields"]] == ["Director", "Jane", "Music", "Bob"]
    tt.mark()
    out = tt.call("add_title_tool", template="Standard_1", lines=["a", "b", "c"])
    assert out.startswith("Error") and "Standard_3" in out
    out = tt.call("add_title_tool", template="Gold_1", text="A", subtitle="B")
    assert out.startswith("Error") and "single text line" in out
    assert tt.undo_steps_since_mark() == 0


@pytest.mark.parametrize("args, needle", [
    ({"text": ""}, "needs text"),
    ({"text": "A", "template": "Nopey"}, "unknown title template"),
    ({"text": "A", "text_color": "not-a-colour"}, "text_color"),
    ({"text": "A", "background_color": "#12"}, "background_color"),
    ({"text": "A", "font_weight": "heavy"}, "font_weight"),
    ({"text": "A", "duration_seconds": 0}, "duration_seconds"),
    ({"text": "A", "position_seconds": -1}, "position_seconds"),
    ({"text": "A", "template": "Solid_Color"}, "no text"),
])
def test_add_title_refusals_leave_history_untouched(tt, args, needle):
    tt.mark()
    out = tt.call("add_title_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert tt.undo_steps_since_mark() == 0


def test_colour_card_without_text(tt):
    rec = receipt(tt.call("add_title_tool", template="Solid_Color", background_color="#1e90ff"))
    assert title_svg.background_color(title_svg.load(rec["path"])) == ("#1e90ff", 1.0)


def test_style_fades_and_font(tt):
    rec = receipt(tt.call("add_title_tool", text="Styled", template="Standard_1", text_color="#FFFFFF80",
                          background_color="black", font="Georgia", font_weight="bold", font_scale=1.25,
                          fade_in_seconds=0.5, fade_out_seconds=1.0, duration_seconds=4.0))
    doc = title_svg.load(rec["path"])
    assert title_svg.text_color(doc) == ("#ffffff", 0.502)
    assert title_svg.node_style(title_svg.tspan_nodes(doc)[0])["font-family"] == "'Georgia'"
    points = tt.clip(rec["timeline_clip_id"])["alpha"]["Points"]
    assert [(p["co"]["X"], p["co"]["Y"]) for p in points] == [(1.0, 0.0), (16.0, 1.0), (91.0, 1.0), (121.0, 0.0)]


def test_long_text_is_shrunk_to_fit(tt):
    rec = receipt(tt.call("add_title_tool", template="Standard_1",
                          text="Our whole summer trip through Japan, Korea and Taiwan"))
    assert "1" in rec["text_shrunk_to_fit"]
    rec = receipt(tt.call("add_title_tool", template="Standard_1", fit_text=False,
                          text="Our whole summer trip through Japan, Korea and Taiwan"))
    assert "text_shrunk_to_fit" not in rec


def test_existing_file_name_gets_a_number_not_overwritten(tt):
    a = receipt(tt.call("add_title_tool", text="Intro", file_name="Opening"))
    b = receipt(tt.call("add_title_tool", text="Intro 2", file_name="Opening", position_seconds=6))
    assert os.path.basename(a["path"]) == "Opening.svg" and os.path.basename(b["path"]) == "Opening (1).svg"
    assert title_svg.line_texts(title_svg.load(a["path"]))[0] == "Intro"


def test_pr183_arguments_still_work(tt):
    out = tt.call("add_title_tool", text="Lower third", template="Footer_1", position_seconds="3",
                  track="", duration_seconds="2.5", file_name="footer")
    rec = receipt(out)
    assert rec["template"] == "Footer_1" and rec["position"] == 3.0 and rec["duration"] == 2.5
    assert os.path.basename(rec["path"]) == "footer.svg"


def test_vertical_project_anchors_by_template_zone(tt):
    tt.store._data["width"], tt.store._data["height"] = 1080, 1920
    header = receipt(tt.call("add_title_tool", text="Top", template="Header_2"))
    assert tt.clip(header["timeline_clip_id"])["gravity"] == 1
    centre = receipt(tt.call("add_title_tool", text="Mid", template="Standard_1", position_seconds=6))
    assert tt.clip(centre["timeline_clip_id"])["gravity"] == 4
    forced = receipt(tt.call("add_title_tool", text="Low", template="Standard_1", position_seconds=12,
                             screen_position="bottom"))
    assert tt.clip(forced["timeline_clip_id"])["gravity"] == 7
    # found live: a 9:16 lower third sat in the band Reels/TikTok cover with their UI -> lifted
    assert forced["lifted_for_vertical_ui"] == -0.12
    assert tt.clip(forced["timeline_clip_id"])["location_y"]["Points"][0]["co"]["Y"] == -0.12
    assert "lifted_for_vertical_ui" not in centre


def test_template_can_be_an_existing_title_path(tt):
    first = receipt(tt.call("add_title_tool", text="Base", template="Gray_Box_4", subtitle="Role"))
    rec = receipt(tt.call("add_title_tool", template=first["path"], text="Second", position_seconds=6))
    assert [f["text"] for f in rec["fields"]] == ["Second", ""]


# ---------------------------------------------------------------------------
# edit_title_tool
# ---------------------------------------------------------------------------

def _two_clips_of_one_title(tt):
    rec = receipt(tt.call("add_title_tool", text="Day 1", template="Standard_1", subtitle="Tokyo"))
    from classes.query import Clip
    c = Clip.get(id=rec["timeline_clip_id"])
    second = dict(c.data)
    second.pop("id")
    second["position"] = 10.0
    new = Clip()
    new.data = second
    new.save()
    tt.mark()
    return rec, new.id


def test_edit_all_clips_writes_a_new_version_and_undo_restores_it(tt):
    rec, other = _two_clips_of_one_title(tt)
    old_path = rec["path"]
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 2", text_color="yellow")
    edit = receipt(out)
    assert out.startswith("Updated title 'Day 2 / Tokyo' on 2 clip(s)")
    assert edit["path"].endswith("Day 1 v2.svg") and edit["path"] != old_path
    assert tt.file(rec["file_id"])["path"] == edit["path"]
    assert tt.file(rec["file_id"])["name"] == "Day 1.svg"
    for cid in (rec["timeline_clip_id"], other):
        assert tt.clip(cid)["reader"]["path"] == edit["path"]
    assert title_svg.line_texts(title_svg.load(old_path))[0] == "Day 1"        # old version untouched
    assert title_svg.text_color(title_svg.load(edit["path"]))[0] == "#ffff00"
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.file(rec["file_id"])["path"] == old_path
    assert tt.clip(other)["reader"]["path"] == old_path
    tt.redo()
    assert tt.clip(other)["reader"]["path"] == edit["path"]


def test_edit_this_clip_gives_it_its_own_copy(tt):
    rec, other = _two_clips_of_one_title(tt)
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=other, scope="this_clip", subtitle="Kyoto"))
    assert edit["scope"] == "this_clip" and edit["file_id"] != rec["file_id"]
    assert tt.clip(other)["file_id"] == edit["file_id"]
    assert tt.clip(rec["timeline_clip_id"])["file_id"] == rec["file_id"]
    assert [f["text"] for f in edit["fields"]] == ["Day 1", "Kyoto"]
    assert tt.undo_steps_since_mark() == 1


def test_duplicate_adds_a_file_and_touches_no_clip(tt):
    rec, other = _two_clips_of_one_title(tt)
    edit = receipt(tt.call("edit_title_tool", file_id=rec["file_id"], scope="duplicate"))
    assert os.path.basename(edit["path"]) == "Day 1 (1).svg" and edit["timeline_clip_ids"] == []
    assert tt.clip(other)["file_id"] == rec["file_id"]


def test_edit_by_query_and_refusals(tt):
    rec, _other = _two_clips_of_one_title(tt)
    receipt(tt.call("add_title_tool", text="Credits", template="Standard_1", position_seconds=20))
    tt.mark()
    assert receipt(tt.call("edit_title_tool", title_query="tokyo", text="Day 9"))["source_file_id"] == rec["file_id"]
    tt.mark()
    for args, needle in [({"title_query": "Day 9"}, "nothing to change"),
                         ({"title_query": "Day 9", "text": "Day 9"}, "already looks like that"),
                         ({"title_query": "nothing like it", "text": "x"}, "no title in the project"),
                         ({"text": "x"}, "which title"),
                         ({"file_id": rec["file_id"], "scope": "this_clip", "text": "x"}, "needs timeline_clip_id")]:
        out = tt.call("edit_title_tool", **args)
        assert out.startswith("Error") and needle in out, (args, out)
    assert tt.undo_steps_since_mark() == 0


def test_edit_refuses_non_titles_and_locked_tracks(tt):
    video = tt.add_file("video")
    vclip = tt.add_clip(video)
    out = tt.call("edit_title_tool", timeline_clip_id=vclip, text="x")
    assert out.startswith("Error") and "not a title" in out
    rec = receipt(tt.call("add_title_tool", text="Locked", template="Standard_1", track="3"))
    tt.lock_track(3000000)
    tt.mark()
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="y")
    assert out.startswith("Error") and "locked" in out
    assert tt.undo_steps_since_mark() == 0


def test_project_listing_shows_titles_and_their_clips(tt):
    rec, other = _two_clips_of_one_title(tt)
    out = receipt(tt.call("list_title_templates_tool", kind="project"))
    [title] = out["project_titles"]
    assert title["file_id"] == rec["file_id"] and {c["timeline_clip_id"] for c in title["clips"]} == {
        rec["timeline_clip_id"], other}
    assert [f["text"] for f in title["fields"]] == ["Day 1", "Tokyo"]


# ---------------------------------------------------------------------------
# Found live: a busy GUI thread, and template fonts that are not installed
# ---------------------------------------------------------------------------

def test_a_commit_timeout_keeps_the_svg_and_says_the_title_may_still_appear(tt, monkeypatch):
    from classes import tool_handlers
    from classes.editor_tools import titles_text_common

    def slow_on_main(func, *args, timeout=None):
        if timeout == titles_text_common.COMMIT_TIMEOUT:
            raise tool_handlers.MainThreadStillRunning(
                "MAIN_THREAD_STILL_RUNNING: this call started on the Qt GUI thread but was still running", "job1")
        return func(*args)

    monkeypatch.setattr(titles_text_common, "on_main", slow_on_main)
    out = tt.call("add_title_tool", text="Busy editor", template="Standard_1")
    assert out.startswith("Error") and "too busy" in out and "get_timeline_state_tool" in out
    # the running work may still land, so the SVG it imports must still be there
    assert os.path.exists(str(tt.tmp_path / "title" / "Busy editor.svg"))


def test_a_commit_that_never_started_cleans_up_and_says_nothing_changed(tt, monkeypatch):
    from classes import tool_handlers
    from classes.editor_tools import titles_text_common

    def cancelled_on_main(func, *args, timeout=None):
        if timeout == titles_text_common.COMMIT_TIMEOUT:
            raise tool_handlers.MainThreadTimeout(
                "MAIN_THREAD_TIMEOUT: the Qt GUI thread did not respond within 240s. The call was withdrawn "
                "before it started and will not run later.")
        return func(*args)

    monkeypatch.setattr(titles_text_common, "on_main", cancelled_on_main)
    tt.mark()
    out = tt.call("add_title_tool", text="Never started", template="Standard_1")
    assert out.startswith("Error") and "nothing was changed" in out
    assert not os.path.exists(str(tt.tmp_path / "title" / "Never started.svg"))
    assert tt.undo_steps_since_mark() == 0


def test_a_missing_template_font_becomes_the_title_editors_fallback(tt, monkeypatch):
    from classes.editor_tools import titles_text
    monkeypatch.setattr(titles_text, "installed_font_families", lambda: ["Arial", "Arial Black", "Helvetica"])
    rec = receipt(tt.call("add_title_tool", text="Fonts", template="Standard_1"))
    assert rec["font"] == "Arial" and rec["font_replaced"] == "DejaVu Sans"
    doc = title_svg.load(rec["path"])
    assert title_svg.node_style(title_svg.tspan_nodes(doc)[0])["font-family"] == "'Arial'"
    # an installed template font is left alone
    monkeypatch.setattr(titles_text, "installed_font_families", lambda: ["DejaVu Sans", "Arial"])
    rec = receipt(tt.call("add_title_tool", text="Kept", template="Standard_1", position_seconds=6))
    assert "font_replaced" not in rec
    assert title_svg.editor_font_family("DejaVu Sans", ["Arial"]) == "Arial"
    # macOS ships many script-specific "Noto Sans X" families: an exact fallback name wins
    assert title_svg.editor_font_family("DejaVu Sans", ["Noto Sans Armenian", "Arial Black", "Arial"]) == "Arial"
    assert title_svg.editor_font_family("DejaVu Sans", ["DejaVu Sans Mono", "Arial"]) == "DejaVu Sans Mono"
    assert title_svg.editor_font_family("Nope", []) == ""


def test_undo_of_a_title_edit_is_reported_as_a_change(tt):
    rec = receipt(tt.call("add_title_tool", text="Day 1", template="Standard_1"))
    tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 2")
    out = tt.call("undo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert tt.clip(rec["timeline_clip_id"])["reader"]["path"] == rec["path"]
    out = tt.call("redo_tool", steps=1)
    assert not out.startswith("Error"), out
