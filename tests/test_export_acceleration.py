"""Unit tests for export acceleration (Phases 0–5) — no real Timeline required."""

from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

from classes.export_acceleration.export_tuning import (
    export_cache_bytes,
    get_export_pipeline_profile,
)
from classes.export_acceleration.hw_decode import (
    HW_NONE,
    HW_VIDEOTOOLBOX,
    detect_best_hardware_decoder,
    maybe_auto_detect_hardware_decoder,
    platform_decoder_candidates,
)
from classes.export_acceleration.hw_encode import (
    SOFTWARE_H264,
    hardware_bitrate_multiplier,
    is_hardware_encoder,
    maybe_apply_hardware_bitrate,
    platform_encoder_candidates,
)
from classes.export_acceleration.smart_render import (
    analyze_smart_render_spans,
    clip_smart_render_reasons,
)
from classes.export_acceleration.background_render import (
    BackgroundRenderManager,
    content_hash_for_segment,
)
from classes.export_acceleration.export_pipeline import (
    PipelineCancelled,
    run_pipelined_export,
)


# ---------------------------------------------------------------------------
# Phase 0 / tuning
# ---------------------------------------------------------------------------


def test_export_cache_bytes_scales_with_resolution():
    hd = export_cache_bytes(1280, 720)
    uhd = export_cache_bytes(3840, 2160)
    assert hd >= 256 * 1024 * 1024
    assert uhd > hd
    assert uhd <= 2 * 1024 * 1024 * 1024


def test_pipeline_profile_serial_on_low_core():
    profile = get_export_pipeline_profile(
        1920, 1080, 30, hardware_concurrency=2, enable_parallel_composite=True
    )
    assert profile.composite_workers == 1


def test_pipeline_profile_parallel_when_enabled():
    profile = get_export_pipeline_profile(
        1280, 720, 30, hardware_concurrency=8, enable_parallel_composite=True
    )
    assert profile.composite_workers >= 2
    assert profile.max_pending_frames >= 24


def test_pipeline_profile_parallel_disabled():
    profile = get_export_pipeline_profile(
        1280, 720, 30, hardware_concurrency=16, enable_parallel_composite=False
    )
    assert profile.composite_workers == 1


# ---------------------------------------------------------------------------
# Phase 1 — hardware decode
# ---------------------------------------------------------------------------


def test_platform_decoder_candidates_nonempty():
    assert platform_decoder_candidates()
    assert HW_NONE not in platform_decoder_candidates()


def test_detect_best_returns_none_when_all_probes_fail(monkeypatch):
    monkeypatch.setattr(
        "classes.export_acceleration.hw_decode.probe_hardware_decoder",
        lambda *a, **k: False,
    )
    assert detect_best_hardware_decoder([HW_VIDEOTOOLBOX]) == HW_NONE


def test_detect_best_returns_first_success(monkeypatch):
    monkeypatch.setattr(
        "classes.export_acceleration.hw_decode.probe_hardware_decoder",
        lambda decoder, **k: decoder == 5,
    )
    assert detect_best_hardware_decoder([2, 5, 1]) == 5


def test_maybe_auto_detect_skips_when_flag_set():
    store = MagicMock()
    store.get.side_effect = lambda key: True if key == "hardwareAutoDetectApplied" else "0"
    assert maybe_auto_detect_hardware_decoder(store) is None
    store.set.assert_not_called()


def test_maybe_auto_detect_applies_once(monkeypatch):
    store = MagicMock()
    store.get.side_effect = lambda key: False if key == "hardwareAutoDetectApplied" else "0"
    monkeypatch.setattr(
        "classes.export_acceleration.hw_decode.detect_best_hardware_decoder",
        lambda **k: 5,
    )
    assert maybe_auto_detect_hardware_decoder(store) == 5
    keys = [c.args[0] for c in store.set.call_args_list]
    assert "hw-decoder" in keys
    assert "hardwareAutoDetectApplied" in keys
    assert "decode_hw_max_width" in keys


def test_hardware_auto_detect_setting_declared_in_defaults():
    path = os.path.join(
        os.path.dirname(__file__), "..", "src", "settings", "_default.settings"
    )
    data = json.load(open(path, encoding="utf-8"))
    keys = {item["setting"] for item in data if isinstance(item, dict)}
    for required in (
        "hardwareAutoDetectApplied",
        "exportPipelined",
        "exportParallelComposite",
        "exportSmartRender",
        "exportPreferHardwareEncoder",
        "backgroundCacheWarming",
        "backgroundDiskRenders",
        "backgroundRenderReuseOnExport",
    ):
        assert required in keys, f"missing settings key: {required}"
    width = next(i for i in data if i.get("setting") == "decode_hw_max_width")
    height = next(i for i in data if i.get("setting") == "decode_hw_max_height")
    assert int(width["value"]) >= 3840
    assert int(height["value"]) >= 2160


# ---------------------------------------------------------------------------
# Phase 2 — hardware encode
# ---------------------------------------------------------------------------


def test_platform_encoder_candidates_end_with_software():
    assert platform_encoder_candidates()[-1] == SOFTWARE_H264


def test_is_hardware_encoder():
    assert is_hardware_encoder("h264_videotoolbox")
    assert is_hardware_encoder("h264_nvenc")
    assert not is_hardware_encoder("libx264")


def test_hardware_bitrate_multiplier():
    assert hardware_bitrate_multiplier("libx264") == 1.0
    assert hardware_bitrate_multiplier("h264_videotoolbox") > 1.0


def test_maybe_apply_hardware_bitrate_only_replaces_software_default(monkeypatch):
    monkeypatch.setattr(
        "classes.export_acceleration.hw_encode.get_preferred_video_encoder",
        lambda **k: "h264_videotoolbox",
    )
    out = maybe_apply_hardware_bitrate(
        {"vcodec": "libx264", "video_bitrate": 2_000_000}, prefer_hardware=True
    )
    assert out["vcodec"] == "h264_videotoolbox"
    assert out["video_bitrate"] > 2_000_000

    kept = maybe_apply_hardware_bitrate(
        {"vcodec": "libx265", "video_bitrate": 2_000_000}, prefer_hardware=True
    )
    assert kept["vcodec"] == "libx265"


# ---------------------------------------------------------------------------
# Phase 3 — pipeline (mock writer / timeline)
# ---------------------------------------------------------------------------


class _FakeFrame:
    def __init__(self, n):
        self.n = n


class _FakeTimeline:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    def GetFrame(self, n):
        self.calls.append(n)
        if self.delay:
            time.sleep(self.delay)
        return _FakeFrame(n)

    def Close(self):
        pass


class _FakeWriter:
    def __init__(self):
        self.written = []

    def WriteFrame(self, frame):
        self.written.append(frame.n)


def test_pipelined_export_preserves_order():
    timeline = _FakeTimeline()
    writer = _FakeWriter()
    metrics = run_pipelined_export(
        writer=writer,
        project_data={},
        video_settings={"width": 640, "height": 360, "fps": {"num": 30, "den": 1}},
        audio_settings={"sample_rate": 48000, "channels": 2, "channel_layout": 2},
        start_frame=1,
        end_frame=40,
        is_cancelled=lambda: False,
        existing_timeline=timeline,
        existing_cache=object(),
        profile=get_export_pipeline_profile(640, 360, 30, enable_parallel_composite=False),
    )
    assert writer.written == list(range(1, 41))
    assert metrics["frames"] == 40


def test_pipelined_export_cancel():
    timeline = _FakeTimeline(delay=0.01)
    writer = _FakeWriter()
    cancel_after = {"n": 0}

    def is_cancelled():
        cancel_after["n"] += 1
        return cancel_after["n"] > 5

    with pytest.raises(PipelineCancelled):
        run_pipelined_export(
            writer=writer,
            project_data={},
            video_settings={"width": 640, "height": 360, "fps": {"num": 30, "den": 1}},
            audio_settings={"sample_rate": 48000, "channels": 2, "channel_layout": 2},
            start_frame=1,
            end_frame=200,
            is_cancelled=is_cancelled,
            existing_timeline=timeline,
            existing_cache=object(),
            profile=get_export_pipeline_profile(640, 360, 30, enable_parallel_composite=False),
        )


# ---------------------------------------------------------------------------
# Phase 4 — smart render
# ---------------------------------------------------------------------------


def _clip(path, *, position=0.0, start=0.0, end=2.0, width=1920, height=1080, vcodec="h264"):
    return {
        "position": position,
        "start": start,
        "end": end,
        "reader": {
            "path": path,
            "width": width,
            "height": height,
            "fps": {"num": 30, "den": 1},
            "vcodec": vcodec,
        },
        "scale_x": {"Points": [{"co": {"X": 1, "Y": 1.0}}]},
        "scale_y": {"Points": [{"co": {"X": 1, "Y": 1.0}}]},
        "effects": [],
    }


def test_clip_reasons_for_effect():
    clip = _clip("/tmp/x.mp4")
    clip["effects"] = [{"type": "Brightness"}]
    reasons = clip_smart_render_reasons(
        clip, export_width=1920, export_height=1080, export_fps=30, export_vcodec="libx264"
    )
    assert "has-effects" in reasons


def test_clip_reasons_resolution_mismatch():
    clip = _clip("/tmp/x.mp4", width=1280, height=720)
    reasons = clip_smart_render_reasons(
        clip, export_width=1920, export_height=1080, export_fps=30, export_vcodec="libx264"
    )
    assert "resolution-mismatch" in reasons


def test_analyze_spans_marks_overlap_as_encode(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"fake")
    project = {
        "fps": {"num": 30, "den": 1},
        "duration": 4,
        "clips": [
            _clip(str(path), position=0.0, end=2.0),
            _clip(str(path), position=1.0, end=3.0),
        ],
        "transitions": [],
    }
    spans = analyze_smart_render_spans(
        project,
        export_width=1920,
        export_height=1080,
        export_fps=30,
        export_vcodec="libx264",
        start_frame=1,
        end_frame=90,
    )
    assert any(s.kind == "encode" for s in spans)
    # Overlap region must not be copy-only for the whole timeline
    assert not all(s.kind == "copy" for s in spans)


def test_analyze_eligible_single_clip(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"fake")
    project = {
        "fps": {"num": 30, "den": 1},
        "duration": 2,
        "clips": [_clip(str(path))],
        "transitions": [],
    }
    spans = analyze_smart_render_spans(
        project,
        export_width=1920,
        export_height=1080,
        export_fps=30,
        export_vcodec="libx264",
        start_frame=1,
        end_frame=60,
    )
    assert spans
    assert all(s.kind == "copy" for s in spans)


# ---------------------------------------------------------------------------
# Phase 5 — background render hash / budget
# ---------------------------------------------------------------------------


def test_content_hash_changes_when_effect_added():
    base = {
        "clips": [{"id": "1", "effects": []}],
        "effects": [],
        "transitions": [],
        "profile": "FHD",
    }
    h1 = content_hash_for_segment(
        base, start_frame=1, end_frame=30, width=1920, height=1080, fps={"num": 30, "den": 1}
    )
    changed = {
        "clips": [{"id": "1", "effects": [{"type": "Blur"}]}],
        "effects": [],
        "transitions": [],
        "profile": "FHD",
    }
    h2 = content_hash_for_segment(
        changed, start_frame=1, end_frame=30, width=1920, height=1080, fps={"num": 30, "den": 1}
    )
    assert h1 != h2


def test_background_manager_budget_eviction(tmp_path):
    root = tmp_path / "renders"
    root.mkdir()
    # Create fake files over budget
    for i in range(5):
        p = root / f"old{i}.json"
        p.write_text("x" * 100)
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))

    mgr = BackgroundRenderManager(
        get_timeline=lambda: None,
        get_project_data=lambda: {},
        is_user_busy=lambda: True,
        enabled_warm=False,
        enabled_disk=True,
        disk_budget_bytes=250,
        render_root=str(root),
    )
    mgr._enforce_budget()
    remaining = list(root.iterdir())
    assert sum(f.stat().st_size for f in remaining) <= 250
