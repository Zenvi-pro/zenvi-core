"""Timer overlays (add_timer_tool) and emoji (classes.emoji_catalog, search/add_emoji_tool)."""

import pytest

from classes import emoji_catalog
from titles_text_fakes import install_openshot_fakes, receipt, tt  # noqa: F401  (fixture)


def _kf(effect_or_clip, key):
    return effect_or_clip[key]["Points"][0]["co"]["Y"]


def _timer(tt, clip_id):
    return next(e for e in tt.clip(clip_id)["effects"] if e["class_name"] == "Timer")


# ---------------------------------------------------------------------------
# add_timer_tool
# ---------------------------------------------------------------------------

def test_countdown_overlay(tt):
    video = tt.add_file("video", duration=30.0)
    tt.add_clip(video, position=0.0)
    out = tt.call("add_timer_tool", mode="count_down", countdown_from_seconds=10, position_seconds=5,
                  screen_position="center", text_size=0.15)
    rec = receipt(out)
    assert out.startswith("Added a count down timer on track 2 at 5.00-15.00s (center)")
    clip = tt.clip(rec["timeline_clip_id"])
    assert clip["zenvi_role"] == "timer" and clip["title"] == "Timer" and clip["layer"] == 2000000
    t = _timer(tt, clip["id"])
    assert (t["mode"], t["time_source"], t["gravity"], t["apply_before_clip"]) == (1, 0, 4, False)
    assert _kf(t, "end_time") == 10.0 and _kf(t, "x_offset") == 0.0 and _kf(t, "y_offset") == 0.0
    assert _kf(t, "font_size") == round(0.15 * 1080 * 600 / 1920, 3)
    assert tt.file(clip["file_id"])["zenvi_role"] == "text_overlay"
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.clip(clip["id"]) is None


def test_stopwatch_in_a_corner_and_until_the_end_of_the_timeline(tt):
    video = tt.add_file("video", duration=30.0)
    tt.add_clip(video, position=0.0, end=24.0)
    rec = receipt(tt.call("add_timer_tool", mode="count_up", screen_position="top_right", format="hh:mm:ss",
                          prefix="T+", margin_percent=4))
    assert rec["duration"] == 24.0
    t = _timer(tt, rec["timeline_clip_id"])
    assert (t["mode"], t["format"], t["gravity"], t["prefix"]) == (0, 1, 2, "T+")
    assert _kf(t, "x_offset") == -4.0 and _kf(t, "y_offset") == 4.0


def test_timer_attached_to_a_trimmed_clip_counts_from_its_first_frame(tt):
    video = tt.add_file("video", duration=30.0)
    clip = tt.add_clip(video, position=3.0, start=6.0, end=16.0)
    rec = receipt(tt.call("add_timer_tool", mode="count_up", timeline_clip_id=clip, screen_position="bottom_left"))
    assert rec["overlay"] is False and rec["duration"] == 10.0
    t = _timer(tt, clip)
    assert t["time_source"] == 1 and _kf(t, "start_time") == -6.0 and t["gravity"] == 6
    assert len(tt.clips()) == 1 and tt.undo_steps_since_mark() == 1


def test_clock_mode_starts_at_a_time_of_day(tt):
    rec = receipt(tt.call("add_timer_tool", mode="clock", start_at_seconds=34200, duration_seconds=8))
    t = _timer(tt, rec["timeline_clip_id"])
    assert t["mode"] == 2 and _kf(t, "start_time") == 34200.0 and rec["duration"] == 8.0


@pytest.mark.parametrize("args, needle", [
    ({"mode": "sideways"}, "must be one of"),
    ({"text_color": "nope"}, "text_color"),
    ({"timeline_clip_id": "missing"}, "no timeline clip"),
    ({"screen_position": "middle"}, "must be one of"),
    ({"format": "seconds"}, "must be one of"),
])
def test_timer_refusals(tt, args, needle):
    tt.mark()
    out = tt.call("add_timer_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert tt.undo_steps_since_mark() == 0


def test_timer_refused_on_locked_or_busy_tracks_and_without_libopenshot_support(tt, monkeypatch):
    video = tt.add_file("video", duration=30.0)
    clip = tt.add_clip(video, position=0.0)
    tt.lock_track(1000000)
    tt.mark()
    out = tt.call("add_timer_tool", timeline_clip_id=clip)
    assert out.startswith("Error") and "locked" in out
    out = tt.call("add_timer_tool", track="1")
    assert out.startswith("Error") and "locked" in out
    install_openshot_fakes(tt, monkeypatch, missing_effects=("Timer",))
    out = tt.call("add_timer_tool")
    assert out.startswith("Error") and "no Timer effect" in out
    assert tt.undo_steps_since_mark() == 0 and len(tt.clips()) == 1


# ---------------------------------------------------------------------------
# emoji
# ---------------------------------------------------------------------------

def test_emoji_catalog_matches_the_dock():
    entries = emoji_catalog.entries()
    assert len(entries) == 1239
    fire = next(e for e in entries if e.code == "1F525")
    assert (fire.name, fire.group, fire.group_name, fire.char) == ("Fire", "travel-places", "Travel", "🔥")
    assert ("Smileys", "smileys-emotion") in emoji_catalog.groups()


@pytest.mark.parametrize("query, code", [
    ("fire", "1F525"), ("🔥", "1F525"), ("1F525", "1F525"), ("u+1f525", "1F525"),
    ("heart", "2764"), ("red heart", "2764"), ("❤️", "2764"), ("party", "1F389"), ("laugh", "1F602"),
    ("100", "1F4AF"), ("boom", "1F4A5"), ("coffee", "2615"),
])
def test_emoji_search_finds_what_people_mean(query, code):
    best = emoji_catalog.search(query)[0][1]
    assert best.code == code, [(round(s), e.code, e.name) for s, e in emoji_catalog.search(query)[:5]]


def test_search_emojis_tool_filters_by_group(tt):
    rec = receipt(tt.call("search_emojis_tool", query="cake", group="food-drink", limit=3))
    assert rec["matches"] and all(m["group"] == "food-drink" for m in rec["matches"])
    assert tt.call("search_emojis_tool").startswith("Error")
    assert tt.call("search_emojis_tool", query="zzqqxxjj").startswith("Error")


def test_add_emoji_top_right_scaled_above_the_video(tt):
    video = tt.add_file("video", duration=30.0)
    tt.add_clip(video, position=0.0)
    out = tt.call("add_emoji_tool", emoji="fire", screen_position="top_right", size=0.15, position_seconds=2,
                  duration_seconds=3)
    rec = receipt(out)
    assert out.startswith("Added 🔥 Fire on track 2 at 2.00-5.00s (top right, size 0.15)")
    clip = tt.clip(rec["timeline_clip_id"])
    assert clip["gravity"] == 2 and _kf(clip, "scale_x") == 0.15 and _kf(clip, "scale_y") == 0.15
    assert _kf(clip, "location_x") == round(-0.04 * 1080 / 1920, 5) and _kf(clip, "location_y") == 0.04
    f = tt.file(rec["file_id"])
    assert f["media_type"] == "image" and f["name"] == "Fire" and f["path"].endswith("1F525.svg")
    assert rec["alternatives"] and tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.clip(clip["id"]) is None and tt.file(rec["file_id"]) is None


def test_second_emoji_reuses_its_file_and_default_length(tt):
    a = receipt(tt.call("add_emoji_tool", emoji="❤️", screen_position="bottom_left"))
    b = receipt(tt.call("add_emoji_tool", emoji="red heart", position_seconds=20))
    assert a["file_id"] == b["file_id"] and a["duration"] == 10.0
    assert tt.clip(a["timeline_clip_id"])["gravity"] == 6


@pytest.mark.parametrize("args, needle", [
    ({"emoji": "zzqqxxjj"}, "no emoji matches"),
    ({"emoji": "thumbs up"}, "no emoji matches"),     # the bundled set has no hands
    ({"emoji": ""}, "missing required"),
    ({"emoji": "fire", "size": 3}, "must be <="),
    ({"emoji": "fire", "track": "9"}, "track"),
])
def test_add_emoji_refusals(tt, args, needle):
    tt.mark()
    out = tt.call("add_emoji_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert tt.undo_steps_since_mark() == 0
