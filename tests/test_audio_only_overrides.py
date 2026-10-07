"""Audio-only media must never composite a video frame over lower layers.

Cover-art MP3s are the bug: libopenshot reports has_video=True for them, so the
clip paints an opaque frame and blacks out everything underneath.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

from classes.clip_placement import apply_audio_only_clip_overrides
from classes.image_types import _AUDIO_EXTS, is_audio_only_media

# Stand-ins for openshot.CONSTANT / openshot.SCALE_NONE so the helper stays
# importable without libopenshot.
CONSTANT = 2
SCALE_NONE = 3


def _apply(clip_data, file_data):
    return apply_audio_only_clip_overrides(
        clip_data, file_data, constant_interpolation=CONSTANT, scale_none=SCALE_NONE
    )


def _cover_art_mp3():
    """The failing case: reader claims a video stream because of embedded art."""
    return {"path": "/media/theme.mp3", "has_audio": True, "has_video": True}


def _plain_mp3():
    return {"path": "/media/pad.mp3", "has_audio": True, "has_video": False}


def _video():
    return {"path": "/media/interview.mp4", "has_audio": True, "has_video": True}


# --------------------------------------------------------------------------
# is_audio_only_media
# --------------------------------------------------------------------------

def test_cover_art_mp3_is_audio_only_despite_has_video():
    assert is_audio_only_media(_cover_art_mp3()) is True


def test_plain_mp3_is_audio_only():
    assert is_audio_only_media(_plain_mp3()) is True


def test_video_is_not_audio_only():
    assert is_audio_only_media(_video()) is False


@pytest.mark.parametrize("ext", _AUDIO_EXTS)
def test_every_known_audio_extension_counts(ext):
    assert is_audio_only_media({"path": "/media/track" + ext, "has_video": True}) is True


def test_query_suffixed_url_still_detected():
    assert is_audio_only_media({"path": "https://cdn/x/track.mp3?sig=abc123"}) is True


def test_uppercase_extension_detected():
    assert is_audio_only_media({"path": "/media/TRACK.MP3"}) is True


def test_media_type_audio_wins_when_extension_is_unknown():
    assert is_audio_only_media({"path": "/tmp/blob", "media_type": "audio"}) is True


def test_media_type_video_is_not_audio_only():
    assert is_audio_only_media({"path": "/tmp/blob", "media_type": "video"}) is False


def test_falls_back_to_stream_flags_without_path_or_media_type():
    assert is_audio_only_media({"has_audio": True, "has_video": False}) is True
    assert is_audio_only_media({"has_audio": True, "has_video": True}) is False


def test_non_dict_is_not_audio_only():
    assert is_audio_only_media(None) is False
    assert is_audio_only_media("track.mp3") is False


# --------------------------------------------------------------------------
# apply_audio_only_clip_overrides
# --------------------------------------------------------------------------

@pytest.mark.parametrize("file_data", [_cover_art_mp3(), _plain_mp3()])
def test_both_mp3_shapes_get_video_disabled(file_data):
    clip = {"id": "C1", "reader": dict(file_data)}
    assert _apply(clip, file_data) is True
    points = clip["has_video"]["Points"]
    assert len(points) == 1
    assert points[0]["co"]["Y"] == 0.0
    assert points[0]["co"]["X"] == 1.0
    assert points[0]["interpolation"] == CONSTANT
    assert clip["scale"] == SCALE_NONE
    # The reader copy must agree, or role detection reads the stale flag back.
    assert clip["reader"]["has_video"] is False


def test_video_clip_is_left_completely_untouched():
    file_data = _video()
    clip = {"id": "C1", "reader": dict(file_data), "scale": 1}
    assert _apply(clip, file_data) is False
    assert "has_video" not in clip
    assert clip["scale"] == 1
    assert clip["reader"]["has_video"] is True


def test_missing_reader_is_tolerated():
    file_data = _plain_mp3()
    clip = {"id": "C1"}
    assert _apply(clip, file_data) is True
    assert clip["scale"] == SCALE_NONE


def test_non_dict_clip_is_a_no_op():
    assert _apply(None, _plain_mp3()) is False
