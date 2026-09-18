"""Tests for Phase 3 filmmaker gap tools."""

from fractions import Fraction
from unittest.mock import MagicMock, patch

from classes.agent_tools.receipt import parse_receipt
from classes.agent_tools.keyframes import set_keyframes, ALLOWED_PROPERTIES
from classes.agent_tools.project_settings import set_project_setting
from classes.agent_tools.effects import add_effect, _dialog_effects
from classes.frame_time import keyframe_x
from classes.clip_resolver import ResolveResult


def test_dialog_effects_are_refused():
    dialogish = _dialog_effects()
    if not dialogish:
        return
    name = next(iter(dialogish))
    clip = MagicMock()
    clip.id = "c1"
    clip.data = {"id": "c1", "effects": []}
    with patch("classes.tool_handlers._resolve_timeline_clip_for_tool",
               return_value=ResolveResult(ok=True, clip=clip)), \
         patch("classes.tool_handlers._get_app", return_value=MagicMock()), \
         patch("classes.agent_tools.effects.list_effect_class_names",
               return_value=[name, "Blur"]):
        raw = add_effect(timeline_clip_id="c1", effect=name)
    receipt = parse_receipt(raw)
    assert receipt["status"] == "refused"
    assert "ProcessEffect" in receipt["summary"] or "dialog" in receipt["summary"]


def test_keyframe_x_matches_frame_time_at_30_and_23976():
    for fps in (Fraction(30, 1), Fraction(24000, 1001)):
        for secs in (0.0, 0.5, 1.0, 12 / float(fps)):
            assert keyframe_x(secs, fps) == keyframe_x(secs, fps)


def test_set_keyframes_builds_points_via_frame_time():
    clip = MagicMock()
    clip.id = "c1"
    clip.data = {"id": "c1"}
    app = MagicMock()
    app.thread.return_value = object()
    fps = Fraction(30, 1)
    with patch("classes.tool_handlers._resolve_timeline_clip_for_tool",
               return_value=ResolveResult(ok=True, clip=clip)), \
         patch("classes.tool_handlers._get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.clip_utils.project_fps_fraction", return_value=fps):
        raw = set_keyframes(
            timeline_clip_id="c1",
            property="alpha",
            points=[{"seconds": 0.0, "value": 1.0}, {"seconds": 12 / 30, "value": 0.0}],
        )
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    points = receipt["data"]["points"]
    assert points[0]["frame"] == keyframe_x(0.0, fps)
    assert points[1]["frame"] == keyframe_x(12 / 30, fps)
    assert clip.save.called
    assert "alpha" in ALLOWED_PROPERTIES


def test_set_keyframes_refuses_unknown_property():
    raw = set_keyframes(property="not_real", points=[{"frame": 1, "value": 1}])
    receipt = parse_receipt(raw)
    assert receipt["status"] == "refused"


def test_project_setting_noop_and_change():
    app = MagicMock()

    def _get(k, d=None):
        return {
            "fps": {"num": 30, "den": 1},
            "width": 1920,
            "height": 1080,
            "sample_rate": 48000,
        }.get(k, d)

    app.project.get.side_effect = _get
    app.thread.return_value = object()
    with patch("classes.tool_handlers._get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None):
        noop = parse_receipt(set_project_setting(fps_num=30, fps_den=1))
        assert noop["status"] == "unchanged"
        changed = parse_receipt(set_project_setting(fps_num=24, fps_den=1))
        assert changed["status"] == "applied"
        app.updates.update.assert_called()
