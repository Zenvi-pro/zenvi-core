"""Compact editor chat snapshot: ids and times, not a ToolReceipt JSON blob."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from classes.tool_handlers import build_editor_snapshot_for_chat


class _File:
    def __init__(self, file_id, name, duration=10.0):
        self.id = file_id
        self.data = {"name": name, "duration": duration, "path": name}


class _Clip:
    def __init__(self, clip_id, name, position, end):
        self.id = clip_id
        self.data = {
            "title": name,
            "position": position,
            "start": 0.0,
            "end": end,
            "file_id": "F1",
            "ui": {"audio_data": [0.01] * 1000},
        }


def test_snapshot_has_no_contract_blob_and_stays_under_cap(monkeypatch):
    files = [_File(f"F{i}", f"clip{i}.wav") for i in range(8)]
    clips = [_Clip(f"C{i}", f"clip{i}.wav", i * 2.0, 2.0) for i in range(50)]
    monkeypatch.setattr("classes.query.File.filter", staticmethod(lambda: files))
    monkeypatch.setattr("classes.query.Clip.filter", staticmethod(lambda: clips))
    monkeypatch.setattr(
        "classes.twelvelabs_match.twelvelabs_is_indexed", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "classes.twelvelabs_match.get_index_block", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        "classes.tool_handlers._summary_preview_for_file_data", lambda *_a, **_k: ""
    )

    out = build_editor_snapshot_for_chat(max_chars=5500)
    assert '"contract"' not in out
    assert '"data"' not in out
    assert "legacy_text" not in out
    assert "audio_data" not in out
    assert "MEDIA_BIN:" in out
    assert "TIMELINE:" in out
    assert "id=C0" in out
    assert len(out) <= 5500 + len("[/Editor snapshot]\n")
