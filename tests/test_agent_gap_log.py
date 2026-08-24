"""Unit tests for the silent tool-gap log (classes.agent_gap_log)."""

import pytest


@pytest.fixture
def gap_log(monkeypatch, tmp_path):
    from classes import agent_gap_log as gl
    monkeypatch.setattr(gl, "GAP_LOG_PATH", str(tmp_path / "agent_tool_gaps.jsonl"))
    return gl


def test_classify_gap_flags_known_gap(gap_log):
    gap = gap_log.classify_gap("please change the project fps to 24")
    assert gap is not None
    assert "fps" in gap or "resolution" in gap


def test_classify_gap_no_match_returns_none(gap_log):
    assert gap_log.classify_gap("undo my last edit") is None
    assert gap_log.classify_gap("") is None


def test_classify_gap_self_retires_once_tool_exists(gap_log, monkeypatch):
    # Simulate the missing tool having shipped: the rule must stop firing.
    monkeypatch.setattr(
        gap_log, "_tool_names", lambda: {"set_project_setting_tool"}
    )
    assert gap_log.classify_gap("please change the project fps to 24") is None


def test_append_and_read_gaps_round_trip(gap_log):
    first = gap_log.append_gap({
        "request": "set fps to 24",
        "missing_capability": "no tool for fps",
        "session_id": "s1",
    })
    second = gap_log.append_gap({
        "request": "duck the audio",
        "missing_capability": "no tool for audio ducking",
        "session_id": "s1",
    })
    assert first["id"] != second["id"]
    assert first["resolved"] is False

    entries = gap_log.read_gaps()
    # Newest first.
    assert [e["id"] for e in entries] == [second["id"], first["id"]]
    assert entries[1]["request"] == "set fps to 24"


def test_mark_resolved(gap_log):
    entry = gap_log.append_gap({"request": "r", "missing_capability": "c", "session_id": "s"})
    assert gap_log.mark_resolved(entry["id"]) is True
    assert gap_log.read_gaps()[0]["resolved"] is True
    assert gap_log.mark_resolved("does-not-exist") is False


def test_delete_gap(gap_log):
    entry = gap_log.append_gap({"request": "r", "missing_capability": "c", "session_id": "s"})
    assert gap_log.delete_gap(entry["id"]) is True
    assert gap_log.read_gaps() == []
    assert gap_log.delete_gap(entry["id"]) is False


def test_read_gaps_empty_when_no_file(gap_log):
    assert gap_log.read_gaps() == []
