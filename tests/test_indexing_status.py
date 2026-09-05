"""Per-file indexing status derivation (pure: no Qt, no project data)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from classes.indexing_status import (  # noqa: E402
    FAILED,
    NONE,
    PENDING,
    RUNNING,
    SUCCESS,
    derive_indexing_status,
)


def test_no_metadata_is_none():
    st = derive_indexing_status(None)
    assert st.state == NONE
    assert st.tooltip == ""


def test_empty_metadata_is_none():
    assert derive_indexing_status({}).state == NONE


def test_active_worker_is_running():
    st = derive_indexing_status({}, is_active=True)
    assert st.state == RUNNING


def test_progress_phase_is_running_with_phase_label():
    st = derive_indexing_status({}, progress={"phase": "indexing", "percent": -1})
    assert st.state == RUNNING
    assert "Indexing for search" in st.tooltip


def test_upload_percent_shown_in_tooltip():
    st = derive_indexing_status({}, progress={"phase": "uploading", "percent": 42})
    assert st.state == RUNNING
    assert "42%" in st.tooltip


def test_done_phase_is_not_running():
    st = derive_indexing_status({"analyzed": True}, progress={"phase": "done", "percent": 100})
    assert st.state == SUCCESS


def test_index_block_status_indexing_is_running():
    st = derive_indexing_status({"index": {"status": "indexing"}})
    assert st.state == RUNNING


def test_legacy_twelvelabs_block_status_indexing_is_running():
    st = derive_indexing_status({"twelvelabs": {"status": "indexing"}})
    assert st.state == RUNNING


def test_analyzed_is_success():
    st = derive_indexing_status({"analyzed": True, "short_summary": "a dog"})
    assert st.state == SUCCESS


def test_ready_index_without_summary_is_success():
    st = derive_indexing_status(
        {"index": {"status": "ready", "index_id": "i1", "video_id": "v1"}}
    )
    assert st.state == SUCCESS


def test_ai_metadata_error_is_failed_with_error_tooltip():
    st = derive_indexing_status({"error": "backend exploded"})
    assert st.state == FAILED
    assert st.tooltip == "backend exploded"


def test_index_block_failed_is_failed():
    st = derive_indexing_status({"index": {"status": "failed", "error": "upload rejected"}})
    assert st.state == FAILED
    assert st.tooltip == "upload rejected"


def test_failed_index_over_previous_good_analysis_is_failed():
    """_apply_ai_metadata keeps old usable content and records the new error."""
    st = derive_indexing_status({"analyzed": True, "description": "old good", "error": "reindex failed"})
    assert st.state == FAILED
    assert st.tooltip == "reindex failed"


def test_running_beats_stale_error():
    st = derive_indexing_status({"error": "old failure"}, is_active=True)
    assert st.state == RUNNING


def test_skip_reason_is_pending_not_failed():
    st = derive_indexing_status(
        {"skip_reason": "Clip duration 41.0 min exceeds the 30-minute limit."}
    )
    assert st.state == PENDING
    assert "30-minute limit" in st.tooltip


def test_credit_blocked_skip_block_is_pending():
    st = derive_indexing_status(
        {"index": {"status": "skipped", "error": "Not enough credits"}}
    )
    assert st.state == PENDING
    assert st.tooltip == "Not enough credits"


def test_status_is_hashable_and_has_label():
    st = derive_indexing_status({"analyzed": True})
    assert st.label
    assert isinstance(st.label, str)
