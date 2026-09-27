"""Thumbnail generation fallbacks (OpenShot #6030, adapted to Zenvi).

Zenvi keeps its ffmpeg fallback and rotation metadata handling and must run
against libopenshot 0.5.0 (no ``Clip.CreateReader``) as well as 1.0.
"""

from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import classes.thumbnail as thumbnail  # noqa: E402


class _Frame:
    def __init__(self, reject_scale_mode=False):
        self.calls = []
        self.reject_scale_mode = reject_scale_mode

    def Thumbnail(self, *args):
        if self.reject_scale_mode and len(args) > 10:
            raise TypeError("Wrong number or type of arguments for overloaded function 'Frame_Thumbnail'")
        self.calls.append(args)


class _Metadata(dict):
    def count(self, key):
        return 1 if key in self else 0


class _Reader:
    def __init__(self, *, open_error=None, metadata=None, reject_scale_mode=False):
        self.open_error = open_error
        self.open_calls = 0
        self.close_calls = 0
        self.decode_sizes = []
        self.frames = []
        self.info = SimpleNamespace(metadata=_Metadata(metadata or {}))
        self.frame = _Frame(reject_scale_mode=reject_scale_mode)

    def SetMaxDecodeSize(self, width, height):
        self.decode_sizes.append((width, height))

    def Open(self):
        self.open_calls += 1
        if self.open_error:
            raise self.open_error

    def GetFrame(self, number):
        self.frames.append(number)
        return self.frame

    def Close(self):
        self.close_calls += 1


@pytest.fixture
def fake_openshot(monkeypatch):
    """Install a fake ``openshot`` module on classes.thumbnail with a scriptable Clip."""
    module = types.ModuleType("openshot")
    module.SCALE_CROP = 1
    monkeypatch.setattr(thumbnail, "openshot", module)
    monkeypatch.setattr(thumbnail.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(thumbnail, "_ensure_thumb_dir", lambda path: None)
    monkeypatch.setattr(thumbnail, "_generate_thumbnail_ffmpeg", lambda *a, **k: False)
    return module


def _install_create_reader(module, readers):
    created = []
    reader_iter = iter(readers)

    def create_reader(path, inspect_reader):
        created.append((path, inspect_reader))
        return next(reader_iter)

    module.Clip = SimpleNamespace(CreateReader=create_reader)
    return created


def test_retries_with_eager_inspection_when_quick_reader_fails(fake_openshot):
    first = _Reader(open_error=RuntimeError("QtImageReader could not open image file."))
    second = _Reader()
    created = _install_create_reader(fake_openshot, [first, second])

    thumbnail.GenerateThumbnail("image.webp", "/tmp/thumb.png", 1, 20, 20, None, None)

    assert created == [("image.webp", False), ("image.webp", True)]
    assert (first.open_calls, second.open_calls) == (1, 1)
    assert second.decode_sizes == [(60, 60)]
    assert second.frames == [1]
    assert (first.close_calls, second.close_calls) == (1, 1)
    assert second.frame.calls[0][:3] == ("/tmp/thumb.png", 20, 20)
    assert second.frame.calls[0][-1] == fake_openshot.SCALE_CROP


def test_renders_svg_placeholder_when_source_cannot_open(fake_openshot):
    failing = [_Reader(open_error=RuntimeError("missing source")) for _ in range(2)]
    placeholder = _Reader()
    created = _install_create_reader(fake_openshot, [*failing, placeholder])

    thumbnail.GenerateThumbnail("missing.webp", "/tmp/thumb.png", 7, 20, 20, None, None)

    assert created[:2] == [("missing.webp", False), ("missing.webp", True)]
    assert created[2] == (os.path.join(thumbnail.info.IMAGES_PATH, "NotFound.svg"), False)
    assert placeholder.frames == [1]
    assert placeholder.frame.calls[0][:3] == ("/tmp/thumb.png", 20, 20)


def test_legacy_libopenshot_uses_single_clip_reader_and_rotation(fake_openshot):
    """libopenshot 0.5.0: no CreateReader, no SetMaxDecodeSize, 10-arg Thumbnail()."""
    class LegacyReader(_Reader):
        SetMaxDecodeSize = property(lambda self: (_ for _ in ()).throw(AttributeError("SetMaxDecodeSize")))

    reader = LegacyReader(metadata={"rotate": "90"}, reject_scale_mode=True)
    clips = []

    class Clip:
        def __init__(self, path):
            clips.append(path)

        def Reader(self):
            return reader

        def Close(self):
            clips.append("closed")

    fake_openshot.Clip = Clip

    thumbnail.GenerateThumbnail("portrait.mov", "/tmp/thumb.png", 3, 100, 65, "mask", "overlay")

    assert clips == ["portrait.mov", "closed"]
    assert reader.frames == [3]
    assert len(reader.frame.calls) == 1
    call = reader.frame.calls[0]
    assert len(call) == 10  # scale-mode argument dropped after TypeError
    assert call[:5] == ("/tmp/thumb.png", 100, 65, "mask", "overlay")
    assert call[-1] == 90.0


def test_reader_applied_orientation_skips_metadata_rotation(fake_openshot):
    """libopenshot >= 1.0 readers rotate frames themselves; do not rotate twice."""
    reader = _Reader(metadata={"rotate": "90"})
    reader.ApplyOrientationMetadata = lambda: True
    _install_create_reader(fake_openshot, [reader])

    thumbnail.GenerateThumbnail("portrait.mov", "/tmp/thumb.png", 1, 100, 65, None, None)

    call = reader.frame.calls[0]
    assert call[9] == 0.0
    assert call[-1] == fake_openshot.SCALE_CROP


def test_ffmpeg_fallback_runs_before_placeholder(fake_openshot, monkeypatch):
    _install_create_reader(fake_openshot, [_Reader(open_error=RuntimeError("x"))] * 2)
    calls = []
    monkeypatch.setattr(thumbnail, "_generate_thumbnail_ffmpeg", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(thumbnail, "_write_not_found_thumbnail", lambda *a: calls.append("placeholder"))

    thumbnail.GenerateThumbnail("odd.bin", "/tmp/thumb.png", 1, 20, 20, None, None)

    assert calls == [("odd.bin", "/tmp/thumb.png", 1, 20, 20)]


def test_http_handler_uses_project_file_icon_size(monkeypatch):
    handler = thumbnail.httpThumbnailHandler.__new__(thumbnail.httpThumbnailHandler)
    handler.path = "/thumbnails/file-id/1/path/"
    handler.wfile = SimpleNamespace(write=lambda *_: None)
    handler.send_response_only = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    handler.send_error = mock.Mock()
    file_record = SimpleNamespace(
        data={"media_type": "video", "fingerprint": None},
        absolute_path=lambda: "/tmp/dummy.webp",
    )
    generate = mock.Mock()
    monkeypatch.setattr(thumbnail, "File", SimpleNamespace(get=lambda **kw: file_record))
    monkeypatch.setattr(thumbnail, "GenerateThumbnail", generate)
    monkeypatch.setattr(thumbnail, "resolve_thumbnail_path", lambda *a, **k: None)
    monkeypatch.setattr(thumbnail, "preferred_thumbnail_path", lambda *a, **k: "/tmp/thumbs/file-id-1.png")
    monkeypatch.setattr(thumbnail.info, "LIST_ICON_SIZE", SimpleNamespace(width=lambda: 100, height=lambda: 65))
    monkeypatch.setattr(thumbnail.os.path, "exists", lambda path: False)
    monkeypatch.setattr(thumbnail.time, "sleep", lambda *_: None)

    handler.do_GET()

    assert generate.call_count == 1
    args = generate.call_args[0]
    assert (args[3], args[4]) == (100, 65)
