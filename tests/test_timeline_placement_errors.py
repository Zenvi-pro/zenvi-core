"""Timeline placement and delete errors name only tools the editor has (zenvi-core#177).

- A refused head-trim must not send the caller to place_moment, which is a
  backend-only workflow the editor MCP does not expose.
- delete_from_timeline_tool refuses unknown arguments such as close_gap instead
  of dropping them and reporting "gap left, no ripple".
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes import tool_handlers  # noqa: E402
from classes.agent_tools.schema import TOOL_SCHEMAS  # noqa: E402
from classes.clip_resolver import ResolveResult  # noqa: E402
from classes.editor_tools import REGISTRY  # noqa: E402

TRACK = 1000000


def _head_trim_refusal():
    """What add_clip_to_timeline_tool answers a blind 'first N seconds' trim."""
    file_obj = MagicMock()
    file_obj.id = "F1"
    file_obj.data = {
        "path": "/clips/long.mp4", "name": "long.mp4", "duration": 120.0,
        "start": 0.0, "end": 120.0, "has_video": True,
    }
    query_mod = MagicMock()
    query_mod.File.get.return_value = file_obj
    with patch.object(tool_handlers, "_get_app", return_value=MagicMock()), \
         patch.dict(sys.modules, {"classes.query": query_mod}):
        return tool_handlers.add_clip_to_timeline(
            file_id="F1", position_seconds="0", duration_seconds="60",
        )


def test_head_trim_refusal_names_only_tools_the_editor_has():
    out = _head_trim_refusal()
    assert out.startswith("Error:"), out
    assert "place_moment" not in out, out
    known = set(tool_handlers.AGENT_TOOL_HANDLERS) | set(REGISTRY)
    params = set(TOOL_SCHEMAS["add_clip_to_timeline_tool"]["properties"])
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", out)) - params
    assert named <= known, f"refusal names tools the editor does not have: {sorted(named - known)}"


def _clip(clip_id, *, position):
    c = MagicMock()
    c.id = clip_id
    c.data = {"id": clip_id, "title": "Clip", "position": position, "start": 0.0,
              "end": 5.0, "layer": TRACK, "file_id": "f1"}
    return c


def _delete(clip, **kwargs):
    app = MagicMock()
    app.project.get.return_value = [{"number": TRACK, "id": "t1", "lock": False}]
    resolved = ResolveResult(ok=True, clip=clip)
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved):
        return tool_handlers.delete_from_timeline(timeline_clip_id=clip.id, **kwargs)


def test_close_gap_is_refused_and_deletes_nothing():
    clip = _clip("c2", position=5.0)
    out = _delete(clip, close_gap=True)
    assert out.startswith("Error: unknown argument(s): close_gap"), out
    assert "did you mean ripple=true?" in out, out
    clip.delete.assert_not_called()


def test_close_gap_through_the_tool_path_deletes_nothing(editor):
    f = editor.add_file("video", duration=20.0)
    b = editor.add_clip(f, position=5.0, start=0.0, end=5.0)
    out = editor.call("delete_from_timeline_tool", timeline_clip_id=b, close_gap=True)
    assert out.startswith("Error"), out
    assert editor.clip(b) is not None


def test_ripple_true_still_closes_the_gap(editor):
    f = editor.add_file("video", duration=20.0)
    editor.add_clip(f, position=0.0, start=0.0, end=5.0)
    b = editor.add_clip(f, position=5.0, start=0.0, end=5.0)
    c = editor.add_clip(f, position=10.0, start=0.0, end=5.0)
    out = editor.call("delete_from_timeline_tool", timeline_clip_id=b, ripple=True)
    assert "closed the gap" in out, out
    assert editor.clip(c)["position"] == 5.0
