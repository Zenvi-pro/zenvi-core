"""Optimize Preview against real libopenshot: transcode a sample clip into a proxy.

Runs only with ``ZENVI_REAL_QT=1`` (real PyQt5) and an importable ``openshot``
(``PYTHONPATH=$ZENVI_DEPS/python``).  Needs ``ffmpeg`` on PATH to synthesize the
3-second 1080p sample; skips otherwise.
"""

import json
import os
import shutil
import subprocess
import types

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

from PyQt5.QtCore import QObject  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from classes import info  # noqa: E402
from classes import proxy_service  # noqa: E402
from classes.proxy_service import ProxyService  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not on PATH")
    path = tmp_path_factory.mktemp("media") / "sample_1080p.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return str(path)


def _reader_json(path):
    clip = openshot.Clip(path)
    clip.Open()
    try:
        return json.loads(clip.Reader().Json())
    finally:
        clip.Close()


def test_build_proxy_reader_writes_720p_proxy_and_prewarms_thumbnails(qapp, sample_video, tmp_path, monkeypatch):
    settings = types.SimpleNamespace(values={"optimize-preview-max-size": "1280x720", "optimize-preview-jobs": 1,
                                             "optimize-preview-thumbnails": 4})
    settings.get = lambda key: settings.values.get(key)
    fake_app = types.SimpleNamespace(settings=settings, project=types.SimpleNamespace(current_filepath=""),
                                     updates=types.SimpleNamespace(transaction_id=None), window=None)
    fake_app.get_settings = lambda: settings
    fake_app._tr = lambda text: text
    fake_app.devicePixelRatio = lambda: 1.0
    monkeypatch.setattr(proxy_service, "get_app", lambda: fake_app)
    monkeypatch.setattr("classes.thumbnail.get_app", lambda: fake_app)
    proxy_root = tmp_path / "optimized"
    thumb_root = tmp_path / "thumbnail"
    proxy_root.mkdir()
    monkeypatch.setattr(info, "PROXY_PATH", str(proxy_root))
    monkeypatch.setattr(info, "THUMBNAIL_PATH", str(thumb_root))

    win = QObject()
    win.statusBar = None
    win.timeline_sync = None
    service = ProxyService(win)
    try:
        source = _reader_json(sample_video)
        source["path"] = sample_video
        source["media_type"] = "video"
        # Mark the job so progress bookkeeping runs like the real executor path
        service._jobs["F1"] = {"id": "F1", "status": "queued", "progress": 0, "cancel_requested": False,
                               "output_path": str(proxy_root / "sample_1080p_proxy.mp4")}
        proxy_reader = service._build_proxy_reader("F1", source)
    finally:
        service.shutdown()

    assert proxy_reader["id"] == "F1"
    assert os.path.isfile(proxy_reader["path"])
    assert os.path.dirname(proxy_reader["path"]) == str(proxy_root)
    assert (proxy_reader["width"], proxy_reader["height"]) == (1280, 720)
    assert proxy_reader["fps"] == source["fps"]
    assert proxy_reader["has_audio"] is True
    assert abs(float(proxy_reader["duration"]) - float(source["duration"])) < 0.2

    # Thumbnails were pre-warmed on the 4/sec grid (30fps -> every 8 frames) into Zenvi's thumbnail layout
    written = sorted(int(os.path.splitext(name)[0]) for name in os.listdir(thumb_root / "F1"))
    assert written[:3] == [1, 9, 17]
    assert written[-1] == int(source["video_length"])

    # The proxy substitutes the clip reader for playback, never the File record itself
    payload = {"files": [{"id": "F1", "path": sample_video, "proxy_reader": proxy_reader}],
               "clips": [{"id": "C1", "file_id": "F1", "reader": dict(source, id="F1")}]}
    rewritten = service.rewrite_json_for_preview(payload)
    assert rewritten["clips"][0]["reader"]["path"] == proxy_reader["path"]
    assert rewritten["files"][0]["path"] == sample_video
