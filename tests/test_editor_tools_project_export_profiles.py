"""project-export: profiles, social presets, reframe, set_project_setting, custom profiles."""

import json
import os
from unittest.mock import MagicMock

import pytest

from classes import info


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


@pytest.fixture
def profiles(editor, tmp_path, monkeypatch):
    """Real built-in profiles, a temp user-profiles folder, and the dialog core wired in."""
    from classes import project_profile
    from classes.project_profile import apply_profile_values

    user_dir = tmp_path / "user_profiles"
    user_dir.mkdir()
    monkeypatch.setattr(info, "USER_PROFILES_PATH", str(user_dir))
    project_profile.invalidate_catalog()
    # The store's fps-change hook looks profiles up through libopenshot (not in the stub).
    editor.store.get_profile = lambda *a, **k: None
    editor.store._data["profile"] = "FHD 1080p 30 fps"

    def apply_project_profile(record, before_seek=None):
        changed = apply_profile_values(record)
        if before_seek:
            before_seek()
        return changed

    editor.window.apply_project_profile = MagicMock(side_effect=apply_project_profile)
    settings = {"default-profile": "HD 720p 30 fps"}
    s = MagicMock()
    s.get.side_effect = lambda k, d=None: settings.get(k, d)
    s.set.side_effect = lambda k, v: settings.__setitem__(k, v)
    editor.app.get_settings.return_value = s
    editor.settings_store = settings
    yield user_dir
    project_profile.invalidate_catalog()


# --- resolution -----------------------------------------------------------

def test_social_presets_map_to_real_profile_files(editor, profiles):
    from classes.editor_tools.project_export_profiles import resolve_profile_request
    reel = resolve_profile_request("Instagram Reels")
    assert (reel["width"], reel["height"], reel["fps_num"]) == (1080, 1920, 30)
    assert reel["description"] == "FHD Vertical 1080p 30 fps" and os.path.isfile(reel["path"])
    assert resolve_profile_request("instagram_post")["description"] == "HD Square 1080p 30 fps"
    assert resolve_profile_request("4:5")["height"] == 1350
    assert resolve_profile_request("youtube_4k")["width"] == 3840
    cinema = resolve_profile_request("cinema")
    assert (cinema["width"], cinema["fps_num"], cinema["fps_den"]) == (1920, 24, 1)
    assert resolve_profile_request("tiktok", fps=60)["fps_num"] == 60
    assert resolve_profile_request("1280x720")["description"] == "HD 720p 30 fps"
    assert resolve_profile_request("FHD 1080p 29.97 fps")["fps_num"] == 30000


def test_social_preset_keeps_the_project_frame_rate(editor, profiles):
    from classes.editor_tools.project_export_profiles import resolve_profile_request
    editor.store._data["fps"] = {"num": 25, "den": 1}
    assert resolve_profile_request("tiktok")["description"] == "FHD Vertical 1080p 25 fps"


def test_unknown_or_ambiguous_profiles_are_refused_with_help(editor, profiles):
    out = editor.call("set_project_profile_tool", profile="hologram")
    assert out.startswith("Error") and "instagram_reel" in out
    out = editor.call("set_project_profile_tool", profile="NTSC")
    assert out.startswith("Error") and "matches" in out
    assert "say what to change" in editor.call("set_project_profile_tool")
    assert editor.undo_steps_since_mark() == 0


def test_list_profiles_filters(editor, profiles):
    data = _receipt(editor.call("list_project_profiles_tool", orientation="portrait", height=1920, fps=30))
    names = {p["name"] for p in data["profiles"]}
    assert "FHD Vertical 1080p 30 fps" in names and all(p["height"] == 1920 for p in data["profiles"])
    assert any(s["name"] == "instagram_reel" for s in data["social_presets"])
    data = _receipt(editor.call("list_project_profiles_tool", aspect="1:1", include_social_presets=False))
    assert data["profiles"] and all(p["aspect"] == "1:1" for p in data["profiles"])
    assert "social_presets" not in data


# --- set_project_profile + reframe ----------------------------------------

def _landscape_clip(editor, **kw):
    return editor.add_clip(editor.add_file("video"), **kw)


def test_instagram_reel_with_fill_reframes_landscape_clips_in_one_undo_step(editor, profiles):
    video = _landscape_clip(editor)
    audio = editor.add_clip(editor.add_file("audio"), layer=2000000)
    image = editor.add_clip(editor.add_file("image"), position=30.0)
    pip = _landscape_clip(editor, position=60.0,
                          scale_x={"Points": [{"co": {"X": 1, "Y": 0.4}, "interpolation": 0}]})
    title = _landscape_clip(editor, position=90.0)
    editor.store._data["clips"][-1]["reader"]["path"] = "/titles/title.svg"
    editor.mark()

    data = _receipt(editor.call("set_project_profile_tool", profile="instagram_reel", reframe="fill"))
    assert (editor.get("width"), editor.get("height")) == (1080, 1920)
    assert editor.get("profile") == "FHD Vertical 1080p 30 fps"
    assert editor.get("display_ratio") == {"num": 9, "den": 16}
    assert set(data["reframed_clip_ids"]) == {video, image}
    assert editor.clip(video)["scale"] == 0 and editor.clip(video)["gravity"] == 4
    assert editor.clip(audio).get("scale", 1) == 1
    reasons = {s["timeline_clip_id"]: s["reason"] for s in data["skipped"]}
    assert "picture-in-picture" in reasons[pip] and "SVG" in reasons[title]
    assert data["suggested_export_preset"] == "Instagram Reels"
    assert editor.undo_steps_since_mark() == 1

    editor.undo()
    assert (editor.get("width"), editor.get("height")) == (1920, 1080)
    assert editor.clip(video)["scale"] == 1
    editor.redo()
    assert editor.get("height") == 1920 and editor.clip(video)["scale"] == 0


def test_keep_reports_letterboxed_clips_and_changes_no_clip(editor, profiles):
    video = _landscape_clip(editor)
    data = _receipt(editor.call("set_project_profile_tool", profile="tiktok"))
    assert data["letterboxed_clip_ids"] == [video] and data["reframed_clip_ids"] == []
    assert editor.clip(video)["scale"] == 1
    assert "reframe='fill'" in editor.call("set_project_profile_tool", profile="tiktok")


def test_same_profile_is_a_no_op(editor, profiles):
    editor.store._data.update(profile="FHD 1080p 30 fps", width=1920, height=1080,
                              display_ratio={"num": 16, "den": 9}, pixel_ratio={"num": 1, "den": 1})
    data = _receipt(editor.call("set_project_profile_tool", profile="FHD 1080p 30 fps"))
    assert data["changed"] is False
    assert editor.undo_steps_since_mark() == 0
    editor.window.apply_project_profile.assert_not_called()


def test_reframe_on_a_locked_track_is_skipped(editor, profiles):
    editor.lock_track(1000000)
    video = _landscape_clip(editor)
    data = _receipt(editor.call("set_project_profile_tool", profile="vertical", reframe="fill"))
    assert data["reframed_clip_ids"] == [] and data["skipped"][0]["reason"] == "track is locked"
    assert editor.clip(video)["scale"] == 1


def test_orientation_flip_and_fps(editor, profiles):
    editor.store._data.update(width=1920, height=1080)
    data = _receipt(editor.call("set_project_profile_tool", orientation="portrait"))
    assert (data["profile"]["width"], data["profile"]["height"]) == (1080, 1920)
    data = _receipt(editor.call("set_project_profile_tool", profile="4k", fps=24))
    assert data["profile"]["name"] == "4K UHD 2160p 24 fps"
    assert editor.get("fps") == {"num": 24, "den": 1}


def test_match_a_file_and_odd_sizes_get_a_custom_profile(editor, profiles):
    fid = editor.add_file("video")  # 1280x720 at 30 fps
    data = _receipt(editor.call("set_project_profile_tool", from_file_id=fid))
    assert data["profile"]["name"] == "HD 720p 30 fps"
    data = _receipt(editor.call("set_project_profile_tool", width=1000, height=1000, fps=30))
    assert data["profile"]["name"] == "Custom 1000x1000 30 fps" and data["profile"]["user"] is True
    assert os.listdir(profiles), "the custom profile file is saved like the profile editor does"
    assert "even" in editor.call("set_project_profile_tool", width=1001, height=1000)
    audio = editor.add_file("audio")
    assert "no picture" in editor.call("set_project_profile_tool", from_file_id=audio)


# --- set_project_setting (PR #183 names) -----------------------------------

def test_set_project_setting_accepts_the_pr183_arguments(editor, profiles):
    data = _receipt(editor.call("set_project_setting_tool", fps=24))
    assert data["changed"] is True and editor.get("fps") == {"num": 24, "den": 1}
    assert editor.get("profile") == "FHD 1080p 24 fps"
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    data = _receipt(editor.call("set_project_setting_tool", fps_num=30000, fps_den=1001))
    assert editor.get("fps") == {"num": 30000, "den": 1001}
    editor.mark()
    data = _receipt(editor.call("set_project_setting_tool", width=1280, height=720))
    assert (editor.get("width"), editor.get("height")) == (1280, 720)


def test_set_project_setting_audio_and_no_ops(editor, profiles):
    data = _receipt(editor.call("set_project_setting_tool", sample_rate=44100, channels="mono"))
    assert editor.get("sample_rate") == 44100 and editor.get("channels") == 1 and editor.get("channel_layout") == 4
    assert data["channel_layout"] == "mono" and editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.get("sample_rate") == 48000 and editor.get("channels") == 2
    editor.mark()
    data = _receipt(editor.call("set_project_setting_tool", sample_rate=48000))
    assert data["changed"] is False and editor.undo_steps_since_mark() == 0
    assert "sample_rate must be" in editor.call("set_project_setting_tool", sample_rate=12345)
    assert "channels must be" in editor.call("set_project_setting_tool", channels="quad")
    assert "nothing to change" in editor.call("set_project_setting_tool")
    assert "fps_num and fps_den" in editor.call("set_project_setting_tool", fps_num=30)


def test_set_project_setting_preset_and_default(editor, profiles):
    _receipt(editor.call("set_project_setting_tool", preset="youtube_shorts"))
    assert (editor.get("width"), editor.get("height")) == (1080, 1920)
    _receipt(editor.call("set_project_setting_tool", sample_rate=44100, save_as_default=True))
    assert editor.settings_store["default-samplerate"] == 44100


# --- custom profiles ------------------------------------------------------

def test_custom_profiles_lifecycle(editor, profiles):
    data = _receipt(editor.call("manage_custom_profiles_tool", action="create", name="Phone 720x1280 24",
                                width=720, height=1280, fps=24))
    path = data["path"]
    assert os.path.isfile(path) and data["profile"]["aspect"] == "9:16"
    out = editor.call("manage_custom_profiles_tool", action="create", name="Phone 720x1280 24", width=720, height=1280)
    assert out.startswith("Error") and "already exists" in out
    data = _receipt(editor.call("manage_custom_profiles_tool", action="duplicate", profile="FHD 1080p 30 fps"))
    assert data["profile"]["name"] == "FHD 1080p 30 fps (copy)" and data["profile"]["user"]
    assert "built-in" in editor.call("manage_custom_profiles_tool", action="edit", profile="FHD 1080p 30 fps",
                                     width=1000, height=500)
    data = _receipt(editor.call("manage_custom_profiles_tool", action="edit", profile="Phone 720x1280 24", fps=30))
    assert data["profile"]["fps"] == 30.0 and data["path"] == path
    _receipt(editor.call("manage_custom_profiles_tool", action="set_default", profile="Phone 720x1280 24"))
    assert editor.settings_store["default-profile"] == "Phone 720x1280 24"
    assert "cannot delete" in editor.call("manage_custom_profiles_tool", action="delete", profile="Phone 720x1280 24")
    _receipt(editor.call("manage_custom_profiles_tool", action="delete", profile="FHD 1080p 30 fps (copy)"))
    assert not any("copy" in n for n in os.listdir(profiles))
    assert editor.undo_steps_since_mark() == 0
