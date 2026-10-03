"""Handler-level edit_ops tests (mocked app / Clip query)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from classes import edit_ops_handlers as h


class _Clip:
    def __init__(self, cid, data):
        self.id = cid
        self.data = dict(data)
        self.data["id"] = cid
        self._deleted = False

    def save(self):
        store[self.id] = self

    def delete(self):
        self._deleted = True
        store.pop(self.id, None)


store: dict[str, _Clip] = {}


@pytest.fixture(autouse=True)
def _clear_store():
    store.clear()
    yield
    store.clear()


def _install(monkeypatch, clips, layers):
    store.clear()
    for c in clips:
        store[c.id] = c

    class ClipQuery:
        @staticmethod
        def filter(**_kw):
            return list(store.values())

        @staticmethod
        def get(id=None, **_kw):
            return store.get(str(id))

    class TrackQuery:
        @staticmethod
        def get(number=None, **_kw):
            for L in layers:
                if int(L.get("number") or 0) == int(number):
                    tr = SimpleNamespace(data=dict(L), id=L.get("id"))
                    def save(self=tr):
                        for i, row in enumerate(layers):
                            if int(row.get("number") or 0) == int(self.data.get("number") or 0):
                                layers[i] = dict(self.data)
                    tr.save = save
                    return tr
            return None

    updates = SimpleNamespace(transaction_id=None)
    project = MagicMock()
    project.get = lambda key, default=None: {
        "layers": layers,
        "edit_mode": "insert",
        "fps": {"num": 24, "den": 1},
    }.get(key, default)
    project.set = MagicMock()
    app = SimpleNamespace(updates=updates, project=project, thread=lambda: None)

    monkeypatch.setattr(h, "_app", lambda: app)
    monkeypatch.setattr("classes.query.Clip", ClipQuery, raising=False)
    # Handlers import Clip inside functions
    import classes.query as query_mod
    monkeypatch.setattr(query_mod, "Clip", ClipQuery)
    monkeypatch.setattr(query_mod, "Track", TrackQuery)

    # Avoid Qt resolve path — inject resolve helpers
    def fake_resolve(**kwargs):
        cid = str(kwargs.get("timeline_clip_id") or "").strip()
        clip = store.get(cid)
        if not clip:
            return SimpleNamespace(ok=False, error="Error: not found", clip=None)
        return SimpleNamespace(ok=True, error="", clip=clip)

    monkeypatch.setattr("classes.tool_handlers._resolve_timeline_clip_for_tool", fake_resolve)
    monkeypatch.setattr(h, "project_fps_fraction", lambda: 24)
    return app


def test_roll_refuses_locked_without_opening_undo(monkeypatch):
    layers = [{"number": 1, "lock": True, "sync_locked": True}]
    a = _Clip("a", {"position": 0.0, "start": 0.0, "end": 2.0, "layer": 1})
    b = _Clip("b", {"position": 2.0, "start": 0.0, "end": 2.0, "layer": 1})
    app = _install(monkeypatch, [a, b], layers)
    out = h.roll_edit(clip_a_id="a", clip_b_id="b", frames="4")
    assert out.startswith("Error:")
    assert "locked" in out.lower()
    assert app.updates.transaction_id is None
    assert a.data["end"] == 2.0


def test_lift_deletes_and_returns_receipt(monkeypatch):
    layers = [{"number": 1, "lock": False, "sync_locked": True}]
    a = _Clip("a", {"position": 0.0, "start": 0.0, "end": 2.0, "layer": 1})
    b = _Clip("b", {"position": 2.0, "start": 0.0, "end": 2.0, "layer": 1})
    _install(monkeypatch, [a, b], layers)
    out = h.lift_clips(clipIds="b")
    assert out.startswith("OK")
    assert "b" not in store
    assert "EDIT_OPS_RECEIPT=" in out
    receipt = json.loads(out.split("EDIT_OPS_RECEIPT=", 1)[1])
    assert receipt["op"] == "lift"
    assert receipt["ok"] is True


def test_extract_closes_gap(monkeypatch):
    layers = [{"number": 1, "lock": False, "sync_locked": True}]
    a = _Clip("a", {"position": 0.0, "start": 0.0, "end": 2.0, "layer": 1})
    b = _Clip("b", {"position": 2.0, "start": 0.0, "end": 2.0, "layer": 1})
    c = _Clip("c", {"position": 4.0, "start": 0.0, "end": 2.0, "layer": 1})
    _install(monkeypatch, [a, b, c], layers)
    out = h.extract_clips(clipIds="b")
    assert out.startswith("OK")
    assert "b" not in store
    assert abs(store["c"].data["position"] - 2.0) < 1e-6


def test_soft_roll_caps_frames(monkeypatch):
    layers = [{"number": 1, "lock": False, "sync_locked": True}]
    a = _Clip("a", {"position": 0.0, "start": 0.0, "end": 5.0, "layer": 1})
    b = _Clip("b", {"position": 5.0, "start": 0.0, "end": 5.0, "layer": 1})
    _install(monkeypatch, [a, b], layers)

    def fake_pair(**_kw):
        return SimpleNamespace(ok=True, clip_a=store["a"], clip_b=store["b"], error="")

    monkeypatch.setattr("classes.tool_handlers._resolve_clip_pair_for_tool", fake_pair)
    out = h.roll_edit(clip_a_id="a", clip_b_id="b", frames="100", soft="true")
    assert out.startswith("OK")
    # Cap is 12f at 24fps → end moves by 12/24 = 0.5s
    assert abs(store["a"].data["end"] - 5.5) < 1e-6


def test_handlers_registered_on_agent_map():
    from classes.tool_handlers import AGENT_TOOL_HANDLERS
    for name in h.HANDLER_MAP:
        assert name in AGENT_TOOL_HANDLERS
        assert AGENT_TOOL_HANDLERS[name] is h.HANDLER_MAP[name]
