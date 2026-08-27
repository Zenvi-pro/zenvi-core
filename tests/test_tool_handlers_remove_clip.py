"""Unit tests for single-placement timeline delete (remove_clip_tool)."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless CI).
_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes import tool_handlers  # noqa: E402
from classes.clip_resolver import ResolveResult  # noqa: E402

TRACK_A = 1000000
TRACK_B = 2000000


def _clip(clip_id, *, position=0.0, layer=TRACK_A, title="Clip"):
    c = MagicMock()
    c.id = clip_id
    c.data = {
        "id": clip_id,
        "title": title,
        "position": position,
        "start": 0.0,
        "end": 10.0,
        "layer": layer,
        "file_id": f"file-{clip_id}",
    }
    return c


def _mock_app(*, locked_layers=()):
    app = MagicMock()
    app.project.get.return_value = [
        {"number": TRACK_A, "id": "t1", "lock": TRACK_A in locked_layers},
        {"number": TRACK_B, "id": "t2", "lock": TRACK_B in locked_layers},
    ]
    # transaction_id must be a plain attribute so we can observe set/clear.
    app.updates.transaction_id = None
    return app


def _run(app, resolved, **kwargs):
    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(
            tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved
        ) as resolver:
            out = tool_handlers.remove_clip(**kwargs)
    return out, resolver


# --- targeting guard -------------------------------------------------------

def test_no_args_errors_without_resolving_or_deleting():
    app = _mock_app()
    sentinel = _clip("c1")
    out, resolver = _run(app, ResolveResult(ok=True, clip=sentinel))
    assert out.startswith("Error:")
    assert "timeline_clip_id or clip_query" in out
    resolver.assert_not_called()
    sentinel.delete.assert_not_called()


def test_blank_string_args_error():
    app = _mock_app()
    out, resolver = _run(
        app, ResolveResult(ok=True, clip=_clip("c1")),
        timeline_clip_id="   ", clip_query="",
    )
    assert out.startswith("Error:")
    resolver.assert_not_called()


# --- resolution failures ---------------------------------------------------

def test_unknown_timeline_clip_id_errors_without_mutation():
    app = _mock_app()
    resolved = ResolveResult(ok=False, error="Error: No timeline clip with id='nope'.")
    out, _ = _run(app, resolved, timeline_clip_id="nope")
    assert out == "Error: No timeline clip with id='nope'."
    assert app.updates.transaction_id is None
    app.window.removeSelection.assert_not_called()


def test_ambiguous_query_returns_candidates_and_deletes_nothing():
    app = _mock_app()
    err = (
        "Ambiguous clip_query — multiple close matches:\n"
        "  1. timeline_clip_id=c1 title='A'\n  2. timeline_clip_id=c2 title='B'"
    )
    resolved = ResolveResult(ok=False, error=err)
    out, _ = _run(app, resolved, clip_query="the clip")
    assert out == err
    assert "timeline_clip_id=c1" in out and "timeline_clip_id=c2" in out
    app.window.removeSelection.assert_not_called()
    app.window.refreshFrameSignal.emit.assert_not_called()


def test_locked_track_errors_without_mutation():
    app = _mock_app(locked_layers=(TRACK_A,))
    target = _clip("c2", position=5.0)
    out, _ = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")
    assert out.startswith("Error:")
    assert "locked" in out
    target.delete.assert_not_called()
    assert app.updates.transaction_id is None


# --- success path ----------------------------------------------------------

def test_deletes_only_the_middle_clip_on_a_busy_track():
    app = _mock_app()
    first = _clip("c1", position=0.0)
    middle = _clip("c2", position=10.0)
    last = _clip("c3", position=20.0)

    out, _ = _run(app, ResolveResult(ok=True, clip=middle), timeline_clip_id="c2")

    assert not out.startswith("Error:")
    middle.delete.assert_called_once_with()
    first.delete.assert_not_called()
    last.delete.assert_not_called()
    # Siblings keep their identity and placement.
    assert first.data["position"] == 0.0 and first.id == "c1"
    assert last.data["position"] == 20.0 and last.id == "c3"


def test_other_tracks_untouched():
    app = _mock_app()
    overlay = _clip("c-overlay", layer=TRACK_B, position=3.0)
    music = _clip("c-music", layer=TRACK_A, position=0.0)

    out, _ = _run(app, ResolveResult(ok=True, clip=overlay), timeline_clip_id="c-overlay")

    assert not out.startswith("Error:")
    overlay.delete.assert_called_once_with()
    music.delete.assert_not_called()


def test_success_uses_transaction_selection_and_refresh():
    app = _mock_app()
    target = _clip("c2", position=10.0)
    seen = []
    target.delete.side_effect = lambda: seen.append(app.updates.transaction_id)

    out, _ = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")

    assert not out.startswith("Error:")
    # A transaction was open during the delete, and cleared afterwards.
    assert seen and seen[0]
    assert app.updates.transaction_id is None
    app.window.removeSelection.assert_called_once_with("c2", "clip")
    app.window.videoPreview.clearTransformState.assert_called_once_with()
    app.window.refreshFrameSignal.emit.assert_called_once_with()


def test_never_uses_selection_based_delete():
    app = _mock_app()
    target = _clip("c2")
    _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")
    app.window.actionRemoveClip_trigger.assert_not_called()


def test_transitions_are_left_alone():
    app = _mock_app()
    target = _clip("c2")
    mock_transition = MagicMock()
    query = MagicMock(Clip=MagicMock(), Transition=MagicMock())
    query.Transition.filter.return_value = [mock_transition]

    with patch.dict(sys.modules, {"classes.query": query}):
        _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")

    query.Transition.filter.assert_not_called()
    mock_transition.delete.assert_not_called()


def test_confirmation_names_clip_track_and_position():
    app = _mock_app()
    target = _clip("c2", position=12.5, title="B-roll")
    out, _ = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")
    assert "c2" in out
    assert "B-roll" in out
    assert "12.50s" in out
    assert str(TRACK_A) in out
    assert "unchanged" in out


def test_query_targeting_forwards_disambiguators_to_resolver():
    app = _mock_app()
    target = _clip("c9", layer=TRACK_B, position=12.0)
    out, resolver = _run(
        app, ResolveResult(ok=True, clip=target),
        clip_query="b-roll", track="3", occurrence="2", position_near=12.0,
    )
    assert not out.startswith("Error:")
    kwargs = resolver.call_args.kwargs
    assert kwargs["clip_query"] == "b-roll"
    assert kwargs["track"] == "3"
    assert kwargs["occurrence"] == "2"
    assert kwargs["position_near"] == 12.0
