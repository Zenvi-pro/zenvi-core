"""Unit tests for Phase 6 colour agent helpers (no Qt / FrameScope)."""

from __future__ import annotations

import json

import pytest

from classes.color_agent import (
    blank_color_grade,
    merge_color_grade,
    resolve_relative_deltas,
    scalar_y,
    summarize_color_grade,
    validate_color_patch,
)


def test_merge_preserves_unset_scalars():
    base = blank_color_grade("eff1")
    set_temp = merge_color_grade(base, {"temperature": 0.2})
    assert scalar_y(set_temp, "temperature") == pytest.approx(0.2)
    assert scalar_y(set_temp, "exposure") == pytest.approx(0.0)
    assert scalar_y(set_temp, "saturation") == pytest.approx(1.0)

    nudged = merge_color_grade(set_temp, {"exposure": 0.15})
    assert scalar_y(nudged, "temperature") == pytest.approx(0.2)
    assert scalar_y(nudged, "exposure") == pytest.approx(0.15)


def test_merge_lut_and_wheels():
    base = blank_color_grade("eff1")
    merged = merge_color_grade(
        base,
        {
            "lut": {"path": "cinematic_&_blockbuster/teal_cinema.cube", "strength": 0.6},
            "wheels": {"shadows": {"color": "#224466", "amount": 0.3, "luma": 0.05}},
        },
    )
    assert merged["lut_path"].endswith("teal_cinema.cube")
    assert scalar_y(merged, "lut_intensity") == pytest.approx(0.6)
    assert merged["wheels"]["shadows"]["color"] == "#224466"
    assert merged["wheels"]["shadows"]["amount"] == pytest.approx(0.3)
    # Unrelated wheel stays neutral.
    assert merged["wheels"]["global"]["amount"] == pytest.approx(0.0)


def test_merge_curves_from_xy_points():
    base = blank_color_grade("eff1")
    merged = merge_color_grade(
        base,
        {"masterCurve": [[0.0, 0.0], [0.25, 0.2], [0.75, 0.8], [1.0, 1.0]]},
    )
    nodes = merged["curve_all"]["nodes"]
    assert len(nodes) == 4
    assert nodes[1]["x"]["Points"][0]["co"]["Y"] == pytest.approx(0.25)
    assert nodes[1]["y"]["Points"][0]["co"]["Y"] == pytest.approx(0.2)


def test_validate_rejects_bad_wheel_before_merge():
    with pytest.raises(ValueError, match="Unknown wheel"):
        validate_color_patch({"wheels": {"skin": {"amount": 0.1}}})
    with pytest.raises(ValueError, match="finite"):
        validate_color_patch({"exposure": "nope"})


def test_color_paste_keeps_effect_id():
    base = blank_color_grade("keep-me")
    paste = blank_color_grade("other")
    paste = merge_color_grade(paste, {"exposure": 0.4})
    merged = merge_color_grade(base, {"color": paste})
    assert merged["id"] == "keep-me"
    assert scalar_y(merged, "exposure") == pytest.approx(0.4)


def test_hand_written_paste_of_plain_numbers_grades():
    # Same case as the web engine's color-agent.test.ts: plain numbers become keyframes.
    merged = merge_color_grade(blank_color_grade("keep-me"), {"color": {"exposure": 1, "saturation": "1.5"}})
    assert merged["exposure"]["Points"][0]["co"]["Y"] == pytest.approx(1.0)
    assert scalar_y(merged, "saturation") == pytest.approx(1.5)


def test_partial_paste_keeps_fields_it_leaves_out():
    graded = merge_color_grade(dict(blank_color_grade("keep-me"), type="ColorGrade"), {"contrast": 1.3})
    merged = merge_color_grade(graded, {"color": {"exposure": 1}})
    assert merged["type"] == "ColorGrade"
    assert merged["id"] == "keep-me"
    assert scalar_y(merged, "contrast") == pytest.approx(1.3)
    assert scalar_y(merged, "exposure") == pytest.approx(1.0)


def test_paste_with_a_non_number_is_refused_before_merge():
    graded = merge_color_grade(blank_color_grade("keep-me"), {"contrast": 1.3})
    before = json.dumps(graded, sort_keys=True)
    with pytest.raises(ValueError, match="exposure must be a finite number"):
        validate_color_patch({"color": {"exposure": "bright"}})
    with pytest.raises(ValueError, match="exposure must be a finite number"):
        merge_color_grade(graded, {"color": {"exposure": "bright"}})
    for junk in (True, None, "", [1.0], {"value": 1}):
        with pytest.raises(ValueError, match="contrast must be a finite number"):
            merge_color_grade(graded, {"color": {"contrast": junk}})
    assert json.dumps(graded, sort_keys=True) == before


def test_full_reference_paste_matches_the_reference():
    # harmonize_look_tool pastes a whole reference grade: every knob ends up the reference's.
    subject = merge_color_grade(blank_color_grade("subject"), {"temperature": -0.3, "lut_path": "a.cube"})
    reference = merge_color_grade(blank_color_grade("ref"), {"temperature": 0.25, "exposure": 0.2})
    reference.pop("id")
    merged = merge_color_grade(subject, {"color": reference})
    assert merged["id"] == "subject"
    assert summarize_color_grade(merged) | {"id": None} == summarize_color_grade(reference) | {"id": None}


def test_find_color_grade_and_reset_filter():
    from classes.color_agent import find_color_grade, is_color_grade_effect

    effects = [
        {"class_name": "Blur", "id": "b1"},
        blank_color_grade("cg1"),
        {"class_name": "Glow", "id": "g1"},
    ]
    assert find_color_grade(effects)["id"] == "cg1"
    remaining = [e for e in effects if not is_color_grade_effect(e)]
    assert [e["id"] for e in remaining] == ["b1", "g1"]
    assert summarize_color_grade(None) == {"present": False}


def test_validate_before_merge_does_not_mutate():
    base = blank_color_grade("eff1")
    before = scalar_y(base, "exposure")
    with pytest.raises(ValueError):
        merge_color_grade(base, {"exposure": float("nan")})
    assert scalar_y(base, "exposure") == before
    with pytest.raises(ValueError):
        merge_color_grade(base, {"exposure": float("inf")})


def test_saturation_below_half_is_rejected():
    from classes.color_agent import validate_color_patch

    with pytest.raises(ValueError, match="saturation neutral is 1.0"):
        validate_color_patch({"saturation": 0.2})


def test_temperature_delta_nudges_from_current():
    from classes.color_agent import merge_color_grade

    base = merge_color_grade(blank_color_grade("e"), {"temperature": 0.10})
    warmer = merge_color_grade(base, {"temperature_delta": 0.08})
    assert scalar_y(warmer, "temperature") == pytest.approx(0.18)
    cooler = merge_color_grade(warmer, {"temperature_delta": -0.08})
    assert scalar_y(cooler, "temperature") == pytest.approx(0.10)


def test_exposure_and_vibrance_deltas_nudge():
    from classes.color_agent import merge_color_grade

    base = merge_color_grade(blank_color_grade("e"), {"exposure": 0.0, "vibrance": 0.0})
    sunnier = merge_color_grade(
        base,
        {"temperature_delta": 0.08, "exposure_delta": 0.04, "vibrance_delta": 0.05},
    )
    assert scalar_y(sunnier, "temperature") == pytest.approx(0.08)
    assert scalar_y(sunnier, "exposure") == pytest.approx(0.04)
    assert scalar_y(sunnier, "vibrance") == pytest.approx(0.05)


def test_match_removes_film_grain_when_reference_has_none(monkeypatch):
    import types
    from classes import tool_handlers as th
    from classes.film_grain_presets import apply_film_grain_preset

    grain = apply_film_grain_preset(
        {"class_name": "FilmGrain", "id": "g1"}, "35mm_classic"
    )
    same = {
        "present": True, "samples": 4, "avg_luma": 0.40, "warm_cool": 0.0,
        "green_magenta": 0.0, "sat_proxy": 0.15, "contrast_span": 0.42,
        "clipped_shadows": 0.02, "clipped_highlights": 0.02,
        "channel_means": {"red": 0.40, "green": 0.40, "blue": 0.40, "luma": 0.40},
    }

    def _fake_profile(clip_data, fps_float, **_kw):
        return {
            "profile": dict(same),
            "scopes_samples": 4,
            "sample_frames": [1],
            "preview_jpegs": [],
            "preview_jpeg": "",
            "duration_s": 2.0,
        }

    monkeypatch.setattr(th, "_profile_clip_dense", _fake_profile)
    writes = []

    def _fake_write(clip_obj, effects):
        writes.append(list(effects))
        clip_obj.data["effects"] = list(effects)

    monkeypatch.setattr(th, "_write_clip_effects", _fake_write)
    monkeypatch.setattr(th, "_new_transaction_id", lambda: "tid-g")

    class FakeWindow:
        class refreshFrameSignal:
            @staticmethod
            def emit():
                return None

    class FakeUpdates:
        transaction_id = None

    class FakeProject:
        @staticmethod
        def generate_id():
            return "effx"

        def get(self, k, d=None):
            return {"fps": {"num": 30, "den": 1}}.get(k, d)

    class FakeApp:
        project = FakeProject()
        updates = FakeUpdates()
        window = FakeWindow()

        def thread(self):
            return object()

    class FakeClip:
        def __init__(self, cid, effects):
            self.id = cid
            self.data = {"effects": effects, "start": 0, "end": 2, "position": 0, "_tag": cid}

    clips = {
        "sub": FakeClip("sub", [grain]),
        "ref": FakeClip("ref", []),
    }
    monkeypatch.setattr(th, "_get_app", lambda: FakeApp())
    monkeypatch.setattr(th, "QThread", None)
    fake_query = types.ModuleType("classes.query")

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clips.get(id)

    fake_query.Clip = ClipProxy
    fake_query.File = type("File", (), {"get": staticmethod(lambda **_k: None)})
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    out = json.loads(th._match_clips_to_reference(["sub"], "ref"))
    assert out["applied"] is True
    assert out["grain_mode"] == "reset"
    assert writes
    assert not any(e.get("class_name") == "FilmGrain" for e in writes[0])


def test_match_grain_action_helpers():
    from classes.color_agent import match_grain_action, summarize_film_grain
    from classes.film_grain_presets import apply_film_grain_preset

    grain = apply_film_grain_preset(
        {"class_name": "FilmGrain", "id": "g1"}, "35mm_classic"
    )
    assert summarize_film_grain(grain)["present"] is True
    assert match_grain_action(grain, None)["mode"] == "reset"
    assert match_grain_action(None, grain)["mode"] == "paste"
    assert match_grain_action(None, None)["mode"] == "noop"


def test_scope_look_distance_detects_warm_gap():
    from classes.color_agent import scope_look_distance

    cool = {
        "present": True,
        "avg_luma": 0.45,
        "warm_cool": -0.08,
        "green_magenta": 0.0,
        "channel_means": {"red": 0.4, "green": 0.45, "blue": 0.5},
    }
    warm = {
        "present": True,
        "avg_luma": 0.46,
        "warm_cool": 0.12,
        "green_magenta": 0.01,
        "channel_means": {"red": 0.55, "green": 0.45, "blue": 0.35},
    }
    dist = scope_look_distance(cool, warm)
    assert dist is not None and dist >= 0.03


def test_grades_meaningfully_differ_and_match_action():
    from classes.color_agent import (
        blank_color_grade,
        grades_meaningfully_differ,
        match_grade_action,
        merge_color_grade,
        summarize_color_grade,
    )

    none = summarize_color_grade(None)
    warm = summarize_color_grade(
        merge_color_grade(blank_color_grade("a"), {"temperature": 0.25, "saturation": 1.3})
    )
    assert grades_meaningfully_differ(warm, none) is True
    assert match_grade_action(
        merge_color_grade(blank_color_grade("a"), {"temperature": 0.25}),
        None,
    )["mode"] == "reset"
    paste = match_grade_action(
        None,
        merge_color_grade(blank_color_grade("b"), {"temperature": 0.2}),
    )
    assert paste["mode"] == "paste"
    assert paste["color"]["class_name"] == "ColorGrade"


def test_curve_and_wheel_defaults_match_the_look_menu(monkeypatch):
    """color_agent keeps its own copy so it imports without libopenshot; pin it to color_presets.py.
    Curve nodes written as BEZIER (0) instead of LINEAR (1) bent even the identity curve into an S.
    """
    from classes import color_agent as ca
    from classes import color_presets as cp

    # The headless openshot stub has no enums; these are libopenshot's values.
    monkeypatch.setattr(cp.openshot, "LINEAR", 1, raising=False)
    monkeypatch.setattr(cp.openshot, "AUTO", 0, raising=False)

    assert ca.default_curve_data() == cp.default_curve_data()
    assert ca.default_wheels_data() == cp.default_wheels_data()
    nodes = ca.points_to_curve([[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]])["nodes"]
    assert {node["interpolation"] for node in nodes} == {1}


def _match_env(monkeypatch, clips):
    """Fake app / Clip store for _match_clips_to_reference; returns the writes."""
    import sys
    import types

    from classes import tool_handlers as th

    monkeypatch.setattr(
        th,
        "_render_clip_isolated",
        lambda *_a, **_k: {"scopes": {"present": True}, "preview_jpeg": ""},
    )
    writes = []

    def _fake_write(clip_obj, effects):
        writes.append((clip_obj.id, list(effects)))
        clip_obj.data["effects"] = list(effects)

    monkeypatch.setattr(th, "_write_clip_effects", _fake_write)

    class FakeApp:
        class updates:
            transaction_id = None

        class window:
            class refreshFrameSignal:
                @staticmethod
                def emit():
                    return None

        class project:
            @staticmethod
            def generate_id():
                return "effx"

            @staticmethod
            def get(key, default=None):
                return {"fps": {"num": 30, "den": 1}}.get(key, default)

    monkeypatch.setattr(th, "_get_app", lambda: FakeApp())
    monkeypatch.setattr(th, "QThread", None)

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clips.get(id)

    fake_query = types.ModuleType("classes.query")
    fake_query.Clip = ClipProxy
    monkeypatch.setitem(sys.modules, "classes.query", fake_query)
    return writes


class _MatchClip:
    def __init__(self, cid, effects):
        self.id = cid
        self.data = {"id": cid, "effects": effects}


def test_match_refuses_a_missing_reference_instead_of_stripping_the_subject(monkeypatch):
    """A mistyped reference id used to read as "reference has no grade or
    grain" and remove both from the subject."""
    from classes import tool_handlers as th
    from classes.film_grain_presets import apply_film_grain_preset

    grade = merge_color_grade(blank_color_grade("cg1"), {"temperature": 0.3})
    grain = apply_film_grain_preset({"class_name": "FilmGrain", "id": "g1"}, "35mm_classic")
    clips = {"sub": _MatchClip("sub", [grade, grain])}
    writes = _match_env(monkeypatch, clips)

    out = th._match_clips_to_reference(["sub"], "typo")
    assert out.startswith("Error:") and "typo" in out
    assert writes == []
    assert clips["sub"].data["effects"] == [grade, grain]


def test_sample_count_for_duration_bounds():
    from classes.color_agent import sample_count_for_duration

    assert sample_count_for_duration(0) == 1
    assert sample_count_for_duration(0.01) == 1
    n30 = sample_count_for_duration(30 * 60)
    n60 = sample_count_for_duration(60 * 60)
    assert 16 <= n30 <= 96
    assert 16 <= n60 <= 96
    assert n60 >= n30


def test_look_profile_solver_and_outcome():
    from classes.color_agent import (
        assess_grade_outcome,
        build_look_profile,
        look_profile_distance,
        solve_grade_from_profiles,
    )

    cool = {
        "present": True,
        "avg_luma": 0.40,
        "warm_cool": -0.08,
        "green_magenta": 0.0,
        "sat_proxy": 0.12,
        "contrast_span": 0.40,
        "clipped_shadows": 0.02,
        "clipped_highlights": 0.01,
        "channel_means": {"red": 0.35, "green": 0.38, "blue": 0.45, "luma": 0.40},
    }
    warm = {
        "present": True,
        "avg_luma": 0.55,
        "warm_cool": 0.10,
        "green_magenta": 0.02,
        "sat_proxy": 0.22,
        "contrast_span": 0.48,
        "clipped_shadows": 0.01,
        "clipped_highlights": 0.02,
        "channel_means": {"red": 0.55, "green": 0.50, "blue": 0.42, "luma": 0.55},
    }
    profile = build_look_profile([cool, cool, warm])
    assert profile["present"] is True
    assert profile["samples"] == 3
    assert profile["avg_luma"] is not None

    dist = look_profile_distance(cool, warm)
    assert dist is not None and dist > 0.05

    patch = solve_grade_from_profiles(cool, warm)
    assert "temperature" in patch or "exposure" in patch
    assert abs(float(patch.get("temperature", 0))) <= 0.35

    # Nuke: after blows highlights vs before
    nuked = dict(cool)
    nuked["clipped_highlights"] = 0.20
    nuked["avg_luma"] = 0.70
    outcome = assess_grade_outcome(cool, nuked, warm, outdoor_bright=True)
    assert outcome["nuke_risk"] is True
    assert outcome["suggested_recovery_patch"]


def test_cap_color_patch_limits_jumps():
    from classes.color_agent import cap_color_patch

    capped = cap_color_patch({"exposure": 2.0, "temperature": -3.0, "saturation": 0.9})
    assert capped["exposure"] <= 0.35
    assert capped["temperature"] >= -0.35


def test_grade_writers_run_their_file_checks_off_the_gui_thread():
    from classes import tool_handlers as th

    for name in ("color_grade_clip_tool",):
        assert name in th.BACKGROUND_SAFE_TOOLS
        assert name not in th.READ_ONLY_TOOLS


def test_solo_render_canvas_keeps_the_project_aspect_ratio():
    """A 1080x1920 project used to render solo frames on a 640x360-clamped
    canvas (e.g. 304x360), pillarboxing the clip and skewing its scopes."""
    from classes.tool_handlers import _solo_render_size

    assert _solo_render_size(1920, 1080) == (640, 360)
    assert _solo_render_size(1080, 1920) == (360, 640)
    assert _solo_render_size(576, 1024) == (360, 640)
    assert _solo_render_size(320, 240) == (320, 240)


def test_resolve_relative_deltas_allows_saturation_above_one():
    """saturation neutral is 1.0; a positive delta must not clamp to [-1, 1]."""
    base = blank_color_grade("cg")
    out = resolve_relative_deltas(base, {"saturation_delta": 0.2})
    assert out["saturation"] == pytest.approx(1.2)
    merged = merge_color_grade(base, {"saturation_delta": 0.18})
    assert scalar_y(merged, "saturation") == pytest.approx(1.18)


def test_resolve_relative_deltas_clamps_temperature_to_unit_range():
    base = blank_color_grade("cg")
    out = resolve_relative_deltas(base, {"temperature_delta": 5.0})
    assert out["temperature"] == pytest.approx(1.0)


def test_grades_differ_when_only_wheels_change():
    from classes.color_agent import blank_color_grade, grades_meaningfully_differ, summarize_color_grade

    a = summarize_color_grade(blank_color_grade("a"))
    b_effect = blank_color_grade("b")
    b_effect["wheels"] = {
        "enabled_keyframes": {"Points": []},
        "global": {"color": "#224466", "amount": 0.3, "luma": 0.0},
        "shadows": {"color": "#ffffff", "amount": 0.0, "luma": 0.0},
        "midtones": {"color": "#ffffff", "amount": 0.0, "luma": 0.0},
        "highlights": {"color": "#ffffff", "amount": 0.0, "luma": 0.0},
    }
    b = summarize_color_grade(b_effect)
    assert grades_meaningfully_differ(a, b) is True

