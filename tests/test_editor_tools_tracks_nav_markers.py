"""Marker tools: add_marker_tool, update_marker_tool, remove_marker_tool, list_markers_tool."""

import json

from classes import track_ops


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


def markers(editor):
    return sorted(editor.get("markers") or [], key=lambda m: m["position"])


def add(editor, seconds, name="", color="blue"):
    data = receipt(editor.call("add_marker_tool", position_seconds=seconds, name=name, color=color))
    editor.mark()
    return data["marker_id"]


def test_add_marker_at_a_time_with_a_name_and_color(editor):
    out = editor.call("add_marker_tool", position_seconds=32, name="Drop", color="red")
    data = receipt(out)
    (m,) = markers(editor)
    assert m["position"] == 32.0 and m["name"] == "Drop"
    assert m["icon"] == "red.png" and m["vector"] == "red"
    assert data["marker_id"] == m["id"]
    assert data["marker"]["timecode"] == "00:00:32,00" and data["marker"]["frame"] == 961
    assert "Drop" in out
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert markers(editor) == []
    editor.redo()
    assert markers(editor)[0]["name"] == "Drop"


def test_add_marker_defaults_to_the_playhead_and_snaps_to_frames(editor):
    editor.window.preview_thread.player.Position.return_value = 46  # frame 46 at 30 fps = 1.5 s
    receipt(editor.call("add_marker_tool"))
    assert markers(editor)[0]["position"] == 1.5
    receipt(editor.call("add_marker_tool", position_seconds=2.01))
    assert markers(editor)[1]["position"] == 2.0


def test_add_marker_reports_other_markers_on_the_same_frame(editor):
    add(editor, 5, "A")
    data = receipt(editor.call("add_marker_tool", position_seconds=5, name="B"))
    assert [r["name"] for r in data["also_at_this_time"]] == ["A"]
    assert len(markers(editor)) == 2


def test_add_marker_refusals(editor):
    assert "past the end" in editor.call("add_marker_tool", position_seconds=301)
    assert editor.call("add_marker_tool", position_seconds=-1).startswith("Error")
    assert editor.call("add_marker_tool", position_seconds=1, color="teal").startswith("Error")
    assert editor.undo_steps_since_mark() == 0 and markers(editor) == []


def test_ui_marker_shape_is_unchanged(editor):
    """Add Marker (M) still writes {position, icon: blue.png, vector: blue} and no name."""
    track_ops.add_marker(1.25)
    (m,) = markers(editor)
    assert m["icon"] == "blue.png" and m["vector"] == "blue" and "name" not in m


def test_list_markers_in_time_order(editor):
    empty = receipt(editor.call("list_markers_tool"))
    assert empty["markers"] == []
    add(editor, 12, "Chorus", "green")
    add(editor, 3, "Intro")
    track_ops.add_marker(7.0)  # an unnamed marker made by the M key
    out = editor.call("list_markers_tool")
    data = receipt(out)
    assert [r["name"] for r in data["markers"]] == ["Intro", "", "Chorus"]
    assert data["markers"][2]["color"] == "green" and data["markers"][1]["color"] == "blue"
    assert data["markers"][0]["timecode"] == "00:00:03,00" and data["markers"][0]["seconds"] == 3.0
    assert "unnamed" in out


def test_update_marker_by_id_or_name(editor):
    mid = add(editor, 10, "Verse")
    data = receipt(editor.call("update_marker_tool", marker=mid, name="Chorus"))
    assert data["changed"] == ["name"] and markers(editor)[0]["name"] == "Chorus"
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    data = receipt(editor.call("update_marker_tool", marker="chorus", position_seconds=14.5, color="yellow"))
    assert sorted(data["changed"]) == ["color", "position"]
    m = markers(editor)[0]
    assert m["position"] == 14.5 and m["vector"] == "yellow" and m["icon"] == "yellow.png"
    editor.undo()
    m = markers(editor)[0]
    assert m["position"] == 10.0 and m["vector"] == "blue"


def test_update_marker_noop_and_refusals(editor):
    mid = add(editor, 10, "Verse")
    add(editor, 20, "Dup")
    add(editor, 30, "Dup")
    out = editor.call("update_marker_tool", marker=mid, name="Verse")
    assert receipt(out)["changed"] == [] and editor.undo_steps_since_mark() == 0
    assert "nothing to change" in editor.call("update_marker_tool", marker=mid)
    assert "no marker" in editor.call("update_marker_tool", marker="Nope", name="x")
    out = editor.call("update_marker_tool", marker="Dup", name="x")
    assert out.startswith("Error") and "2 markers are named" in out
    assert "past the end" in editor.call("update_marker_tool", marker=mid, position_seconds=999)
    assert editor.undo_steps_since_mark() == 0


def test_remove_marker_by_name_id_time_and_all(editor):
    a = add(editor, 3, "Intro")
    add(editor, 12, "Drop")
    add(editor, 20, "Outro")
    add(editor, 25)
    data = receipt(editor.call("remove_marker_tool", markers=["Drop"]))
    assert [r["name"] for r in data["removed"]] == ["Drop"] and data["remaining"] == 3
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    receipt(editor.call("remove_marker_tool", markers=[a]))
    receipt(editor.call("remove_marker_tool", at_seconds=20.3))
    assert [m.get("name", "") for m in markers(editor)] == [""]
    editor.mark()
    receipt(editor.call("remove_marker_tool", all=True))
    assert markers(editor) == [] and editor.undo_steps_since_mark() == 1
    editor.undo()
    assert len(markers(editor)) == 1


def test_remove_several_markers_is_one_step(editor):
    add(editor, 1, "A")
    add(editor, 2, "B")
    receipt(editor.call("remove_marker_tool", markers="A, B"))
    assert markers(editor) == [] and editor.undo_steps_since_mark() == 1
    editor.undo()
    assert len(markers(editor)) == 2


def test_remove_marker_refusals(editor):
    assert "no markers" in editor.call("remove_marker_tool", all=True)
    add(editor, 10, "Dup")
    add(editor, 20, "Dup")
    assert "say which markers" in editor.call("remove_marker_tool")
    assert "only one of" in editor.call("remove_marker_tool", markers=["Dup"], all=True)
    out = editor.call("remove_marker_tool", markers=["Dup"])
    assert out.startswith("Error") and "2 markers are named" in out
    assert "nearest" in editor.call("remove_marker_tool", at_seconds=15)
    assert "no marker" in editor.call("remove_marker_tool", markers=["Nope"])
    assert len(markers(editor)) == 2 and editor.undo_steps_since_mark() == 0


def test_navigation_positions_match_the_marker_menu(editor):
    """Previous/Next Marker stops: start, markers, end when nothing is selected, else the
    selected clip's edges and keyframes (main_window.findAllMarkerPositions)."""
    track_ops.add_marker(4.0)
    f = editor.add_file("video")
    c = editor.add_clip(f, position=10.0, start=2.0, end=6.0,
                        alpha={"Points": [{"co": {"X": 91.0, "Y": 1.0}}]})  # 3.0 s into the source
    no_selection = sorted(track_ops.navigation_positions(last_frame=301))
    assert no_selection == [0.0, 4.0, 10.0]
    with_clip = sorted(track_ops.navigation_positions(selected_clips=[c]))
    # start 10, keyframe at 10 + (3.0 - 2.0) = 11, last frame of the clip 14 - 1/30
    assert with_clip[:4] == [0.0, 4.0, 10.0, 11.0] and abs(with_clip[4] - (14 - 1 / 30)) < 1e-9
    assert track_ops.adjacent_position(with_clip, 10.0, 1) == 11.0
    assert track_ops.adjacent_position(with_clip, 10.0, -1) == 4.0
    assert track_ops.adjacent_position([0.0], 0.0, -1) is None
