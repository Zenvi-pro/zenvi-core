"""Thumbnail grid helpers added for Optimize Preview (classes/thumbnail.py)."""

from unittest.mock import patch

from classes import thumbnail


def test_frame_step_for_fps_targets_four_thumbnails_per_second():
    assert thumbnail.ThumbnailFrameStepForFps(30) == 8
    assert thumbnail.ThumbnailFrameStepForFps(24) == 6
    assert thumbnail.ThumbnailFrameStepForFps(60, target_fps=12) == 5
    assert thumbnail.ThumbnailFrameStepForFps(0) == 1


def test_round_frame_to_grid_snaps_to_nearest_grid_frame():
    # 30 fps -> step 8 -> grid frames 1, 9, 17, ...
    assert thumbnail.RoundFrameToThumbnailGrid(1, 30) == 1
    assert thumbnail.RoundFrameToThumbnailGrid(4, 30) == 1
    assert thumbnail.RoundFrameToThumbnailGrid(6, 30) == 9
    assert thumbnail.RoundFrameToThumbnailGrid(12, 30) == 9
    assert thumbnail.RoundFrameToThumbnailGrid(14, 30) == 17
    assert thumbnail.RoundFrameToThumbnailGrid(0, 30) == 1
    # unknown fps -> identity
    assert thumbnail.RoundFrameToThumbnailGrid(7, 0) == 7


def test_thumbnail_path_for_frame_uses_media_cache_layout(tmp_path):
    with patch.object(thumbnail.info, "THUMBNAIL_PATH", str(tmp_path)):
        assert thumbnail.ThumbnailPathForFrame("F1", 9) == str(tmp_path / "F1" / "9.png")


def test_thumbnail_path_for_frame_prefers_fingerprint_cache(tmp_path):
    fingerprint = {"sha256": "a" * 64}
    with patch("classes.media_cache.media_cache_root", return_value=str(tmp_path)):
        path = thumbnail.ThumbnailPathForFrame("F1", 9, fingerprint=fingerprint)
    assert path == str(tmp_path / ("a" * 64) / "thumbs" / "9.png")
