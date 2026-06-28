"""Regression tests for the data_version / QueryObject cache ordering.

These guard the timeline overview off-by-one ("applies the previous drag"): a
listener that reads project data AFTER a dispatch must observe the committed
state, and an earlier listener reading mid-dispatch must not poison the cache for
later listeners. Runs headlessly by patching get_app (no QApplication).
"""

import copy
import os
import sys
import types

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# libopenshot bindings aren't needed for these pure-state tests; stub if absent.
sys.modules.setdefault("openshot", types.ModuleType("openshot"))

import classes.query as query_mod  # noqa: E402
from classes.query import Clip, QueryObject  # noqa: E402
from classes.updates import UpdateManager, UpdateInterface  # noqa: E402


class _FakeProject:
    """Minimal project store mirroring ProjectDataStore.get path semantics."""

    def __init__(self, data):
        self._data = data

    def get(self, key):
        if not isinstance(key, list):
            key = [key]
        node = self._data
        for part in key:
            node = node[part]
        # Real store returns a detached copy of the requested node.
        return copy.deepcopy(node)


class _FakeApp:
    def __init__(self, project, updates):
        self.project = project
        self.updates = updates


class _CommittingStore(UpdateInterface):
    """Mimics ProjectDataStore: mutate clip in place, then bump the version."""

    def __init__(self, app):
        self.app = app

    def changed(self, action):
        if action.type == "update":
            target_id = None
            for part in action.key:
                if isinstance(part, dict) and "id" in part:
                    target_id = part["id"]
            for clip in self.app.project._data.get("clips", []):
                if clip.get("id") == target_id:
                    clip.update(action.values)
                    break
        # Commit AFTER writing the mutation, as the real store now does.
        self.app.updates.commit_data_version()


class _RecordingReader(UpdateInterface):
    """Records the clip position it observes when notified."""

    def __init__(self):
        self.seen = []

    def changed(self, action):
        clip = Clip.get(id="c1")
        self.seen.append(clip.data.get("position") if clip else None)


def _setup():
    # Reset the process-wide query cache so a stale class-level version can't leak.
    QueryObject._cache = {}
    QueryObject._cache_version = None

    data = {
        "clips": [{"id": "c1", "position": 0.0, "layer": 1, "start": 0.0, "end": 5.0}],
        "duration": 60.0,
    }
    updates = UpdateManager()
    app = _FakeApp(_FakeProject(data), updates)
    query_mod.get_app = lambda: app
    return app


def test_late_listener_sees_committed_position():
    app = _setup()
    early = _RecordingReader()   # like TimelineSync at index 0
    store = _CommittingStore(app)
    late = _RecordingReader()    # like the ZoomSlider overview, appended last

    app.updates.add_listener(early, 0)
    app.updates.add_listener(store)
    app.updates.add_listener(late)

    app.updates.update(["clips", {"id": "c1"}], {"id": "c1", "position": 10.0,
                                                 "layer": 1, "start": 0.0, "end": 5.0})

    # The overview-equivalent listener must see the NEW position, never the previous one.
    assert late.seen[-1] == 10.0
    # The early (pre-commit) read observed the old value but under the old version,
    # so it cannot poison what the late listener reads.
    assert early.seen[-1] == 0.0


def test_no_version_bump_until_commit():
    app = _setup()
    start_version = app.updates.data_version

    seen_versions = {}

    class _VersionProbe(UpdateInterface):
        def __init__(self, label):
            self.label = label

        def changed(self, action):
            seen_versions[self.label] = app.updates.data_version

    early = _VersionProbe("early")
    store = _CommittingStore(app)
    late = _VersionProbe("late")
    app.updates.add_listener(early, 0)
    app.updates.add_listener(store)
    app.updates.add_listener(late)

    app.updates.update(["clips", {"id": "c1"}], {"id": "c1", "position": 7.0,
                                                 "layer": 1, "start": 0.0, "end": 5.0})

    # Version is unchanged for listeners before the store commits, and bumped after.
    assert seen_versions["early"] == start_version
    assert seen_versions["late"] == start_version + 1
    assert app.updates.data_version == start_version + 1


def test_fresh_query_after_dispatch_is_current():
    app = _setup()
    store = _CommittingStore(app)
    app.updates.add_listener(store)

    app.updates.update(["clips", {"id": "c1"}], {"id": "c1", "position": 42.0,
                                                 "layer": 1, "start": 0.0, "end": 5.0})

    clip = Clip.get(id="c1")
    assert clip.data.get("position") == 42.0


if __name__ == "__main__":
    test_late_listener_sees_committed_position()
    test_no_version_bump_until_commit()
    test_fresh_query_after_dispatch_is_current()
    print("test_update_version_ordering: ok")
