"""Unit tests for the converged timeline delete tool.

delete_from_timeline_tool replaced the pair remove_clip_tool /
delete_clips_on_track_tool, which the agent could not reliably choose
between -- and one of which was uncallable, because its backend schema
declared no arguments while the handler hard-required a target.

The old names remain as aliases, so they are exercised here too.
"""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
            out = tool_handlers.delete_from_timeline(**kwargs)
    return out, resolver


# --- targeting guard -------------------------------------------------------

def test_no_args_errors_without_resolving_or_deleting():
    app = _mock_app()
    sentinel = _clip("c1")
    out, resolver = _run(app, ResolveResult(ok=True, clip=sentinel))
    assert out.startswith("Error:")
    # The error must name every way to target the tool, so an agent that got
    # here can act on it instead of guessing at another delete tool.
    assert "timeline_clip_id" in out
    assert "clip_query" in out
    assert "track" in out
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


def test_success_clears_selection_and_refreshes():
    app = _mock_app()
    target = _clip("c2", position=10.0)

    out, _ = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")

    assert not out.startswith("Error:")
    app.window.removeSelection.assert_called_once_with("c2", "clip")
    app.window.videoPreview.clearTransformState.assert_called_once_with()
    app.window.refreshFrameSignal.emit.assert_called_once_with()


def test_handler_does_not_open_its_own_transaction():
    """execute_tool owns the transaction now.

    The handler used to mint its own id and reset it to None in a finally,
    which detached anything the caller did afterwards into separate undo
    steps. It must leave the ambient transaction exactly as it found it.
    """
    app = _mock_app()
    app.updates.transaction_id = "OUTER"
    target = _clip("c2", position=10.0)
    seen = []
    target.delete.side_effect = lambda: seen.append(app.updates.transaction_id)

    out, _ = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")

    assert not out.startswith("Error:")
    assert seen == ["OUTER"], "handler replaced the caller's transaction"
    assert app.updates.transaction_id == "OUTER", "handler clobbered it on exit"


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


# --- scope routing ---------------------------------------------------------

def _run_track(app, **kwargs):
    """Drive the track branch with classes.query stubbed."""
    clips = kwargs.pop("clips", [])
    transitions = kwargs.pop("transitions", [])
    query = MagicMock(Clip=MagicMock(), Transition=MagicMock())
    query.Clip.filter.return_value = clips
    query.Transition.filter.return_value = transitions
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.dict(sys.modules, {"classes.query": query}):
        out = tool_handlers.delete_from_timeline(**kwargs)
    return out, query


def test_auto_scope_picks_clip_when_given_an_id():
    app = _mock_app()
    target = _clip("c2")
    out, resolver = _run(app, ResolveResult(ok=True, clip=target), timeline_clip_id="c2")
    assert not out.startswith("Error:")
    resolver.assert_called_once()
    target.delete.assert_called_once_with()


def test_auto_scope_picks_track_when_given_only_a_track():
    app = _mock_app()
    a, b = _clip("c1"), _clip("c2")
    out, query = _run_track(app, track=str(TRACK_A), clips=[a, b])
    assert not out.startswith("Error:"), out
    query.Clip.filter.assert_called_once_with(layer=TRACK_A)
    a.delete.assert_called_once_with()
    b.delete.assert_called_once_with()


def test_explicit_clip_scope_refuses_a_bare_track():
    """Guards the dangerous widening: "delete on track 1" must not clear it."""
    app = _mock_app()
    out, resolver = _run(
        app, ResolveResult(ok=True, clip=_clip("c1")),
        track=str(TRACK_A), scope="clip",
    )
    assert out.startswith("Error:")
    assert "timeline_clip_id or clip_query" in out
    resolver.assert_not_called()


def test_explicit_track_scope_requires_a_track():
    app = _mock_app()
    out, _query = _run_track(app, scope="track")
    assert out.startswith("Error:")
    assert "track" in out


def test_unknown_scope_is_rejected():
    app = _mock_app()
    out, _query = _run_track(app, track=str(TRACK_A), scope="everything")
    assert out.startswith("Error:")
    assert "scope" in out


def test_track_scope_deletes_transitions_first():
    """Transitions reference clip time ranges, so they must go first."""
    app = _mock_app()
    order = []
    clip = _clip("c1")
    clip.delete.side_effect = lambda: order.append("clip")
    transition = MagicMock(id="t1")
    transition.delete.side_effect = lambda: order.append("transition")

    out, _query = _run_track(
        app, track=str(TRACK_A), clips=[clip], transitions=[transition]
    )
    assert not out.startswith("Error:"), out
    assert order == ["transition", "clip"]


def test_track_scope_can_keep_transitions():
    app = _mock_app()
    transition = MagicMock(id="t1")
    out, query = _run_track(
        app, track=str(TRACK_A), clips=[], transitions=[transition],
        include_transitions=False,
    )
    assert not out.startswith("Error:"), out
    query.Transition.filter.assert_not_called()
    transition.delete.assert_not_called()


def test_track_scope_respects_locked_tracks():
    app = _mock_app(locked_layers=(TRACK_A,))
    clip = _clip("c1")
    out, _query = _run_track(app, track=str(TRACK_A), clips=[clip])
    assert out.startswith("Error:")
    assert "locked" in out
    clip.delete.assert_not_called()


def test_result_states_the_undo_cost():
    """The agent needs to know a delete is ONE undo step, not N."""
    app = _mock_app()
    out, _ = _run(app, ResolveResult(ok=True, clip=_clip("c2")), timeline_clip_id="c2")
    assert "1 undo step" in out


# --- deprecated aliases ----------------------------------------------------

def test_remove_clip_alias_still_deletes_one_clip():
    app = _mock_app()
    target = _clip("c2", position=4.0)
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.object(tool_handlers, "_resolve_timeline_clip_for_tool",
                      return_value=ResolveResult(ok=True, clip=target)):
        out = tool_handlers.remove_clip(timeline_clip_id="c2")
    assert not out.startswith("Error:"), out
    target.delete.assert_called_once_with()


def test_remove_clip_alias_never_widens_to_a_track_clear():
    app = _mock_app()
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.object(tool_handlers, "_resolve_timeline_clip_for_tool") as resolver:
        out = tool_handlers.remove_clip(track=str(TRACK_A))
    assert out.startswith("Error:")
    resolver.assert_not_called()


def test_delete_clips_on_track_alias_still_clears_a_track():
    app = _mock_app()
    clip = _clip("c1")
    query = MagicMock(Clip=MagicMock(), Transition=MagicMock())
    query.Clip.filter.return_value = [clip]
    query.Transition.filter.return_value = []
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.dict(sys.modules, {"classes.query": query}):
        out = tool_handlers.delete_clips_on_track(track=str(TRACK_A))
    assert not out.startswith("Error:"), out
    clip.delete.assert_called_once_with()


def test_delete_clips_on_track_alias_still_requires_a_track():
    app = _mock_app()
    with patch.object(tool_handlers, "_get_app", return_value=app):
        out = tool_handlers.delete_clips_on_track()
    assert out == "Error: track is required."


# --- registration ----------------------------------------------------------

def test_all_three_names_dispatch():
    for name in ("delete_from_timeline_tool", "remove_clip_tool",
                 "delete_clips_on_track_tool"):
        assert name in tool_handlers.AGENT_TOOL_HANDLERS
        assert name in tool_handlers.TOOL_DISPLAY_LABELS


def test_the_three_names_share_one_display_label():
    """Distinct labels are what made the agent read them as distinct tools."""
    labels = {
        tool_handlers.TOOL_DISPLAY_LABELS[n]
        for n in ("delete_from_timeline_tool", "remove_clip_tool",
                  "delete_clips_on_track_tool")
    }
    assert labels == {"Delete from timeline"}


def test_track_scope_refuses_a_contradictory_clip_target():
    """One clip named + "clear the track" must not clear the track.

    The destructive reading of a contradictory call is never the safe guess.
    """
    app = _mock_app()
    clip = _clip("c1")
    out, query = _run_track(
        app, track=str(TRACK_A), scope="track",
        timeline_clip_id="c9", clips=[clip],
    )
    assert out.startswith("Error:"), out
    assert "clears the whole track" in out
    query.Clip.filter.assert_not_called()
    clip.delete.assert_not_called()


def test_track_scope_refuses_a_contradictory_clip_query():
    app = _mock_app()
    clip = _clip("c1")
    out, query = _run_track(
        app, track=str(TRACK_A), scope="track",
        clip_query="the lower third", clips=[clip],
    )
    assert out.startswith("Error:"), out
    clip.delete.assert_not_called()


def test_auto_scope_prefers_the_clip_when_a_track_also_narrows_it():
    """track is a documented disambiguator for clip_query, not a conflict."""
    app = _mock_app()
    target = _clip("c9", layer=TRACK_B, position=12.0)
    out, resolver = _run(
        app, ResolveResult(ok=True, clip=target),
        clip_query="b-roll", track="3",
    )
    assert not out.startswith("Error:"), out
    target.delete.assert_called_once_with()
    assert resolver.call_args.kwargs["track"] == "3"


def test_auto_scope_deleting_one_clip_names_it_so_a_wrong_guess_is_visible():
    app = _mock_app()
    target = _clip("c9", position=12.0, title="Lower third")
    out, _ = _run(
        app, ResolveResult(ok=True, clip=target),
        clip_query="lower third", track="3",
    )
    assert "c9" in out and "Lower third" in out
    assert "Other clips on that track are unchanged" in out
