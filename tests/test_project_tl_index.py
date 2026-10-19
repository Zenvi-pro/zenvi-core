"""Tests for project TwelveLabs index helper (no Qt app required)."""

from __future__ import annotations

from classes.project_tl_index import build_project_index_name


def test_build_project_index_name():
    assert build_project_index_name("proj-uuid-1") == "zenvi-proj-uuid-1"
    assert build_project_index_name("") == "zenvi-videos"
