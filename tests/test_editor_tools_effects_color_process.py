"""effects-color editor tools: frame analysis and the processing effects (Stabilizer, Tracker, ...)."""

import copy
import json

import pytest

from classes import effect_models
from classes.editor_tools import effects_color_analysis as analysis
from classes.editor_tools import effects_color_process as proc
from effects_color_helpers import effect_of, install_effect_fixtures, receipt


@pytest.fixture
def fx(editor, monkeypatch):
    return install_effect_fixtures(editor, monkeypatch)


# ---------------------------------------------------------------------------
# Frame statistics
# ---------------------------------------------------------------------------

def _hist(pairs):
    h = [0] * 256
    for value, count in pairs:
        h[value] += count
    return h


def _vec(size, points):
    v = [0] * (size * size)
    for (x, y), n in points:
        v[y * size + x] += n
    return v


def test_stats_of_a_gray_frame():
    size = 129
    gray = _hist([(128, 1000)])
    d = {"luma": gray, "red": gray, "green": gray, "blue": gray, "vectorscope": _vec(size, [((64, 64), 1000)]),
         "vectorscope_size": size, "clipped_shadows": 0, "clipped_highlights": 0, "total_pixels": 1000}
    s = analysis.compute_stats(d)
    assert s["luma"]["p50"] == pytest.approx(0.502, abs=0.002) and s["gray_pct"] == 100.0
    assert s["cast"] == "neutral" and s["transparent_pct"] == 0.0
    assert "black and white" in s["verdict"] and s["suggested_grade"] == {"contrast": 0.2}


def test_stats_of_a_dark_warm_frame_suggest_fixes():
    size = 129
    d = {"luma": _hist([(20, 900), (40, 100)]), "red": _hist([(50, 1000)]), "green": _hist([(25, 1000)]),
         "blue": _hist([(5, 1000)]), "vectorscope": _vec(size, [((40, 80), 1000)]), "vectorscope_size": size,
         "clipped_shadows": 100, "clipped_highlights": 0, "total_pixels": 2000}
    s = analysis.compute_stats(d)
    assert s["warmth"] > 0.1 and s["cast"].startswith("warm")
    assert any(v.startswith("underexposed") for v in s["verdict"])
    assert s["suggested_grade"]["exposure"] > 1 and s["suggested_grade"]["temperature"] < 0
    assert s["transparent_pct"] == 50.0


class _FakeFrame:
    def __init__(self):
        self.saved = None

    def GetWidth(self):
        return 640

    def GetHeight(self):
        return 360

    def Save(self, path, scale, fmt, quality):
        self.saved = path


def test_analyze_tool_renders_clip_alone_and_compares(fx, monkeypatch, tmp_path):
    c = fx.add_clip(fx.add_file("video"), position=4.0, start=1.0, end=5.0)
    fx.add_effect(c, "Negate")
    calls = []
    frame = _FakeFrame()

    class _R:
        def __init__(self):
            self.frame = frame

        def close(self):
            calls.append("closed")

    def render(clip_data, t, strip=None):
        calls.append((clip_data["id"] if clip_data else None, t, strip))
        return _R()

    bright = _hist([(200, 10)])
    monkeypatch.setattr(analysis, "render_frame", render)
    monkeypatch.setattr(analysis, "frame_pixels", lambda f, r=None: [(200, 200, 200, 255)] * 30)
    monkeypatch.setattr(analysis, "scope_data", lambda f, r=None: {
        "luma": bright, "red": bright, "green": bright, "blue": bright, "vectorscope": [], "vectorscope_size": 0,
        "clipped_shadows": 0, "clipped_highlights": 0, "total_pixels": 10})
    png = str(tmp_path / "f.png")
    r = receipt(fx.call("analyze_frame_colors_tool", timeline_clip_id=c, compare_without_effects=True,
                        save_frame_path=png, region={"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}))
    assert calls[0] == (c, 6.0, None) and calls[2] == (c, 6.0, "all") and calls.count("closed") == 2
    assert frame.saved == png and r["stats"]["luma"]["p50"] == pytest.approx(0.784, abs=0.002)
    assert r["without_effects"] and r["effects"] == ["Negate"] and r["frame_size"] == [640, 360]
    for kwargs, needle in (({"time": 20}, "outside the clip"), ({"save_frame_path": "rel.png"}, "absolute"),
                           ({"region": {"x": 0.9, "y": 0, "width": 0.5, "height": 1}}, "inside the frame")):
        out = fx.call("analyze_frame_colors_tool", timeline_clip_id=c, **kwargs)
        assert out.startswith("Error") and needle in out, out
    assert fx.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# Processing effects
# ---------------------------------------------------------------------------

@pytest.fixture
def jobs(fx, monkeypatch):
    """Processing with libopenshot's job replaced: records the context, returns the effect JSON."""
    seen = {}

    def run_job(class_name, clip_data, context, prompts, timeout):
        seen.update(class_name=class_name, context=context, prompts=prompts, timeout=timeout)
        e = copy.deepcopy(fx.effect_fixture(class_name))
        e["id"] = "PROC1"
        e["protobuf_data_path"] = "/tmp/PROC1.data"
        if class_name == "Tracker":
            e["objects"] = {"PROC1-0": {"box_id": "PROC1-0"}}
        e["_seconds"] = 1.5
        return e

    monkeypatch.setattr(proc, "opencv_available", lambda: True)
    monkeypatch.setattr(proc, "run_job", run_job)
    return seen


def test_stabilizer_through_add_effect_tool(fx, jobs):
    c = fx.add_clip(fx.add_file("video"))
    r = receipt(fx.call("add_effect_tool", timeline_clip_id=c, effect="stabilize",
                        processing={"smoothing": 45}, properties={"zoom": 1.2}))
    assert jobs["context"] == {"smoothing-window": 45.0} and r["seconds"] == 1.5
    st = effect_of(fx, c, "Stabilizer")[0]
    assert st["id"] == "PROC1" and st["zoom"]["Points"][0]["co"]["Y"] == 1.2
    assert fx.undo_steps_since_mark() == 1
    fx.undo()
    assert effect_of(fx, c, "Stabilizer") == []


def test_tracker_region_and_first_frame(fx, jobs):
    c = fx.add_clip(fx.add_file("video"), position=10.0, start=2.0, end=12.0)
    r = receipt(fx.call("process_clip_effect_tool", timeline_clip_id=c, effect="Tracker", tracker_type="csrt",
                        region={"x": 0.4, "y": 0.3, "width": 0.2, "height": 0.25}, region_time=12.0))
    region = jobs["context"]["region"]
    assert region["normalized_x"] == 0.4 and region["normalized_height"] == 0.25
    assert region["first-frame"] == 60 and jobs["context"]["tracker-type"] == "CSRT"
    assert r["tracked_object_ids"] == ["PROC1-0"]


@pytest.mark.parametrize("kwargs, needle", [
    ({"effect": "Tracker"}, "needs region"),
    ({"effect": "Tracker", "region": {"x": 0.9, "y": 0.1, "width": 0.3, "height": 0.2}}, "inside the frame"),
    ({"effect": "Stabilizer", "smoothing": 0}, ">= 1"),
    ({"effect": "ObjectMask"}, "at least one point"),
    ({"effect": "Tracker", "region": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}, "region_time": 99},
     "outside the clip"),
])
def test_processing_refusals(fx, jobs, kwargs, needle):
    c = fx.add_clip(fx.add_file("video"))
    out = fx.call("process_clip_effect_tool", timeline_clip_id=c, **kwargs)
    assert out.startswith("Error") and needle in out, out
    assert "context" not in jobs and fx.undo_steps_since_mark() == 0


def test_processing_refuses_images_and_missing_opencv(fx, jobs, monkeypatch):
    img = fx.add_clip(fx.add_file("image"))
    out = fx.call("process_clip_effect_tool", timeline_clip_id=img, effect="Stabilizer")
    assert out.startswith("Error") and "still image" in out
    v = fx.add_clip(fx.add_file("video"), layer=2000000)
    monkeypatch.setattr(proc, "opencv_available", lambda: False)
    out = fx.call("process_clip_effect_tool", timeline_clip_id=v, effect="Stabilizer")
    assert out.startswith("Error") and "OpenCV" in out
    assert fx.undo_steps_since_mark() == 0


def test_object_detection_needs_model_download_opt_in(fx, jobs, monkeypatch):
    c = fx.add_clip(fx.add_file("video"))
    monkeypatch.setattr(effect_models, "yolo_installed_files_match", lambda model: False)
    out = fx.call("process_clip_effect_tool", timeline_clip_id=c, effect="ObjectDetection")
    assert out.startswith("Error") and "download_model=true" in out and "context" not in jobs
    downloads = []

    def fake_download(manifest, model, report_progress=None, **kw):
        downloads.append(model["id"])
        monkeypatch.setattr(effect_models, "yolo_installed_files_match", lambda m: True)

    monkeypatch.setattr(effect_models, "download_yolo_model", fake_download)
    r = receipt(fx.call("process_clip_effect_tool", timeline_clip_id=c, effect="ObjectDetection",
                        download_model=True, class_filter="person", confidence=0.4))
    assert downloads == ["yolo26n-seg"] and jobs["context"]["model"].endswith("yolo26n-seg/model.onnx")
    det = effect_of(fx, c, "ObjectDetection")[0]
    assert det["class_filter"] == "person" and det["confidence_threshold"] == 0.4
    assert r["notes"] == ["downloaded YOLO26: Nano"]


def test_object_mask_prompts_in_source_pixels():
    ctx = proc.object_mask_prompt_context(
        {"frame": 31, "positive_points": [{"x": 0.5, "y": 0.25}], "negative_points": [],
         "positive_rects": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}]}, 1920, 1080)
    assert ctx["positive_x"] == 960 and ctx["positive_y"] == 270
    assert ctx["rect_x1"] == pytest.approx(192) and ctx["rect_y2"] == pytest.approx(648)
    assert ctx["object_mask_selection"]["frames"]["31"]["positive_points"] == [{"x": 960.0, "y": 270.0}]
    assert json.dumps(ctx)


def test_pixel_stats_white_balance_and_saturation():
    warm_greys = [(160, 150, 130, 255)] * 90 + [(250, 20, 20, 255)] * 10 + [(0, 0, 0, 0)] * 50
    p = analysis.pixel_stats(warm_greys)
    assert p["neutral_warmth"] == pytest.approx(0.118, abs=0.002) and p["neutral_pct"] == 90.0
    assert p["saturation_p90"] > 0.9 and 0.2 < p["saturation_mean"] < 0.3
    gray = _hist([(150, 100)])
    d = {"luma": gray, "red": gray, "green": gray, "blue": gray, "vectorscope": [], "vectorscope_size": 0,
         "clipped_shadows": 0, "clipped_highlights": 0, "total_pixels": 100}
    s = analysis.compute_stats(d, p)
    assert s["cast"] == "warm (orange)" and s["suggested_grade"]["temperature"] < 0
    assert analysis.pixel_stats([(0, 0, 0, 0)]) is None


def test_yolo_download_verifies_installs_and_cleans_up(tmp_path, monkeypatch):
    import hashlib
    import io
    import os
    import zipfile
    from classes import http_client, info

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pkg/model.onnx", b"onnx-bytes")
        z.writestr("pkg/coco.names", b"person\ncar\n")
    archive = buf.getvalue()
    model = {"id": "yolo-test", "name": "YOLO test", "asset": "yolo-test.zip",
             "sha256": hashlib.sha256(archive).hexdigest()}
    manifest = {"base_url": "https://example.invalid/models", "models": [model]}
    monkeypatch.setattr(info, "YOLO_PATH", str(tmp_path))
    urls = []

    def fake_download(url, path, label, report_progress=None, cancel_exceptions=()):
        urls.append(url)
        with open(path, "wb") as fh:
            fh.write(archive)

    monkeypatch.setattr(http_client, "download_file", fake_download)
    effect_models.download_yolo_model(manifest, model)
    assert urls == ["https://example.invalid/models/yolo-test.zip"]
    assert open(effect_models.yolo_model_path(model), "rb").read() == b"onnx-bytes"
    assert effect_models.yolo_installed_files_match(model)
    assert sorted(os.listdir(tmp_path / "yolo-test")) == ["classes.names", "install.json", "model.onnx"]

    bad = dict(model, id="yolo-bad", sha256="0" * 64)
    with pytest.raises(ValueError):
        effect_models.download_yolo_model(manifest, bad)
    assert os.listdir(tmp_path / "yolo-bad") == [] and not effect_models.yolo_installed_files_match(bad)
