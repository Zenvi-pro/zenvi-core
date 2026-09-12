"""search_clips renders raw seconds (3dp) and degraded hits without keep windows."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes import tool_handlers  # noqa: E402


def _run_search(results):
    info = {
        "index_id": "idx-1",
        "index_name": "zenvi-proj",
        "indexed_count": 1,
        "video_map": {"vid-a": {"file_id": "file-a", "name": "a.mp4"}},
    }
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.search.return_value = {"results": results}
    with patch(
        "classes.project_tl_index.collect_project_twelvelabs_index",
        return_value=info,
    ), patch(
        "classes.api_client.get_backend_client",
        return_value=client,
    ):
        return tool_handlers.search_clips(query="dog jumps", top_k="5")


def _run_search_query(results, query):
    info = {
        "index_id": "idx-1",
        "index_name": "zenvi-proj",
        "indexed_count": 1,
        "video_map": {"vid-a": {"file_id": "file-a", "name": "a.mp4"}},
    }
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.search.return_value = {"results": results}
    with patch(
        "classes.project_tl_index.collect_project_twelvelabs_index",
        return_value=info,
    ), patch(
        "classes.api_client.get_backend_client",
        return_value=client,
    ):
        return tool_handlers.search_clips(query=query, top_k="5")


def test_search_clips_emits_raw_seconds_three_dp():
    out = _run_search([{
        "video_id": "vid-a",
        "start": 83.4,
        "end": 91.2,
        "peak": 87.1,
        "rank": 1,
        "filename": "a.mp4",
        "role": "action",
        "degraded": False,
    }])
    assert "start_seconds=83.400" in out
    assert "end_seconds=91.200" in out
    assert "peak_seconds=87.100" in out
    assert "keep window 1:23-1:31" not in out
    assert "83.400" in out


def test_search_clips_round_trip_seconds():
    start, end, peak = 83.4, 91.2, 87.1
    out = _run_search([{
        "video_id": "vid-a",
        "start": start,
        "end": end,
        "peak": peak,
        "rank": 1,
        "filename": "a.mp4",
    }])
    import re
    m = re.search(
        r"start_seconds=([0-9.]+) end_seconds=([0-9.]+) peak_seconds=([0-9.]+)",
        out,
    )
    assert m
    assert abs(float(m.group(1)) - start) < 1e-3
    assert abs(float(m.group(2)) - end) < 1e-3
    assert abs(float(m.group(3)) - peak) < 1e-3


def test_degraded_hit_is_not_keep_window():
    out = _run_search([{
        "video_id": "vid-a",
        "start": 0.0,
        "end": 40.0,
        "rank": 1,
        "filename": "a.mp4",
        "role": "orientation",
        "degraded": True,
    }])
    assert "keep window" not in out
    assert "chapter-level match only" in out
    hit_line = [ln for ln in out.splitlines() if "media_bin_file_id=file-a" in ln][0]
    assert "start_seconds=" not in hit_line


# --- #167 follow-up: the best match must survive truncation and be findable ---

def _hit(start, end, rank):
    return {
        "video_id": "vid-a",
        "start": start,
        "end": end,
        "peak": (start + end) / 2.0,
        "rank": rank,
        "filename": "a.mp4",
        "role": "action",
        "degraded": False,
    }


def test_best_match_is_not_truncated_away_by_chronological_cutoff():
    """rank=1 sits late in time; a chronological top-8 would drop it entirely."""
    hits = [_hit(float(i * 10), float(i * 10 + 5), rank=i + 2) for i in range(12)]
    hits.append(_hit(300.0, 307.5, rank=1))
    out = _run_search(hits)
    assert "start_seconds=300.000" in out, out


def test_best_match_is_marked_so_the_agent_can_tell_which_to_place():
    out = _run_search([
        _hit(0.0, 7.5, rank=1),
        _hit(7.5, 25.5, rank=3),
        _hit(0.0, 2.2, rank=46),
    ])
    best_line = [l for l in out.splitlines() if "start_seconds=0.000" in l and "end_seconds=7.500" in l]
    assert best_line, out
    assert "best match" in best_line[0], out
    other = [l for l in out.splitlines() if "end_seconds=25.500" in l]
    assert other and "best match" not in other[0], out


def test_occurrences_stay_in_time_order():
    out = _run_search([
        _hit(30.0, 35.0, rank=1),
        _hit(10.0, 15.0, rank=5),
        _hit(20.0, 25.0, rank=3),
    ])
    starts = [
        float(l.split("start_seconds=")[1].split(" ")[0])
        for l in out.splitlines() if "start_seconds=" in l and l.strip()[0].isdigit()
    ]
    assert starts == sorted(starts), out


def test_listed_row_numbers_match_what_an_ordinal_resolves_to():
    """A listed "3." must be the window "the 3rd time" returns, even after
    rank selection drops rows from the display."""
    hits = [_hit(float(i * 10), float(i * 10 + 5), rank=20 - i) for i in range(12)]
    listed = _run_search(hits)
    rows = [l.strip() for l in listed.splitlines() if l.strip()[:1].isdigit() and ". start_seconds=" in l]
    assert rows, listed
    for row in rows:
        n = int(row.split(".")[0])
        start = float(row.split("start_seconds=")[1].split(" ")[0])
        nth = _run_search_query(hits, f"dog jumps the {n}th time")
        assert f"start_seconds={start:.3f}" in nth, (n, row, nth)


def test_late_best_match_is_both_kept_and_marked():
    hits = [_hit(float(i * 10), float(i * 10 + 5), rank=i + 2) for i in range(12)]
    hits.append(_hit(300.0, 307.5, rank=1))
    out = _run_search(hits)
    line = [l for l in out.splitlines() if "start_seconds=300.000" in l]
    assert line, out
    assert "best match" in line[0], out


def test_ninth_best_by_rank_is_omitted_even_when_early():
    hits = [_hit(float(100 + i * 10), float(105 + i * 10), rank=i + 1) for i in range(8)]
    hits.append(_hit(0.0, 5.0, rank=99))
    out = _run_search(hits)
    assert "start_seconds=0.000" not in out, out
    assert "start_seconds=100.000" in out, out
