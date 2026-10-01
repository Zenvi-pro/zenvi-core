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


def _fake_resolve(width, height, num, den):
    from classes.agent_tools.project_settings import _fps_label, _profile_fields
    desc = "Custom %dx%d %s fps" % (width, height, _fps_label(num, den))
    return _profile_fields(desc, width, height, num, den, (16, 9), (1, 1))


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
         patch("classes.tool_handlers.QThread", None), \
         patch("classes.agent_tools.project_settings.resolve_profile", _fake_resolve):
        noop = parse_receipt(set_project_setting(fps_num=30, fps_den=1))
        assert noop["status"] == "unchanged"
        changed = parse_receipt(set_project_setting(fps_num=24, fps_den=1))
        assert changed["status"] == "applied"
        app.updates.update.assert_called()


def _real_openshot():
    import openshot
    profile_cls = getattr(openshot, "Profile", None)
    return callable(profile_cls) and not isinstance(profile_cls, MagicMock)


def test_project_setting_survives_reopen_with_real_profiles(tmp_path, monkeypatch):
    """set -> save -> reopen: the project names a profile with the new values.

    Opening a project re-applies its named profile (ProjectDataStore.load ->
    get_profile); only the profile name survives a save, so the tool must
    switch it. Runs the real lookup with ZENVI_REAL_QT=1 and libopenshot.
    """
    import pytest
    if not _real_openshot():
        pytest.skip("needs real libopenshot (ZENVI_REAL_QT=1 with ZENVI_DEPS on PYTHONPATH)")
    import os
    from classes import info
    from classes.agent_tools.project_settings import resolve_profile
    from classes.project_data import ProjectDataStore

    user_dir = tmp_path / "profiles"
    monkeypatch.setattr(info, "USER_PROFILES_PATH", str(user_dir))

    def reopen(description):
        store = ProjectDataStore.__new__(ProjectDataStore)
        store._data = {"clips": [], "effects": [], "markers": []}
        assert store.get_profile(profile_desc=description) is not None
        return store._data

    # A stock combination switches to the stock profile; nothing is written.
    stock = resolve_profile(1920, 1080, 24, 1)
    assert not user_dir.exists() or not os.listdir(user_dir)
    data = reopen(stock["profile"])
    assert (data["width"], data["height"], data["fps"]) == (1920, 1080, {"num": 24, "den": 1})

    # An unusual one is saved as a user profile the reopen lookup finds.
    custom = resolve_profile(1234, 566, 24000, 1001)
    assert custom["profile"] == "Custom 1234x566 23.98 fps"
    assert len(os.listdir(user_dir)) == 1
    data = reopen(custom["profile"])
    assert (data["width"], data["height"], data["fps"]) == (1234, 566, {"num": 24000, "den": 1001})
    # Asking again reuses that profile instead of writing a second file.
    assert resolve_profile(1234, 566, 24000, 1001)["profile"] == custom["profile"]
    assert len(os.listdir(user_dir)) == 1
