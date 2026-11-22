"""Titles: classes.title_svg (the Title Editor's rules), HyperFrames add_title_tool and list/edit_title_tool."""

import os

import pytest

from classes import hyperframes_titles as ht
from classes import title_svg
from titles_text_fakes import hft, receipt, tt  # noqa: F401  (fixtures)


# ---------------------------------------------------------------------------
# title_svg: fields, text, font, colours, names
# ---------------------------------------------------------------------------

def _doc(name):
    return title_svg.load(title_svg.find_template(name))


def test_every_bundled_template_parses_and_names_resolve():
    paths = [p for p in title_svg.template_paths() if p.endswith(".svg")]
    assert len(paths) == 51
    for p in paths:
        doc = title_svg.load(p)
        title_svg.fields(doc)
        title_svg.artboard_size(doc)
    assert title_svg.template_display_name(title_svg.find_template("Gray_Box_4")) == "Gray box 4"
    assert title_svg.find_template("gray box 4") == title_svg.find_template("Gray_Box_4.svg")
    assert title_svg.find_template("nope") is None


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
# A fake HyperFrames CLI, and SVG titles made the Title Editor's way
# ---------------------------------------------------------------------------

def _svg_title(tt, title, subtitle="", position=0.0, layer=1000000):
    """An SVG title as the Title Editor saves one (Standard_1), placed on the timeline."""
    from classes.editor_tools import titles_text_common as common
    doc = _doc("Standard_1")
    main, sub = title_svg.fields(doc)[:2]
    title_svg.set_field_text(doc, main, title)
    title_svg.set_field_text(doc, sub, subtitle)
    path = title_svg.write(doc, os.path.join(common.title_dir(), title + ".svg"))
    f = common.import_media_file(path)
    clip = common.place_clip(f.id, position, 5.0, layer)
    return {"file_id": f.id, "timeline_clip_id": clip["id"], "path": path}


def _second_clip(tt, clip_id, position=10.0):
    from classes.query import Clip
    data = dict(Clip.get(id=clip_id).data)
    data.pop("id")
    data["position"] = position
    new = Clip()
    new.data = data
    new.save()
    return new.id


# ---------------------------------------------------------------------------
# list_title_templates_tool
# ---------------------------------------------------------------------------

def test_list_title_styles_with_fields_and_query(tt):
    out = receipt(tt.call("list_title_templates_tool"))
    assert [s["style"] for s in out["styles"]] == list(ht.STYLES)
    lower = next(s for s in out["styles"] if s["style"] == "lower_third")
    assert lower["zone"] == "bottom" and lower["fields"] == ["title", "subtitle"] and lower["look"]
    assert [s["style"] for s in receipt(tt.call("list_title_templates_tool", query="lower third"))["styles"]] == [
        "lower_third"]
    assert "templates" not in out                       # the OpenShot SVG templates are not offered any more


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
# add_title_tool: a HyperFrames title, rendered here and placed over the video
# ---------------------------------------------------------------------------

def test_add_title_renders_a_hyperframes_title_above_the_video(tt, hft):
    video = tt.add_file("video", duration=20.0)
    tt.add_clip(video, position=0.0, layer=1000000)
    tt.mark()
    out = tt.call("add_title_tool", text="Tokyo Day 1", subtitle="Shibuya", position_seconds=2.0)
    rec = receipt(out)
    assert out.startswith("Added title 'Tokyo Day 1 / Shibuya' (Headline) on track 2 at 2.00-7.00s")
    program, args = hft.renders[-1]
    assert program == "hyperframes" and args[0] == "render" and args[args.index("--format") + 1] == "webm"
    html = hft.html[-1]
    assert "Tokyo" in html and 'data-width="1920"' in html and 'data-duration="5"' in html
    assert hft.reencodes == [(1920, 1080)]                         # the alpha pass at the project's size
    assert rec["path"] == os.path.join(str(tt.tmp_path / "title"), "Tokyo Day 1.mov") and os.path.isfile(rec["path"])
    assert not [n for n in os.listdir(tt.tmp_path / "title") if n.endswith(".svg")]
    assert hft.projects() == []                                    # the scratch render folder is gone
    f = tt.file(rec["file_id"])
    assert f["path"] == rec["path"] and f["ai_metadata"]["source"] == ht.SOURCE
    assert f["ai_metadata"]["title"]["style"] == "headline" and f["ai_metadata"]["title"]["subtitle"] == "Shibuya"
    clip = tt.clip(rec["timeline_clip_id"])
    assert clip["layer"] == 2000000 and clip["position"] == 2.0 and clip["end"] - clip["start"] == 5.0
    assert rec["style"] == "headline" and rec["fields"] == {"title": "Tokyo Day 1", "subtitle": "Shibuya"}
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.file(rec["file_id"]) is None


def test_end_card_is_rendered_opaque_and_skips_the_alpha_pass(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Thanks for watching", subtitle="Subscribe", style="end_card"))
    _program, args = hft.renders[-1]
    assert args[args.index("--format") + 1] == "mp4" and hft.reencodes == []
    assert rec["path"].endswith("Thanks for watching.mp4") and rec["opaque"] is True


def test_lower_third_newline_colours_and_font(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Jane Doe\nFounder, Acme", style="lower_third",
                          text_color="yellow", accent_color="#FF0000", font="Georgia", font_scale=1.5))
    spec = tt.file(rec["file_id"])["ai_metadata"]["title"]
    assert (spec["title"], spec["subtitle"], spec["text_color"], spec["accent_color"], spec["font"]) == (
        "Jane Doe", "Founder, Acme", "#ffff00", "#ff0000", "Georgia")
    assert "Founder, Acme" in hft.html[-1] and '"Georgia"' in hft.html[-1]
    assert spec["zone"] == "bottom" and rec["screen_position"] == "bottom"


def test_vertical_project_renders_at_its_own_size(tt, hft):
    tt.store._data["width"], tt.store._data["height"] = 1080, 1920
    receipt(tt.call("add_title_tool", text="Top", style="kicker"))
    assert 'data-width="1080"' in hft.html[-1] and 'data-height="1920"' in hft.html[-1]
    assert hft.reencodes == [(1080, 1920)]


def test_colour_card_without_text(tt, hft):
    rec = receipt(tt.call("add_title_tool", background_color="#ffffff", duration_seconds=0.1))
    assert rec["path"].endswith(".mp4") and rec["opaque"] is True
    assert tt.file(rec["file_id"])["ai_metadata"]["title"]["background_color"] == "#ffffff"


def test_names_are_never_overwritten(tt, hft):
    a = receipt(tt.call("add_title_tool", text="Intro", file_name="Opening"))
    b = receipt(tt.call("add_title_tool", text="Intro 2", file_name="Opening", position_seconds=6))
    assert os.path.basename(a["path"]) == "Opening.mov" and os.path.basename(b["path"]) == "Opening (1).mov"


@pytest.mark.parametrize("args, needle", [
    ({}, "needs text"),
    ({"text": "x", "text_color": "not-a-colour"}, "text_color"),
    ({"text": "x", "font": "Bad;font{}"}, "font must be"),
    ({"text": "x", "track": "3"}, "locked"),
])
def test_add_title_refusals_render_nothing_and_leave_history_untouched(tt, hft, args, needle):
    tt.lock_track(3000000)
    tt.mark()
    out = tt.call("add_title_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert hft.renders == [] and tt.undo_steps_since_mark() == 0


def test_add_title_without_hyperframes_says_how_to_set_it_up(tt, hft, monkeypatch):
    from classes import hyperframes_local as hf
    monkeypatch.setattr(hf, "status", lambda: {"ready": False})
    tt.mark()
    out = tt.call("add_title_tool", text="Hello")
    assert out.startswith("Error") and "hyperframes_setup_tool" in out
    assert hft.renders == [] and tt.undo_steps_since_mark() == 0


def test_a_failed_render_changes_nothing(tt, hft):
    hft.fail = True
    tt.mark()
    out = tt.call("add_title_tool", text="Broken")
    assert out.startswith("Error") and "chrome crashed" in out
    assert tt.undo_steps_since_mark() == 0 and hft.projects() == []
    assert not os.path.isdir(tt.tmp_path / "title") or os.listdir(tt.tmp_path / "title") == []


def test_a_commit_timeout_keeps_the_render_and_says_the_title_may_still_appear(tt, hft, monkeypatch):
    from classes import tool_handlers
    from classes.editor_tools import titles_text_common

    def slow_on_main(func, *args, timeout=None):
        if timeout == titles_text_common.COMMIT_TIMEOUT:
            raise tool_handlers.MainThreadStillRunning(
                "MAIN_THREAD_STILL_RUNNING: this call started on the Qt GUI thread but was still running", "job1")
        return func(*args)

    monkeypatch.setattr(titles_text_common, "on_main", slow_on_main)
    out = tt.call("add_title_tool", text="Busy editor")
    assert out.startswith("Error") and "too busy" in out and "get_timeline_state_tool" in out
    # the running work may still land, so the video it imports must still be there
    assert os.path.exists(str(tt.tmp_path / "title" / "Busy editor.mov"))


def test_a_commit_that_never_started_cleans_up_and_says_nothing_changed(tt, hft, monkeypatch):
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
    out = tt.call("add_title_tool", text="Never started")
    assert out.startswith("Error") and "nothing was changed" in out
    assert not os.path.exists(str(tt.tmp_path / "title" / "Never started.mov"))
    assert tt.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# edit_title_tool: HyperFrames titles are rendered again; SVG titles are rewritten
# ---------------------------------------------------------------------------

def test_edit_a_hyperframes_title_renders_it_again_for_every_clip(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Day 1", subtitle="Tokyo"))
    other = _second_clip(tt, rec["timeline_clip_id"])
    tt.mark()
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 2", style="kicker")
    edit = receipt(out)
    assert out.startswith("Updated title 'Day 2 / Tokyo' on 2 clip(s)")
    assert edit["path"].endswith("Day 1 v2.mov") and len(hft.renders) == 2 and "Day 2" in hft.html[-1]
    assert 'data-duration="5"' in hft.html[-1]                    # the file keeps its length
    # The new render is a new Project Files entry with its own probe; every clip plays it, the old entry goes.
    f = tt.file(edit["file_id"])
    assert edit["file_id"] != rec["file_id"] and tt.file(rec["file_id"]) is None
    assert f["path"] == edit["path"] and f["ai_metadata"]["title"]["style"] == "kicker"
    assert f["ai_metadata"]["title"]["subtitle"] == "Tokyo"         # only what was passed changes
    for cid in (rec["timeline_clip_id"], other):
        assert tt.clip(cid)["file_id"] == edit["file_id"] and tt.clip(cid)["reader"]["path"] == edit["path"]
    assert os.path.isfile(rec["path"])                             # the old version is kept on disk
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.file(rec["file_id"])["path"] == rec["path"] and tt.clip(other)["reader"]["path"] == rec["path"]
    assert tt.file(rec["file_id"])["ai_metadata"]["title"]["style"] == "headline"


def test_edit_to_an_opaque_card_carries_the_new_files_probe(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Thanks"))
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], style="end_card"))
    assert edit["path"].endswith(".mp4")
    assert tt.clip(rec["timeline_clip_id"])["reader"] == {k: v for k, v in tt.file(edit["file_id"]).items()
                                                          if k in tt.clip(rec["timeline_clip_id"])["reader"]}


def test_font_scale_on_edit_is_kept_in_range(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Big", font_scale=3))
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], font_scale=3))
    assert tt.file(edit["file_id"])["ai_metadata"]["title"]["font_scale"] == 5.0


def test_a_track_locked_during_the_render_refuses_the_edit(tt, hft, monkeypatch):
    from classes import hyperframes_local as hf
    rec = receipt(tt.call("add_title_tool", text="Day 1"))
    layer = tt.clip(rec["timeline_clip_id"])["layer"]
    run = hf.run

    def locking_run(*args, **kw):
        tt.lock_track(layer)                     # the person locks the track while the render runs
        return run(*args, **kw)

    monkeypatch.setattr(hf, "run", locking_run)
    tt.mark()
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 2")
    assert out.startswith("Error") and "locked" in out
    assert tt.undo_steps_since_mark() == 0 and tt.clip(rec["timeline_clip_id"])["file_id"] == rec["file_id"]


def test_a_project_files_name_is_never_reused_even_when_its_media_is_gone(tt, hft):
    first = receipt(tt.call("add_title_tool", text="Intro"))
    os.remove(first["path"])                     # deleted on disk, still in Project Files
    second = receipt(tt.call("add_title_tool", text="Intro", position_seconds=6))
    assert second["path"] != first["path"] and second["file_id"] != first["file_id"]
    assert tt.file(first["file_id"])["ai_metadata"]["title"]["title"] == "Intro"


def test_edit_a_hyperframes_title_for_one_clip_and_refusals(tt, hft):
    rec = receipt(tt.call("add_title_tool", text="Day 1", subtitle="Tokyo"))
    other = _second_clip(tt, rec["timeline_clip_id"])
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=other, scope="this_clip", subtitle="Kyoto"))
    assert edit["scope"] == "this_clip" and edit["file_id"] != rec["file_id"]
    assert tt.clip(other)["file_id"] == edit["file_id"] and tt.clip(rec["timeline_clip_id"])["file_id"] == rec["file_id"]
    assert tt.file(edit["file_id"])["ai_metadata"]["title"]["subtitle"] == "Kyoto"
    renders = len(hft.renders)
    tt.mark()
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 1")
    assert out.startswith("Error") and "already looks like that" in out
    assert len(hft.renders) == renders and tt.undo_steps_since_mark() == 0


def _during_render(monkeypatch, action):
    """Run *action* once while the next title renders."""
    from classes import hyperframes_local as hf
    real, pending = hf.run, [action]

    def run(*args, **kwargs):
        if pending:
            pending.pop()()
        return real(*args, **kwargs)
    monkeypatch.setattr(hf, "run", run)


def test_two_titles_of_one_name_rendering_together_keep_their_own_files(tt, hft, monkeypatch):
    inner = []
    _during_render(monkeypatch, lambda: inner.append(
        receipt(tt.call("add_title_tool", text="Intro", file_name="same", position_seconds=10))))
    outer = receipt(tt.call("add_title_tool", text="Outro", file_name="same"))
    assert outer["path"] != inner[0]["path"]
    assert tt.file(inner[0]["file_id"])["path"] == inner[0]["path"]


def test_a_rendered_title_reports_its_length(tt, hft):
    from classes.editor_tools.media_files import describe_file
    from classes.query import File
    rec = receipt(tt.call("add_title_tool", text="Intro"))
    info = describe_file(File.get(id=rec["file_id"]), full=True)
    assert info["kind"] == "title" and info["duration"] and info["media_duration"]
    svg = _svg_title(tt, "Day 1", position=20.0)
    assert describe_file(File.get(id=svg["file_id"]), full=True)["duration"] is None


def test_this_clip_edit_refuses_a_clip_repointed_during_the_render(tt, hft, monkeypatch):
    from classes.query import Clip
    rec = receipt(tt.call("add_title_tool", text="Day 1"))
    other = _second_clip(tt, rec["timeline_clip_id"])
    f = tt.add_file("video", duration=30.0)

    def repoint():
        live = Clip.get(id=other)
        live.data = {"file_id": f}
        live.save()
    _during_render(monkeypatch, repoint)
    out = tt.call("edit_title_tool", timeline_clip_id=other, scope="this_clip", text="Day 2")
    assert out.startswith("Error") and "no longer uses this title" in out
    assert tt.clip(other)["file_id"] == f


def test_this_clip_edit_leaves_a_clip_added_during_the_render_alone(tt, hft, monkeypatch):
    rec = receipt(tt.call("add_title_tool", text="Day 1"))
    added = []
    _during_render(monkeypatch, lambda: added.append(_second_clip(tt, rec["timeline_clip_id"])))
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], scope="this_clip",
                           text="Day 2"))
    assert edit["timeline_clip_ids"] == [rec["timeline_clip_id"]] and edit["scope"] == "this_clip"
    assert tt.clip(added[0])["file_id"] == rec["file_id"]


def test_svg_title_edits_refuse_hyperframes_only_changes(tt):
    svg = _svg_title(tt, "Day 1", "Tokyo")
    out = tt.call("edit_title_tool", timeline_clip_id=svg["timeline_clip_id"], style="kicker")
    assert out.startswith("Error") and "SVG title" in out and "add_title_tool" in out


def test_edit_all_clips_writes_a_new_version_and_undo_restores_it(tt):
    rec = _svg_title(tt, "Day 1", "Tokyo")
    other = _second_clip(tt, rec["timeline_clip_id"])
    tt.mark()
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
    rec = _svg_title(tt, "Day 1", "Tokyo")
    other = _second_clip(tt, rec["timeline_clip_id"])
    tt.mark()
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=other, scope="this_clip", subtitle="Kyoto"))
    assert edit["scope"] == "this_clip" and edit["file_id"] != rec["file_id"]
    assert tt.clip(other)["file_id"] == edit["file_id"]
    assert tt.clip(rec["timeline_clip_id"])["file_id"] == rec["file_id"]
    assert [f["text"] for f in edit["fields"]] == ["Day 1", "Kyoto"]
    assert tt.undo_steps_since_mark() == 1


def test_duplicate_adds_a_file_and_touches_no_clip(tt):
    rec = _svg_title(tt, "Day 1", "Tokyo")
    other = _second_clip(tt, rec["timeline_clip_id"])
    edit = receipt(tt.call("edit_title_tool", file_id=rec["file_id"], scope="duplicate"))
    assert os.path.basename(edit["path"]) == "Day 1 (1).svg" and edit["timeline_clip_ids"] == []
    assert tt.clip(other)["file_id"] == rec["file_id"]


def test_edit_by_query_and_refusals(tt):
    rec = _svg_title(tt, "Day 1", "Tokyo")
    _svg_title(tt, "Credits", position=20.0)
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
    rec = _svg_title(tt, "Locked", layer=3000000)
    tt.lock_track(3000000)
    tt.mark()
    out = tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="y")
    assert out.startswith("Error") and "locked" in out
    assert tt.undo_steps_since_mark() == 0


def test_project_listing_shows_both_kinds_of_title_and_their_clips(tt, hft):
    rec = _svg_title(tt, "Day 1", "Tokyo")
    other = _second_clip(tt, rec["timeline_clip_id"])
    hf_rec = receipt(tt.call("add_title_tool", text="Jane", subtitle="Host", style="lower_third", position_seconds=30))
    titles = {t["file_id"]: t for t in receipt(tt.call("list_title_templates_tool", kind="project"))["project_titles"]}
    svg = titles[rec["file_id"]]
    assert {c["timeline_clip_id"] for c in svg["clips"]} == {rec["timeline_clip_id"], other}
    assert [f["text"] for f in svg["fields"]] == ["Day 1", "Tokyo"] and svg["kind"] == "svg"
    made = titles[hf_rec["file_id"]]
    assert made["kind"] == "hyperframes" and made["style"] == "lower_third"
    assert made["fields"] == {"title": "Jane", "subtitle": "Host"}
    assert [c["timeline_clip_id"] for c in made["clips"]] == [hf_rec["timeline_clip_id"]]
    query = receipt(tt.call("list_title_templates_tool", kind="project", query="host"))["project_titles"]
    assert [t["file_id"] for t in query] == [hf_rec["file_id"]]


# ---------------------------------------------------------------------------
# Found live: template fonts that are not installed, undo reports
# ---------------------------------------------------------------------------

def test_a_missing_template_font_becomes_the_title_editors_fallback(tt, monkeypatch):
    from classes.editor_tools import titles_text
    rec = _svg_title(tt, "Fonts")
    monkeypatch.setattr(titles_text, "installed_font_families", lambda: ["Arial", "Arial Black", "Helvetica"])
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Fonts 2"))
    assert edit["font"] == "Arial" and edit["font_replaced"] == "DejaVu Sans"
    doc = title_svg.load(edit["path"])
    assert title_svg.node_style(title_svg.tspan_nodes(doc)[0])["font-family"] == "'Arial'"
    # an installed template font is left alone
    monkeypatch.setattr(titles_text, "installed_font_families", lambda: ["DejaVu Sans", "Arial"])
    kept = _svg_title(tt, "Kept", position=6.0)
    assert "font_replaced" not in receipt(tt.call("edit_title_tool", timeline_clip_id=kept["timeline_clip_id"],
                                                  text="Kept 2"))
    assert title_svg.editor_font_family("DejaVu Sans", ["Arial"]) == "Arial"
    # macOS ships many script-specific "Noto Sans X" families: an exact fallback name wins
    assert title_svg.editor_font_family("DejaVu Sans", ["Noto Sans Armenian", "Arial Black", "Arial"]) == "Arial"
    assert title_svg.editor_font_family("DejaVu Sans", ["DejaVu Sans Mono", "Arial"]) == "DejaVu Sans Mono"
    assert title_svg.editor_font_family("Nope", []) == ""


def test_undo_of_a_title_edit_is_reported_as_a_change(tt):
    rec = _svg_title(tt, "Day 1")
    tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Day 2")
    out = tt.call("undo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert tt.clip(rec["timeline_clip_id"])["reader"]["path"] == rec["path"]
    out = tt.call("redo_tool", steps=1)
    assert not out.startswith("Error"), out


def test_place_clip_can_skip_the_preview_refresh_for_batches(tt):
    """Batch importers skip the per-clip preview refresh; a single clip refreshes as before."""
    from classes.editor_tools import titles_text_common as common
    timeline = tt.window.timeline
    f = tt.add_file("video", duration=4.0)
    common.place_clip(f, 0.0, 2.0, 1000000)
    assert timeline.add_calls[-1]["ignore_refresh"] is False and timeline.update_calls[-1]["ignore_refresh"] is False
    for i in range(3):
        common.place_clip(f, 3.0 + i * 3, 2.0, 1000000, ignore_refresh=i < 2)
    assert [c["ignore_refresh"] for c in timeline.add_calls[-3:]] == [True, True, False]
    assert [c["ignore_refresh"] for c in timeline.update_calls[-3:]] == [True, True, False]
    tt.window.IgnoreUpdates.emit.reset_mock()
    common.end_clip_batch()
    tt.window.IgnoreUpdates.emit.assert_called_once_with(False, False)
