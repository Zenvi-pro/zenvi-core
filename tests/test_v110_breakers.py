"""Regression tests for v1.1.0 breakers (pure, no live installer)."""

from __future__ import annotations

import inspect
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_ordinal_map_does_not_treat_one_as_first():
    from classes.tool_handlers import _ORDINAL_MAP, _detect_ordinal, _parse_occurrence

    assert "one" not in _ORDINAL_MAP
    assert "two" not in _ORDINAL_MAP
    assert _detect_ordinal("one person walking") == 0
    assert _parse_occurrence("0", "one person walking") == 0
    assert _parse_occurrence("0", "the first clip") == 1


def test_slice_clip_imports_get_index_block():
    from classes import tool_handlers as th

    src = inspect.getsource(th.slice_clip_at_best_match)
    assert "get_index_block" in src
    assert "from classes.twelvelabs_match import get_index_block" in src


def test_catalog_and_slice_are_background_safe():
    from classes.tool_handlers import BACKGROUND_SAFE_TOOLS

    assert "get_project_catalog_tool" in BACKGROUND_SAFE_TOOLS
    assert "slice_clip_at_best_match_tool" in BACKGROUND_SAFE_TOOLS


def test_set_session_backend_pushes_tabs_to_js():
    path = os.path.join(_ROOT, "windows", "ai_chat_ui.py")
    source = open(path, encoding="utf-8").read()
    start = source.index("def _set_session_backend")
    body = source[start:source.index("\n    def ", start + 1)]
    assert "_push_tabs_to_js()" in body


def test_check_audio_device_binds_settings_before_use():
    path = os.path.join(_ROOT, "windows", "preview_thread.py")
    source = open(path, encoding="utf-8").read()
    start = source.index("def CheckAudioDevice")
    body = source[start:source.index("\n    def ", start + 1)]
    assign = body.index("s = get_app().get_settings()")
    use = body.index("s.get(\"playback-audio-device\")")
    assert assign < use
    # Invalid-rate path must not reference detected_sample_rate_int outside the branch
    after_if = body.split("if detected_sample_rate and not math.isnan")[1]
    # The float-settings conversion that used unbound s/int is inside the valid-rate block
    # or omitted; playback-audio-device use is after s is assigned.
    assert "detected_sample_rate_int" not in body.split("active_audio_device")[1]


def test_found_current_version_reinstalls_crash_handler():
    path = os.path.join(_ROOT, "windows", "main_window.py")
    source = open(path, encoding="utf-8").read()
    start = source.index("def foundCurrentVersion")
    body = source[start:source.index("\n    def ", start + 1)]
    assert "sentry.init_tracing()" in body
    assert "crash_handler.install()" in body
    assert body.index("sentry.init_tracing()") < body.index("crash_handler.install()")
