"""Unit tests for Phase 6 colour agent helpers (no Qt / FrameScope)."""

from __future__ import annotations

import copy
import json
import os

import pytest

from classes.color_agent import (
    blank_color_grade,
    merge_color_grade,
    parse_clip_ids,
    reference_gap_hints,
    scalar_y,
    summarize_color_grade,
    summarize_scope_video,
    validate_color_patch,
)


def test_parse_clip_ids_dedupes_and_accepts_json_list():
    assert parse_clip_ids(clipIds='["a","b"]', timeline_clip_id="b") == ["a", "b"]
    assert parse_clip_ids(clip_ids="x, y", clipId="z") == ["x", "y", "z"]


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


def test_summarize_and_reference_hints():
    grade = merge_color_grade(blank_color_grade("e"), {"temperature": 0.1})
    summary = summarize_color_grade(grade)
    assert summary["present"] is True
    assert summary["temperature"] == pytest.approx(0.1)

    subject = summarize_scope_video(
        {
            "present": True,
            "summary": {"avg_luma": 0.4, "clipped_shadows": 0.0, "clipped_highlights": 0.0},
            "histogram": {
                "luma": [0] * 64 + [10] * 64 + [0] * 128,
                "red": [10] * 100 + [0] * 156,
                "green": [0] * 80 + [10] * 100 + [0] * 76,
                "blue": [0] * 120 + [10] * 100 + [0] * 36,
            },
        }
    )
    reference = summarize_scope_video(
        {
            "present": True,
            "summary": {"avg_luma": 0.55, "clipped_shadows": 0.0, "clipped_highlights": 0.0},
            "histogram": {
                "luma": [0] * 40 + [10] * 80 + [0] * 136,
                "red": [0] * 40 + [10] * 100 + [0] * 116,
                "green": [0] * 80 + [10] * 100 + [0] * 76,
                "blue": [10] * 100 + [0] * 156,
            },
        }
    )
    assert subject["present"] and reference["present"]
    for key in ("avg_luma", "clipped_shadows", "clipped_highlights", "channel_means"):
        assert key in subject
    hints = reference_gap_hints(subject, reference)
    assert "gap" in hints
    assert isinstance(hints["hints"], list)
    assert any("exposure" in h for h in hints["hints"])


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


def test_look_and_lut_catalog_ids_are_stable():
    from classes.color_agent import (
        FILM_GRAIN_LOOK_IDS,
        LOOK_PRESET_IDS,
        LUT_CATALOG,
        lut_relative_path,
    )

    assert "warm_up" in LOOK_PRESET_IDS
    assert "reset" in LOOK_PRESET_IDS
    assert "35mm_classic" in FILM_GRAIN_LOOK_IDS
    paths = [
        lut_relative_path(cat, look_id)
        for cat, looks in LUT_CATALOG
        for look_id in looks
    ]
    assert "cinematic_&_blockbuster/teal_cinema.cube" in paths
    assert len(paths) >= 40


def test_validate_before_merge_does_not_mutate():
    base = blank_color_grade("eff1")
    before = scalar_y(base, "exposure")
    with pytest.raises(ValueError):
        merge_color_grade(base, {"exposure": float("nan")})
    assert scalar_y(base, "exposure") == before
    with pytest.raises(ValueError):
        merge_color_grade(base, {"exposure": float("inf")})


def test_list_looks_catalog_filters_and_resolves():
    from classes.color_agent import (
        hints_to_color_patch,
        list_looks_catalog,
        resolve_look_id,
        resolve_lut_filesystem_path,
    )
    from classes import info

    catalog = list_looks_catalog()
    assert catalog["count"] >= 40
    kinds = {e["kind"] for e in catalog["looks"]}
    assert kinds >= {"color_preset", "lut", "film_grain"}

    warm = list_looks_catalog("warm tungsten")
    assert any(e["id"] == "warm_up" for e in warm["looks"])

    teal = list_looks_catalog("teal orange")
    assert any(e["kind"] == "lut" for e in teal["looks"])

    assert resolve_look_id("warm_up")["kind"] == "color_preset"
    lut = resolve_look_id("teal_cinema")
    assert lut["kind"] == "lut"
    assert lut["lut_path"].endswith("teal_cinema.cube")
    grain = resolve_look_id("grain:35mm_classic")
    assert grain["grain_id"] == "35mm_classic"

    with pytest.raises(ValueError, match="Unknown"):
        resolve_look_id("not_a_real_look")

    abs_path = resolve_lut_filesystem_path(
        "cinematic_&_blockbuster/teal_cinema.cube",
        colors_path=info.COLORS_PATH,
    )
    assert abs_path.endswith("teal_cinema.cube")
    assert os.path.isfile(abs_path)

    patch = hints_to_color_patch(
        [{"exposure": 0.1, "reason": "x"}, {"temperature": 0.05, "reason": "y"}]
    )
    assert patch["exposure"] == pytest.approx(0.1)
    assert patch["temperature"] == pytest.approx(0.05)


def test_apply_look_and_match_validation_without_app():
    """Handlers reject bad args before touching Qt."""
    from classes.tool_handlers import apply_look, list_looks, match_color_to_reference

    listed = json.loads(list_looks(query="cinematic"))
    assert listed["ok"] is True
    assert listed["count"] >= 1

    assert apply_look().startswith("Error:")
    assert apply_look(clipIds="c1").startswith("Error:")
    assert "Unknown lookId" in apply_look(clipIds="c1", lookId="nope")

    assert match_color_to_reference().startswith("Error:")
    assert match_color_to_reference(clipId="c1").startswith("Error:")


def test_soft_presets_match_ui_warm_up_numbers():
    from classes.color_agent import apply_soft_color_preset

    warm = apply_soft_color_preset(blank_color_grade("e"), "warm_up")
    assert scalar_y(warm, "temperature") == pytest.approx(0.18)
    assert scalar_y(warm, "tint") == pytest.approx(0.03)
    assert scalar_y(warm, "vibrance") == pytest.approx(0.10)
    sunny = apply_soft_color_preset(blank_color_grade("e"), "sunny")
    assert scalar_y(sunny, "temperature") == pytest.approx(0.12)
    assert scalar_y(sunny, "exposure") == pytest.approx(0.05)
    assert scalar_y(sunny, "saturation") == pytest.approx(1.08)
    assert scalar_y(sunny, "saturation") >= 1.0
    boost = apply_soft_color_preset(blank_color_grade("e"), "boost_color")
    assert scalar_y(boost, "saturation") == pytest.approx(1.18)
    assert len(boost["curve_all"]["nodes"]) == 4


def test_saturation_below_half_is_rejected():
    from classes.color_agent import validate_color_patch

    with pytest.raises(ValueError, match="saturation neutral is 1.0"):
        validate_color_patch({"saturation": 0.2})


def test_list_looks_finds_sunny_query():
    from classes.color_agent import list_looks_catalog

    out = list_looks_catalog("sunny")
    ids = {e["id"] for e in out["looks"]}
    assert "sunny" in ids
    assert any("sunlit" in i or i == "sunny" for i in ids)


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


def test_resolve_color_targets_all_and_playhead(monkeypatch):
    from classes import tool_handlers as th

    monkeypatch.setattr(th, "_all_timeline_clip_ids", lambda: ["a", "b"])
    monkeypatch.setattr(th, "_selected_timeline_clip_ids", lambda: [])
    monkeypatch.setattr(th, "_playhead_timeline_clip_ids", lambda: ["b"])

    assert th._resolve_color_target_ids(clipIds="all") == ["a", "b"]
    assert th._resolve_color_target_ids(all_clips=True) == ["a", "b"]
    assert th._resolve_color_target_ids() == ["b"]
    assert th._resolve_color_target_ids(timeline_clip_id="a") == ["a"]


def test_apply_look_writes_preset_under_one_transaction(monkeypatch):
    """End-to-end apply_look with stubbed Clip/app (no real openshot/Qt app)."""
    from classes import color_agent as ca
    from classes import tool_handlers as th

    class FakeUpdates:
        def __init__(self):
            self.transaction_id = None
            self.saves = []

    class FakeProject:
        def generate_id(self):
            return "gen-1"

    class FakeWindow:
        class refreshFrameSignal:
            @staticmethod
            def emit():
                return None

    class FakeApp:
        def __init__(self):
            self.updates = FakeUpdates()
            self.project = FakeProject()
            self.window = FakeWindow()

        def thread(self):
            return object()

    class FakeClip:
        def __init__(self, cid):
            self.id = cid
            self.data = {"effects": [{"class_name": "Blur", "id": "blur1"}]}
            self.saved = []

        def save(self):
            self.saved.append(copy.deepcopy(self.data))

    clips = {"c1": FakeClip("c1"), "c2": FakeClip("c2")}
    app = FakeApp()

    monkeypatch.setattr(th, "_get_app", lambda: app)
    monkeypatch.setattr(th, "QThread", None)
    monkeypatch.setattr(th, "_new_transaction_id", lambda: "tid-1")

    import classes.query as query_mod

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clips.get(id)

    monkeypatch.setattr(query_mod, "Clip", ClipProxy, raising=False)

    # Patch Clip import inside _do_look
    import types
    fake_query = types.ModuleType("classes.query")
    fake_query.Clip = ClipProxy
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    out = json.loads(th.apply_look(clipIds='["c1","c2"]', lookId="warm_up"))
    assert out["ok"] is True
    assert out["undo"] == "one step"
    assert len(out["clips"]) == 2
    for clip in clips.values():
        assert len(clip.saved) == 1
        grade = ca.find_color_grade(clip.data["effects"])
        assert grade is not None
        assert scalar_y(grade, "temperature") == pytest.approx(0.18)
        assert any(e.get("class_name") == "Blur" for e in clip.data["effects"])
    assert app.updates.transaction_id is None


def test_apply_look_lut_merges_without_wiping_exposure(monkeypatch):
    from classes import color_agent as ca
    from classes import tool_handlers as th
    import types

    class FakeUpdates:
        transaction_id = None

    class FakeProject:
        def generate_id(self):
            return "gen-lut"

    class FakeWindow:
        class refreshFrameSignal:
            @staticmethod
            def emit():
                return None

    class FakeApp:
        updates = FakeUpdates()
        project = FakeProject()
        window = FakeWindow()

        def thread(self):
            return object()

    base = ca.merge_color_grade(ca.blank_color_grade("cg1"), {"exposure": 0.25})

    class FakeClip:
        def __init__(self):
            self.id = "c1"
            self.data = {"effects": [base]}

        def save(self):
            pass

    clip = FakeClip()
    monkeypatch.setattr(th, "_get_app", lambda: FakeApp())
    monkeypatch.setattr(th, "QThread", None)
    monkeypatch.setattr(th, "_new_transaction_id", lambda: "tid")
    fake_query = types.ModuleType("classes.query")

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clip if id == "c1" else None

    fake_query.Clip = ClipProxy
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    out = json.loads(th.apply_look(clipIds="c1", lookId="teal_cinema", lutIntensity=0.55))
    assert out["ok"] is True
    grade = ca.find_color_grade(clip.data["effects"])
    assert scalar_y(grade, "exposure") == pytest.approx(0.25)
    assert scalar_y(grade, "lut_intensity") == pytest.approx(0.55)
    assert "teal_cinema.cube" in (grade.get("lut_path") or "")


def test_match_color_dry_run_builds_patch_without_apply(monkeypatch):
    import types
    from classes import tool_handlers as th
    from classes.color_agent import merge_color_grade, blank_color_grade

    inspect_payload = {
        "ok": True,
        "atFrame": 10,
        "scopes": {"present": True, "avg_luma": 0.3},
        "reference": {"scopes": {"present": True, "avg_luma": 0.5}},
        "gap": {"avg_luma": 0.2},
        "hints": [{"exposure": 0.16, "reason": "match average luma"}],
        "grades_differ": False,
        "grain_differ": False,
        "film_grain": {"present": False},
        "preview_jpeg": "",
    }

    monkeypatch.setattr(
        th,
        "inspect_color",
        lambda **_kw: json.dumps(inspect_payload),
    )
    monkeypatch.setattr(
        th,
        "_render_clip_isolated",
        lambda *_a, **_k: {"scopes": {"present": True}, "preview_jpeg": ""},
    )
    writes = []

    def _fake_write(clip_obj, effects):
        writes.append(list(effects))
        clip_obj.data["effects"] = list(effects)

    monkeypatch.setattr(th, "_write_clip_effects", _fake_write)
    monkeypatch.setattr(th, "_new_transaction_id", lambda: "tid-1")

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
            return "eff1"

        def get(self, k, d=None):
            return {"fps": {"num": 30, "den": 1}}.get(k, d)

    class FakeApp:
        project = FakeProject()
        updates = FakeUpdates()
        window = FakeWindow()

        def thread(self):
            return object()

    class FakeClip:
        def __init__(self, cid):
            self.id = cid
            self.data = {"effects": []}

    clips = {"sub": FakeClip("sub"), "ref": FakeClip("ref")}
    monkeypatch.setattr(th, "_get_app", lambda: FakeApp())
    monkeypatch.setattr(th, "QThread", None)
    fake_query = types.ModuleType("classes.query")

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clips.get(id)

    fake_query.Clip = ClipProxy
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    dry = json.loads(
        th.match_color_to_reference(
            clipId="sub", reference="ref", dry_run="true"
        )
    )
    assert dry["applied"] is False
    assert dry["proposed_patch"]["exposure"] == pytest.approx(0.16)
    assert writes == []

    live = json.loads(
        th.match_color_to_reference(clipId="sub", reference="ref")
    )
    assert live["applied"] is True
    assert live["apply_receipt"]["color"] == "scopes"
    assert writes, "expected effects write"
    grade = next(e for e in writes[0] if e.get("class_name") == "ColorGrade")
    from classes.color_agent import scalar_y
    assert scalar_y(grade, "exposure") == pytest.approx(0.16)


def test_match_removes_film_grain_when_reference_has_none(monkeypatch):
    import types
    from classes import tool_handlers as th
    from classes.film_grain_presets import apply_film_grain_preset

    grain = apply_film_grain_preset(
        {"class_name": "FilmGrain", "id": "g1"}, "35mm_classic"
    )
    inspect_payload = {
        "ok": True,
        "atFrame": 10,
        "scopes": {"present": True, "avg_luma": 0.4},
        "reference": {
            "scopes": {"present": True, "avg_luma": 0.4},
            "film_grain": {"present": False},
        },
        "gap": {"look_distance": 0.0},
        "hints": [],
        "grades_differ": False,
        "grain_differ": True,
        "film_grain": {"present": True, "amount": 0.24},
        "preview_jpeg": "",
    }
    monkeypatch.setattr(th, "inspect_color", lambda **_kw: json.dumps(inspect_payload))
    monkeypatch.setattr(
        th,
        "_render_clip_isolated",
        lambda *_a, **_k: {"scopes": {"present": True}, "preview_jpeg": ""},
    )
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
            self.data = {"effects": effects}

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
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    out = json.loads(th.match_color_to_reference(clipId="sub", reference="ref"))
    assert out["applied"] is True
    assert out["grain_mode"] == "reset"
    assert out["apply_receipt"]["grain"] == "removed"
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
    from classes.color_agent import reference_gap_hints, scope_look_distance

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
    gap = reference_gap_hints(cool, warm)
    assert gap["hints"]
    assert gap["gap"]["look_distance"] == dist


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


def test_apply_color_merge_one_transaction(monkeypatch):
    from classes import color_agent as ca
    from classes import tool_handlers as th
    import types

    class FakeUpdates:
        transaction_id = None

    class FakeProject:
        def generate_id(self):
            return "g"

    class FakeWindow:
        class refreshFrameSignal:
            @staticmethod
            def emit():
                return None

    class FakeApp:
        updates = FakeUpdates()
        project = FakeProject()
        window = FakeWindow()

        def thread(self):
            return object()

    class FakeClip:
        def __init__(self):
            self.id = "c1"
            self.data = {"effects": []}

        def save(self):
            pass

    clip = FakeClip()
    monkeypatch.setattr(th, "_get_app", lambda: FakeApp())
    monkeypatch.setattr(th, "QThread", None)
    monkeypatch.setattr(th, "_new_transaction_id", lambda: "tid")
    fake_query = types.ModuleType("classes.query")

    class ClipProxy:
        @staticmethod
        def get(id=None, **_kw):
            return clip if id == "c1" else None

    fake_query.Clip = ClipProxy
    monkeypatch.setitem(__import__("sys").modules, "classes.query", fake_query)

    out = json.loads(th.apply_color(clipIds="c1", exposure=0.2, temperature=0.1))
    assert out["ok"] is True
    grade = ca.find_color_grade(clip.data["effects"])
    assert scalar_y(grade, "exposure") == pytest.approx(0.2)
    assert scalar_y(grade, "temperature") == pytest.approx(0.1)

    # Bad args never touch clip.
    before = copy.deepcopy(clip.data)
    err = th.apply_color(clipIds="c1", exposure="bad")
    assert err.startswith("Error:")
    assert clip.data == before



def test_soft_presets_and_curves_are_the_look_menu_payloads(monkeypatch):
    """The agent builds the Look menu's exact payloads, curve nodes included.

    color_agent keeps its own copy so it imports without libopenshot; pin it to
    color_presets.py. Curve nodes written as BEZIER (0) instead of LINEAR (1)
    bent even the identity curve into an S, so every apply_look_tool preset
    crushed the shadows and blew out the highlights.
    """
    from classes import color_agent as ca
    from classes import color_presets as cp

    # The headless openshot stub has no enums; these are libopenshot's values.
    monkeypatch.setattr(cp.openshot, "LINEAR", 1, raising=False)
    monkeypatch.setattr(cp.openshot, "AUTO", 0, raising=False)

    for preset in ca.LOOK_PRESET_IDS:
        if preset == "reset":
            continue
        base = blank_color_grade("e1")
        assert ca.apply_soft_color_preset(copy.deepcopy(base), preset) == (
            cp.apply_color_grade_preset(copy.deepcopy(base), preset)
        ), preset
    assert ca.default_curve_data() == cp.default_curve_data()
    assert ca.default_wheels_data() == cp.default_wheels_data()
    nodes = ca.points_to_curve([[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]])["nodes"]
    assert {node["interpolation"] for node in nodes} == {1}
