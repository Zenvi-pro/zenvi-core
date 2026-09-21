"""Dev-only frame-alignment guard on UpdateManager.insert/update."""

from __future__ import annotations

from fractions import Fraction

import classes.updates as updates_mod
from classes.updates import UpdateManager, reset_frame_guard_warnings


def _stub_fps(monkeypatch):
    monkeypatch.setattr(
        "classes.clip_utils.project_fps_fraction",
        lambda: Fraction(30, 1),
    )


def test_guard_warns_on_off_grid_write(monkeypatch):
    reset_frame_guard_warnings()
    _stub_fps(monkeypatch)
    monkeypatch.setenv("ZENVI_FRAME_GUARD", "1")
    warnings = []
    monkeypatch.setattr(
        updates_mod.log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mgr = UpdateManager()
    mgr.updateListeners = []
    mgr.update(["clips", {"id": "c1"}], {"position": 1.111111, "start": 0.0, "end": 1.0})
    assert warnings
    assert "frame_time guard" in warnings[0][0][0]


def test_guard_silent_on_aligned_write(monkeypatch):
    reset_frame_guard_warnings()
    _stub_fps(monkeypatch)
    monkeypatch.setenv("ZENVI_FRAME_GUARD", "1")
    warnings = []
    monkeypatch.setattr(
        updates_mod.log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mgr = UpdateManager()
    mgr.updateListeners = []
    mgr.update(["clips", {"id": "c1"}], {"position": 1.0, "start": 0.0, "end": 2.0})
    assert not warnings


def test_guard_skips_load(monkeypatch):
    reset_frame_guard_warnings()
    _stub_fps(monkeypatch)
    monkeypatch.setenv("ZENVI_FRAME_GUARD", "1")
    warnings = []
    monkeypatch.setattr(
        updates_mod.log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mgr = UpdateManager()
    mgr.updateListeners = []
    mgr.load({"clips": [{"position": 1.111111, "start": 0.0, "end": 1.0}]})
    assert not warnings


def test_guard_disabled(monkeypatch):
    reset_frame_guard_warnings()
    _stub_fps(monkeypatch)
    monkeypatch.setenv("ZENVI_FRAME_GUARD", "0")
    warnings = []
    monkeypatch.setattr(
        updates_mod.log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mgr = UpdateManager()
    mgr.updateListeners = []
    mgr.insert(["clips"], {"position": 1.111111, "start": 0.0, "end": 1.0})
    assert not warnings


def test_guard_dedups_same_site(monkeypatch):
    reset_frame_guard_warnings()
    _stub_fps(monkeypatch)
    monkeypatch.setenv("ZENVI_FRAME_GUARD", "1")
    warnings = []
    monkeypatch.setattr(
        updates_mod.log,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )
    mgr = UpdateManager()
    mgr.updateListeners = []

    def write_off_grid(pos):
        mgr.update(["clips"], {"position": pos})

    write_off_grid(1.111111)
    write_off_grid(2.222222)
    assert len(warnings) == 1
