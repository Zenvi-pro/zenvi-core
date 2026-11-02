"""A stored OMP thread count below the detected default is reset once (it starved preview)."""

from __future__ import annotations

import json

import pytest

FLAG = "ompThreadsDefaultApplied"
OMP = "omp_threads_number"


@pytest.fixture
def load_store(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    pytest.importorskip("openshot")  # classes.settings imports it
    from classes import info, settings

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(info, "USER_PATH", str(user_dir))
    monkeypatch.setattr(settings, "lib_default_thread_counts", lambda: (12, 8))
    path = user_dir / "openshot.settings"

    def load(saved=None):
        if saved is not None:
            path.write_text(json.dumps([{"setting": k, "value": v} for k, v in saved.items()]), encoding="utf-8")
        store = settings.SettingStore()
        store.load()
        return store

    return load


@pytest.mark.parametrize("saved, expected", [
    # Stale low count on an install that predates the reset.
    ({OMP: 2}, 12),
    # Above the default is someone's deliberate choice.
    ({OMP: 16}, 16),
    ({OMP: 12}, 12),
    # Already reset once: a low count set since then is the user's call.
    ({OMP: 2, FLAG: True}, 2),
])
def test_existing_install(load_store, saved, expected):
    assert load_store(saved).get(OMP) == expected


def test_a_low_count_chosen_after_the_reset_survives_restarts(load_store):
    store = load_store({OMP: 2})
    assert store.get(OMP) == 12
    store.set(OMP, 3)
    store.save()
    assert load_store().get(OMP) == 3


def test_new_install_uses_the_detected_default(load_store):
    assert load_store().get(OMP) == 12
