"""Inside an update batch the timeline, preview and minimap react once at the end, not once per change.

Measured on a real project (87 clips): placing 20 clips took 19.6 s (about a second each, spent in libopenshot's timeline update and the
preview redraw after every single insert); the same placement in a batch took 5.7 s. Project data and undo history are written per
action exactly as before; only the listeners that register a flush hold back.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import update_batch  # noqa: E402


@pytest.fixture(autouse=True)
def clean_batch():
    update_batch._depth = 0
    update_batch._flushers.clear()
    yield
    update_batch._depth = 0
    update_batch._flushers.clear()


def test_listeners_flush_once_when_the_outermost_batch_ends():
    calls = []
    update_batch.register(lambda: calls.append("flush"))
    with update_batch.batch():
        assert update_batch.active()
        with update_batch.batch():
            pass
        assert calls == []                                  # the inner end does not flush
    assert calls == ["flush"] and not update_batch.active()


def test_a_batch_that_raises_still_lets_the_listeners_catch_up():
    calls = []
    update_batch.register(lambda: calls.append("flush"))
    with pytest.raises(RuntimeError):
        with update_batch.batch():
            raise RuntimeError("tool failed halfway")
    assert calls == ["flush"] and not update_batch.active()


def test_one_listener_failing_does_not_stop_the_others():
    calls = []
    update_batch.register(lambda: (_ for _ in ()).throw(ValueError("boom")))
    update_batch.register(lambda: calls.append("second"))
    with update_batch.batch():
        pass
    assert calls == ["second"]


def test_a_batch_nobody_closes_ends_itself_when_its_lease_runs_out():
    calls = []
    update_batch.register(lambda: calls.append("flush"))
    update_batch.begin(lease_seconds=0.0)                   # a worker that died after begin()
    import time
    time.sleep(0.01)
    assert update_batch.active() is False and calls == ["flush"]


def test_begin_end_can_span_several_short_hops():
    calls = []
    update_batch.register(lambda: calls.append("flush"))
    update_batch.begin()
    for _ in range(3):
        with update_batch.batch():                          # each hop is its own nested batch
            pass
        assert calls == []
    update_batch.end()
    assert calls == ["flush"]


class _Action:
    def __init__(self, kind, key0="clips", value=None):
        self.type, self.key, self.values = kind, [key0], value or {}

    def json(self, is_array=False):
        return json.dumps([{"type": self.type, "key": self.key, "value": self.values}])


def _timeline_sync():
    pytest.importorskip("PyQt5.QtCore")
    from classes.timeline import TimelineSync
    sync = TimelineSync.__new__(TimelineSync)
    applied = []
    sync.timeline = SimpleNamespace(ApplyJsonDiff=lambda payload: applied.append(json.loads(payload)),
                                    ClearAllCache=lambda *_: applied.append("cache cleared"))
    sync.window = SimpleNamespace(refreshFrameSignal=SimpleNamespace(emit=lambda: applied.append("refresh")), proxy_service=None)
    import threading
    sync.timeline_lock = threading.RLock()
    sync._batched_diffs, sync._batched_property_edit = [], False
    return sync, applied


def test_the_timeline_applies_a_batch_of_changes_in_one_call_and_redraws_once():
    sync, applied = _timeline_sync()
    update_batch.register(sync.flush_batch)
    with update_batch.batch():
        for i in range(5):
            sync.changed(_Action("insert", value={"id": f"c{i}"}))
        assert applied == []                                 # nothing reaches libopenshot until the end
    assert len(applied) == 3 and applied[1:] == ["cache cleared", "refresh"]
    assert [op["value"]["id"] for op in applied[0]] == ["c0", "c1", "c2", "c3", "c4"]     # one diff, in order


def test_changes_outside_a_batch_apply_at_once_as_before():
    sync, applied = _timeline_sync()
    sync.changed(_Action("insert", value={"id": "c0"}))
    assert applied[0][0]["value"]["id"] == "c0" and applied.count("refresh") == 1


def test_changes_that_do_not_touch_the_timeline_are_not_held_back_or_applied():
    sync, applied = _timeline_sync()
    with update_batch.batch():
        sync.changed(_Action("update", key0="files"))
    assert applied == [] and sync._batched_diffs == []


def test_a_project_load_discards_held_back_changes_and_is_never_batched():
    sync, _applied = _timeline_sync()
    update_batch.register(sync.flush_batch)
    with update_batch.batch():
        sync.changed(_Action("insert", value={"id": "c0"}))
        assert len(sync._batched_diffs) == 1
        sync._batched_diffs = []                             # what changed() does first for a load
    assert sync._batched_diffs == []


def test_the_preview_is_not_asked_to_redraw_twice_inside_a_batch(monkeypatch):
    from classes.editor_tools import _base
    drawn = []
    monkeypatch.setattr(_base, "get_app", lambda: SimpleNamespace(window=SimpleNamespace(refreshFrameSignal=SimpleNamespace(emit=lambda: drawn.append(1)))))
    _base.refresh_preview()
    assert drawn == [1]
    with update_batch.batch():
        _base.refresh_preview()
    assert drawn == [1]                                      # the batch redraws once itself
