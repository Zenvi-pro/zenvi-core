"""Regenerable inspect media fixtures."""

from __future__ import annotations


def test_inspect_fixture_materializes(inspect_h264_fixture):
    path = inspect_h264_fixture
    assert path.name == "h264_720p30_2s.mp4"
    assert path.stat().st_size > 1024


def test_inspect_fixture_cache_hit(inspect_h264_fixture, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "generate_inspect_fixtures.py"
    spec = importlib.util.spec_from_file_location("generate_inspect_fixtures", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    first = mod.ensure_fixture("h264_720p30_2s.mp4")
    mtime = first.stat().st_mtime
    second = mod.ensure_fixture("h264_720p30_2s.mp4")
    assert second == first
    assert second.stat().st_mtime == mtime
