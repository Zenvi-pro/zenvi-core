"""apply_transition_tool: the transition it picks, the duration it uses and what its reply says."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from classes import info
from classes import tool_handlers as th

TRANSITIONS = ("fade", "circle_in_to_out", "wipe_left_to_right", "wipe_right_to_left")


@pytest.fixture(autouse=True)
def _project_env(tmp_path, monkeypatch):
    """Four transition images in the Transitions folder, and a fake libopenshot."""
    common = tmp_path / "transitions" / "common"
    common.mkdir(parents=True)
    for name in TRANSITIONS:
        (common / f"{name}.svg").write_bytes(b"<svg/>")
    monkeypatch.setattr(info, "PATH", str(tmp_path))
    monkeypatch.setattr(info, "TRANSITIONS_PATH", str(tmp_path / "no-user-folder"))
    fake_openshot = MagicMock()
    fake_openshot.Keyframe.return_value.Json.return_value = "{}"
    fake_openshot.QtImageReader.return_value.Json.return_value = "{}"
    monkeypatch.setitem(sys.modules, "openshot", fake_openshot)
    monkeypatch.setattr(th, "QThread", None)


def _clip(cid, position, start=0.0, end=5.0):
    return SimpleNamespace(id=cid, data={"id": cid, "layer": 1000000, "position": position,
                                         "start": start, "end": end})


def _pair(c2_pos=5.0, c1_end=5.0):
    return [_clip("c1", 0.0, end=c1_end), _clip("c2", c2_pos)]


def _apply(clips, **args):
    """Run the apply_transition_tool handler on a fake project; returns (reply, fake app)."""
    by_id = {c.id: c for c in clips}
    app = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch.object(th, "_get_app", return_value=app), \
         patch("classes.query.Clip.get", side_effect=lambda id=None: by_id.get(id)):
        reply = th.AGENT_TOOL_HANDLERS["apply_transition_tool"](**args)
    return reply, app


def test_adjacent_clips_get_one_transition_at_their_join():
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", transition_name="fade", duration="0.5")
    assert not reply.startswith("Error"), reply
    added = app.window.timeline.update_transition_data
    assert added.call_count == 1
    data = added.call_args.args[0]
    assert (data["type"], data["title"], data["position"], data["end"]) == ("Mask", "fade", 4.5, 0.5)
    app.updates.update.assert_called_once_with(["clips", {"id": "c2"}], {"position": 4.5})
    assert "fade" in reply and "at 4.50s" in reply and "duration 0.50s" in reply
    assert "clamped" not in reply


def test_blank_duration_is_the_default_one_second_and_is_reported():
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", transition_name="fade", duration="")
    assert not reply.startswith("Error"), reply
    assert app.window.timeline.update_transition_data.call_args.args[0]["end"] == 1.0
    assert "duration 1.00s" in reply


def test_overlap_longer_than_clip2_start_is_clamped_and_reported():
    reply, app = _apply(_pair(c2_pos=0.5, c1_end=0.5), clip1_id="c1", clip2_id="c2",
                        transition_name="fade", duration="1.0")
    data = app.window.timeline.update_transition_data.call_args.args[0]
    assert (data["position"], data["end"]) == (0.0, 0.5)
    assert "duration 0.50s (clamped from 1.00s)" in reply


@pytest.mark.parametrize("placement", ["between", "start"])
def test_partial_name_is_applied_and_reported_by_its_full_name(placement):
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", placement=placement,
                        transition_name="left to right", duration="0.5")
    assert not reply.startswith("Error"), reply
    assert app.window.timeline.update_transition_data.call_count == 1
    assert app.window.timeline.update_transition_data.call_args.args[0]["title"] == "wipe_left_to_right"
    assert "wipe_left_to_right" in reply


def test_partial_name_matching_several_transitions_is_an_error_naming_them():
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", transition_name="wipe", duration="0.5")
    assert reply.startswith("Error") and "wipe_left_to_right" in reply and "wipe_right_to_left" in reply
    assert not app.window.timeline.update_transition_data.called


@pytest.mark.parametrize("placement", ["between", "start"])
@pytest.mark.parametrize("name", ["", "   ", "nonexistent_wipe"])
def test_unknown_or_empty_name_is_an_error_that_lists_valid_names(placement, name):
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", placement=placement,
                        transition_name=name, duration="0.5")
    assert reply.startswith("Error") and "fade" in reply, reply
    assert not app.window.timeline.update_transition_data.called
    assert not app.updates.update.called


@pytest.mark.parametrize("placement", ["between", "start"])
@pytest.mark.parametrize("duration", ["abc", "0", "-2"])
def test_duration_that_is_not_a_positive_number_is_an_error(placement, duration):
    reply, app = _apply(_pair(), clip1_id="c1", clip2_id="c2", placement=placement,
                        transition_name="fade", duration=duration)
    assert reply.startswith("Error"), reply
    assert not app.window.timeline.update_transition_data.called
    assert not app.updates.update.called


@pytest.mark.parametrize("args", [
    {"clip1_id": "c1", "clip2_id": "nope"},
    {"clip1_id": "nope", "clip2_id": "c2"},
    {"clip1_id": "nope", "placement": "start"},
])
def test_missing_clip_is_an_error_and_adds_nothing(args):
    reply, app = _apply(_pair(), transition_name="fade", duration="0.5", **args)
    assert reply.startswith("Error"), reply
    assert not app.window.timeline.update_transition_data.called
    assert not app.updates.update.called
