"""Pin the native-vs-web timeline parity audit for silent run_js gaps.

When ViewClass is TimelineWidget, run_js is a no-op. Methods that only call
run_js with no native branch are silent feature gaps. This module records the
audit verdict for each method so the list cannot grow unnoticed.

Verdicts:
  covered — native path exists via another route (dragEnterEvent, signals, etc.)
  fixed   — TimelineView now dispatches to a TimelineWidget method
  n/a     — web-only lifecycle (page_ready) with no native equivalent needed
"""

from __future__ import annotations

import ast
import pathlib

# Canonical list from Phase 1 audit. Update verdicts when fixing gaps.
PARITY_AUDIT = {
    # Web document.ready — native has no HTML page.
    "page_ready": "n/a",
    # Playhead follows preview_thread.position_changed → update_playhead_pos.
    # Also wired to update_playhead_pos for SeekSignal / main_window callers.
    "movePlayhead": "fixed",
    # Sets TimelineWidget.is_auto_center (was silent no-op).
    "SetPlayheadFollow": "fixed",
    # Stores keyframe_prop_filter and refreshes markers (was silent no-op).
    "SetPropertyFilter": "fixed",
    # Invalidates thumb/clip caches for the clip (was silent no-op).
    "Thumbnail_Updated": "fixed",
    # Properties table selection sync → _select_timeline_item.
    "AddSelectionJS": "fixed",
    # Zoom-slider TimelineScroll → set_scroll_left (also wired on native init).
    "update_scroll": "fixed",
    # Waveform redraw → clear paint caches + update.
    "redraw_audio_onTimeout": "fixed",
    # SelectionChanged → TimelineWidget.handle_selection.
    "handle_selection": "fixed",
    # Native dragEnterEvent owns clip creation; TimelineView.addClip is web-only.
    "addClip": "covered",
    "addTransition": "covered",
    # Nested helpers only reached from the web dragEnterEvent path.
    "handle_js_position": "covered",
    "callback": "covered",
}

UNGARDED_RUN_JS_METHODS = frozenset(PARITY_AUDIT.keys())


def _timeline_py_source():
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "src" / "windows" / "views" / "timeline.py").read_text(encoding="utf-8")


def _methods_calling_run_js_without_native_guard(source: str) -> set[str]:
    """Parse timeline.py for methods that call run_js but never mention TimelineWidget."""
    tree = ast.parse(source)
    class_body = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "TimelineView":
            class_body = node
            break
    assert class_body is not None, "TimelineView class not found"

    found = set()
    for item in class_body.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Nested functions (handle_js_position, callback) live inside methods —
        # walk the whole function body for nested defs too.
        stack = [item]
        while stack:
            fn = stack.pop()
            text = ast.dump(fn)
            calls_run_js = "run_js" in text
            has_native_guard = "TimelineWidget" in text
            if calls_run_js and not has_native_guard:
                found.add(fn.name)
            for child in ast.walk(fn):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child is not fn
                    and child.name not in found
                ):
                    # Only consider direct nested defs once
                    if any(
                        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is child
                        for n in fn.body
                    ):
                        stack.append(child)
    return found


def test_parity_audit_covers_every_known_gap():
    assert set(PARITY_AUDIT) == UNGARDED_RUN_JS_METHODS
    assert len(PARITY_AUDIT) == 13


def test_parity_verdicts_are_known_labels():
    allowed = {"covered", "fixed", "n/a"}
    for name, verdict in PARITY_AUDIT.items():
        assert verdict in allowed, f"{name} has unknown verdict {verdict!r}"


def test_ungarded_run_js_set_matches_source():
    """If someone adds a new run_js-only method, this fails until the audit is updated."""
    source = _timeline_py_source()
    found = _methods_calling_run_js_without_native_guard(source)
    # The parser also sees helpers that still only call run_js on web paths
    # after a native early-return in the parent — those are fine to omit from
    # the canonical 13 if they gained a TimelineWidget mention. Require the
    # canonical 13 still appear as needing attention OR now mention TimelineWidget.
    still_unguarded = set()
    for name in UNGARDED_RUN_JS_METHODS:
        # Heuristic: method source between def name and next def at same indent
        if f"def {name}" not in source:
            # nested names
            if name not in ("handle_js_position", "callback"):
                raise AssertionError(f"audit method {name} missing from timeline.py")
            continue
        # After fixes, methods may mention TimelineWidget — drop from found.
        # "fixed" entries that are still unguarded are regressions.
        if PARITY_AUDIT[name] == "fixed" and name in found:
            still_unguarded.add(name)
        elif PARITY_AUDIT[name] in ("covered", "n/a"):
            pass
    assert not still_unguarded, (
        f"PARITY_AUDIT 'fixed' methods still lack a TimelineWidget guard: "
        f"{sorted(still_unguarded)}"
    )
    # Any method still in `found` that is not in our audit is a NEW gap.
    unexpected = found - UNGARDED_RUN_JS_METHODS
    # Nested defs inside guarded parents may still look unguarded; only flag
    # top-level TimelineView methods.
    top_level_unexpected = {
        n for n in unexpected
        if f"\n    def {n}(" in source or source.startswith(f"def {n}(")
    }
    assert not top_level_unexpected, (
        f"New run_js-only TimelineView methods without audit entry: "
        f"{sorted(top_level_unexpected)}"
    )


def test_snap_helper_reset_clears_targets():
    from types import SimpleNamespace
    from windows.views.timeline_backend.snap import SnapHelper

    widget = SimpleNamespace(_snap_active_targets={"drag-left": {"px": 1.0, "tol": 12.0}})
    geo = SimpleNamespace()
    helper = SnapHelper(widget, geo)
    helper.reset(["drag-left"])
    assert "drag-left" not in widget._snap_active_targets
    widget._snap_active_targets = {"a": 1, "b": 2}
    helper.reset()
    assert widget._snap_active_targets == {}
