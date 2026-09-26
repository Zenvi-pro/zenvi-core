"""query.py shares waveform sample vectors across deep copies."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import classes.query as query_mod


class _ClipType:
    object_name = "clips"
    object_key = ["clips"]


def test_audio_data_shared_not_copied(monkeypatch):
    audio = [0.1] * 5000
    child = {
        "id": "C1",
        "title": "x",
        "ui": {"audio_data": audio},
        "position": 0.0,
    }
    updates = MagicMock(data_version=1)
    app = MagicMock(updates=updates)
    monkeypatch.setattr(query_mod, "get_app", lambda: app)

    # Reset class cache
    query_mod.QueryObject._cache = {}
    query_mod.QueryObject._cache_version = -1

    cached = query_mod.QueryObject._get_cached_child(_ClipType, child, 1)
    assert cached is not child
    assert cached["title"] == "x"
    assert cached["ui"]["audio_data"] is audio

    # A normal deepcopy would not share
    plain = copy.deepcopy(child)
    assert plain["ui"]["audio_data"] is not audio
