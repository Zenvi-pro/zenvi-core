"""Concurrency test for the QueryObject cache lock.

The query cache is shared between the GUI thread and worker threads (preview,
thumbnails, AI tagging). Concurrent reads during cache invalidation must not
raise or corrupt state, and reads must converge to the committed value.
"""

import copy
import os
import sys
import threading
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
    def __init__(self, data):
        self._data = data

    def get(self, key):
        if not isinstance(key, list):
            key = [key]
        node = self._data
        for part in key:
            node = node[part]
        return copy.deepcopy(node)


class _FakeApp:
    def __init__(self, project, updates):
        self.project = project
        self.updates = updates


class _CommittingStore(UpdateInterface):
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
        self.app.updates.commit_data_version()


def test_concurrent_reads_during_dispatch_are_safe():
    QueryObject._cache = {}
    QueryObject._cache_version = None

    data = {"clips": [{"id": "c1", "position": 0.0, "layer": 1, "start": 0.0, "end": 5.0}]}
    updates = UpdateManager()
    app = _FakeApp(_FakeProject(data), updates)
    query_mod.get_app = lambda: app
    updates.add_listener(_CommittingStore(app))

    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            try:
                clip = Clip.get(id="c1")
                if clip is not None:
                    _ = clip.data.get("position")
            except Exception as ex:  # noqa: BLE001 - we want to surface any race
                errors.append(ex)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()

    try:
        for i in range(1, 401):
            updates.update(["clips", {"id": "c1"}],
                           {"id": "c1", "position": float(i), "layer": 1, "start": 0.0, "end": 5.0})
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    assert not errors, f"reader threads raced: {errors[:3]}"
    # Eventual consistency: a fresh read reflects the final committed position.
    assert Clip.get(id="c1").data.get("position") == 400.0


if __name__ == "__main__":
    test_concurrent_reads_during_dispatch_are_safe()
    print("test_query_cache_concurrency: ok")
