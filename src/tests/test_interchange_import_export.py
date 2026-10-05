"""
 @file
 @brief Unit tests for EDL import/export behavior (FCP XML: tests/test_premiere_*.py).
"""

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Project:
    def __init__(self, **overrides):
        self.current_filepath = overrides.pop("current_filepath", "")
        self._data = {
            "fps": {"num": 24, "den": 1},
            "width": 1920,
            "height": 1080,
            "sample_rate": 48000,
            "channels": 2,
            "layers": [{"number": 1, "label": "Main", "lock": False}],
            "id": "project-1",
            "pixel_ratio": {"num": 1, "den": 1},
            "interlaced_frame": False,
        }
        self._data.update(overrides)

    def get(self, key):
        return self._data.get(key)


class _App:
    def __init__(self, project=None):
        self.project = project or _Project()
        self.window = types.SimpleNamespace(
            refreshFrameSignal=_Signal(),
            propertyTableView=types.SimpleNamespace(select_frame=lambda *_: None),
            preview_thread=types.SimpleNamespace(player=types.SimpleNamespace(Position=lambda: 0)),
        )

    def _tr(self, value):
        return value


class _TrackRecord:
    saved = []

    def __init__(self, number=None, data=None):
        self.data = data or {}
        if number is not None:
            self.data.setdefault("number", number)

    def save(self):
        self.__class__.saved.append(self)


class _ClipRecord:
    saved = []

    def __init__(self, data=None):
        self.data = data or {}

    def save(self):
        self.__class__.saved.append(self)


class _FileRecord:
    def __init__(self, file_id, path, media_type="video", **data):
        self.id = file_id
        self.data = {
            "id": file_id,
            "path": path,
            "name": os.path.basename(path),
            "media_type": media_type,
            "duration": 10.0,
            "width": 1920,
            "height": 1080,
            "fps": {"num": 24, "den": 1},
            "sample_rate": 48000,
            "channels": 2,
        }
        self.data.update(data)

    def absolute_path(self):
        return self.data["path"]


class _FileQuery:
    by_id = {}
    by_path = {}

    @classmethod
    def reset(cls, files):
        cls.by_id = {f.id: f for f in files}
        cls.by_path = {f.data["path"]: f for f in files}

    @classmethod
    def get(cls, **kwargs):
        if "id" in kwargs:
            return cls.by_id.get(kwargs["id"])
        if "path" in kwargs:
            return cls.by_path.get(kwargs["path"])
        return None


class _OpenShotClip:
    def __init__(self, path):
        self.path = path

    def Json(self):
        has_audio = self.path.lower().endswith((".wav", ".mp3", ".aac"))
        has_video = not has_audio
        return json.dumps({
            "id": "clip-from-reader",
            "reader": {"path": self.path, "has_audio": has_audio, "has_video": has_video},
        })

    def Reader(self):
        media_type = "audio" if self.path.lower().endswith((".wav", ".mp3", ".aac")) else "video"
        return types.SimpleNamespace(Json=lambda: json.dumps({
            "path": self.path,
            "media_type": media_type,
            "has_audio": media_type == "audio",
            "has_video": media_type == "video",
        }))


class InterchangeImportExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edl_importer = importlib.import_module("classes.importers.edl")
        cls.edl_exporter = importlib.import_module("classes.exporters.edl")

    def setUp(self):
        _TrackRecord.saved = []
        _ClipRecord.saved = []
        _FileQuery.reset([])

    def test_edl_create_clip_sets_svg_audio_thumbnail_for_audio_only_clip(self):
        audio_path = "/tmp/audio.wav"
        _FileQuery.reset([_FileRecord("audio-file", audio_path, media_type="audio")])
        app = _App()
        track = types.SimpleNamespace(data={"number": 7})
        context = {
            "clip_path": audio_path,
            "clip_title": "Audio Clip",
            "audio_ctx": [{
                "reel": "AX",
                "clip_start_time": "00:00:00:00",
                "clip_end_time": "00:00:01:00",
                "timeline_position": "00:00:02:00",
            }],
        }

        with patch.object(self.edl_importer, "get_app", return_value=app), \
             patch.object(self.edl_importer, "find_missing_file", return_value=(audio_path, False, False)), \
             patch.object(self.edl_importer, "File", _FileQuery), \
             patch.object(self.edl_importer, "Clip", _ClipRecord), \
             patch.object(self.edl_importer.openshot, "Clip", _OpenShotClip):
            self.edl_importer.create_clip(context, track)

        self.assertEqual(len(_ClipRecord.saved), 1)
        clip_data = _ClipRecord.saved[0].data
        self.assertEqual(clip_data["file_id"], "audio-file")
        self.assertEqual(clip_data["layer"], 7)
        self.assertFalse(clip_data["has_video"]["Points"][0]["co"]["Y"])
        self.assertTrue(clip_data["image"].endswith(os.path.join("images", "AudioThumbnail.svg")))

    def test_edl_import_parses_grouped_clip_source_and_keyframe_comments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = os.path.join(tmpdir, "clip.mp4")
            edl_path = os.path.join(tmpdir, "input.edl")
            with open(edl_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "TITLE: Test Sequence\n"
                    "FCM: NON-DROP FRAME\n\n"
                    "001  AX       V     C        00:00:00:00 00:00:01:00 00:00:02:00 00:00:03:00\n"
                    "001  AX       A     C        00:00:00:00 00:00:01:00 00:00:02:00 00:00:03:00\n"
                    "* FROM CLIP NAME: clip.mp4\n"
                    "* SOURCE FILE: clip.mp4\n"
                    "* VIDEO LEVEL AT 00:00:00:12 IS 50% BEZIER\n"
                    "* AUDIO LEVEL AT 00:00:00:12 IS -6.00 DB HOLD\n"
                    "* SCALE X AT 00:00:00:12 IS 125% LINEAR\n"
                )

            contexts = []
            app = _App(_Project(layers=[{"number": 1, "label": "Existing"}]))

            with patch.object(self.edl_importer, "get_app", return_value=app), \
                 patch.object(self.edl_importer.QFileDialog, "getOpenFileName", return_value=(edl_path, "")), \
                 patch.object(self.edl_importer, "Track", _TrackRecord), \
                 patch.object(self.edl_importer, "create_clip", side_effect=lambda ctx, track: contexts.append(dict(ctx))):
                self.edl_importer.import_edl()

        self.assertEqual(len(_TrackRecord.saved), 1)
        self.assertEqual(len(contexts), 1)
        ctx = contexts[0]
        self.assertEqual(ctx["clip_path"], media_path)
        self.assertEqual(ctx["video_ctx"]["timeline_position"], "00:00:02:00")
        self.assertEqual(ctx["audio_ctx"][0]["clip_end_time"], "00:00:01:00")
        self.assertAlmostEqual(ctx["opacity"][0]["value"], 0.5)
        self.assertAlmostEqual(ctx["volume"][0]["value"], self.edl_importer._db_to_volume(-6.0), places=4)
        self.assertAlmostEqual(ctx["scale_x"][0]["value"], 1.25)

    def test_edl_export_writes_tracks_gap_media_rows_and_keyframe_comments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = os.path.join(tmpdir, "media.mp4")
            out_base = os.path.join(tmpdir, "out.edl")
            _FileQuery.reset([_FileRecord("file-1", media_path, media_type="video")])
            app = _App(_Project(fps={"num": 24, "den": 1}, layers=[{"number": 1, "label": "Main"}]))
            clip = _ClipRecord({
                "id": "clip-1",
                "file_id": "file-1",
                "title": "Clip One",
                "position": 1.0,
                "start": 0.0,
                "end": 2.0,
                "reel": "R1",
                "reader": {"path": media_path, "has_video": True, "has_audio": True},
                "alpha": {"Points": [{"co": {"X": 1, "Y": 0.5}, "interpolation": 0}]},
                "volume": {"Points": [{"co": {"X": 1, "Y": 0.5}, "interpolation": 2}]},
                "scale_x": {"Points": [{"co": {"X": 1, "Y": 1.25}, "interpolation": 1}]},
            })

            with patch.object(self.edl_exporter, "get_app", return_value=app), \
                 patch.object(self.edl_exporter.QFileDialog, "getSaveFileName", return_value=(out_base, "")), \
                 patch.object(self.edl_exporter, "File", _FileQuery), \
                 patch.object(self.edl_exporter.Track, "get", return_value=_TrackRecord(number=1)), \
                 patch.object(self.edl_exporter.Clip, "filter", return_value=[clip]):
                self.edl_exporter.export_edl()

            exported_path = os.path.join(tmpdir, "out-Main.edl")
            with open(exported_path, "r", encoding="utf-8") as handle:
                exported = handle.read()

        self.assertIn("TITLE: out - Main", exported)
        self.assertIn("FCM: NON-DROP FRAME", exported)
        self.assertIn("001  BL", exported)
        self.assertIn("002  R1       V", exported)
        self.assertIn("002  R1       A", exported)
        self.assertIn("* FROM CLIP NAME: Clip One", exported)
        self.assertIn("* SOURCE FILE: media.mp4", exported)
        self.assertIn("* VIDEO LEVEL AT 00:00:00:00 IS 50% BEZIER", exported)
        self.assertIn("* AUDIO LEVEL AT 00:00:00:00 IS -6.02 DB HOLD", exported)
        self.assertIn("* SCALE X AT 00:00:00:00 IS 125% LINEAR", exported)

    # The Final Cut Pro XML (FCP7 / Premiere) exporter and importer are covered headlessly by
    # tests/test_premiere_export.py and tests/test_premiere_import.py.


if __name__ == "__main__":
    unittest.main()
