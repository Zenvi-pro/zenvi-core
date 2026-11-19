"""The editor snapshot every chat turn carries: the EDIT_STATE line (contract, brief headline, index summary).

The line sits before the media list so truncation, which cuts the end, can never drop it, and it is computed from project
data already in memory: no disk on the GUI thread (AGENTS.md).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import edit_contract as EC  # noqa: E402
from classes import tool_handlers as th  # noqa: E402
from classes.editor_tools import media_index_tools_edit as TE  # noqa: E402


def media(i, *, ready=False, analyzed=False, kind="video", subclip=False):
    ai = {}
    if ready:
        ai["index"] = {"status": "ready", "index_id": "idx", "index_name": "idx", "video_id": f"F{i}", "provider": "media-index-v2",
                       "media_type": kind, "v2": True, "fingerprint": "sha"}      # the block media_index/job.py _v2_index_block writes
    if analyzed:
        ai["analyzed"] = True
    data = {"name": f"clip{i}.mp4", "duration": 10.0, "media_type": kind, "ai_metadata": ai}
    if subclip:
        data["zenvi_subclip"] = True
    return SimpleNamespace(id=f"F{i}", data=data)


@pytest.fixture
def snap(monkeypatch):
    state = SimpleNamespace(files=[], store={TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}}, timeline="TL: 3 clips")
    import classes.query as Q
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: state.files), raising=False)
    monkeypatch.setattr(th, "get_timeline_state", lambda: state.timeline)
    monkeypatch.setattr(th, "_summary_preview_for_file_data", lambda d: "")
    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: state.store.get(k, d)))
    monkeypatch.setattr(TE, "get_app", lambda: app)
    return state


def edit_state_of(text):
    lines = [ln for ln in text.splitlines() if ln.startswith("EDIT_STATE:")]
    return lines[0] if lines else None


def test_a_free_project_gets_the_index_summary_only(snap):
    snap.files = [media(1, ready=True), media(2, analyzed=True), media(3), media(4)]
    line = edit_state_of(th.build_editor_snapshot_for_chat())
    assert line == "EDIT_STATE: index: videos=4 ready=1 analyzed=1 not_indexed=2"


def test_a_finished_piece_shows_the_contract_the_brief_and_the_audit(snap):
    snap.files = [media(i, ready=True) for i in range(1, 5)]
    c = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    c = EC.advance(c, "review_done", needs=["dead_air"])
    snap.store[EC.CONTRACT_KEY] = c
    snap.store[TE.BRIEF_KEY] = {"form": "vlog", "target_seconds": 210, "vibe": "warm cinematic"}
    line = edit_state_of(th.build_editor_snapshot_for_chat())
    assert "contract=finished_piece/audited" in line and "form=vlog" in line and "target=210s" in line
    assert "index: videos=4 ready=4" in line and "audit: needs=1 rounds=1" in line


def test_a_brief_alone_is_enough_to_show_the_state(snap):
    snap.files = [media(1, ready=True)]
    snap.store[TE.BRIEF_KEY] = {"form": "reel", "target_seconds": 45}
    line = edit_state_of(th.build_editor_snapshot_for_chat())
    assert "contract=free" in line and "/" not in line.split("brief:")[0] and "form=reel" in line


def test_a_brief_that_unlocks_placement_reads_as_briefed(snap):
    snap.store[EC.CONTRACT_KEY] = EC.new_contract(EC.MODE_FINISHED)
    snap.store[TE.BRIEF_KEY] = {"form": "vlog", "target_seconds": 90}
    snap.files = [media(1, ready=True)]
    assert "contract=finished_piece/briefed" in edit_state_of(th.build_editor_snapshot_for_chat())


def test_audio_images_and_subclips_are_not_counted_as_videos(snap):
    snap.files = [media(1, ready=True), media(2, kind="audio"), media(3, kind="image"), media(4, subclip=True)]
    assert "index: videos=1 ready=1 analyzed=0 not_indexed=0" in edit_state_of(th.build_editor_snapshot_for_chat())


def test_a_project_with_no_video_and_no_brief_has_no_line(snap):
    snap.files = [media(1, kind="audio")]
    assert edit_state_of(th.build_editor_snapshot_for_chat()) is None


def test_the_line_survives_truncation_of_a_huge_project(snap):
    snap.files = [media(i, ready=True) for i in range(1, 60)]
    snap.timeline = "T" * 20000
    snap.store[EC.CONTRACT_KEY] = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    snap.store[TE.BRIEF_KEY] = {"form": "vlog", "target_seconds": 200}
    text = th.build_editor_snapshot_for_chat()
    assert len(text) <= 5500 + len("[/Editor snapshot]\n") + 4
    assert "(truncated)" in text and "contract=finished_piece/briefed" in edit_state_of(text)


def test_the_line_is_short_even_for_hostile_briefs(snap):
    snap.files = [media(1, ready=True)]
    snap.store[EC.CONTRACT_KEY] = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    snap.store[TE.BRIEF_KEY] = {"form": "vlog", "target_seconds": 60, "vibe": "x" * 5000}
    assert len(edit_state_of(th.build_editor_snapshot_for_chat())) <= 700


def test_a_broken_contract_store_never_breaks_the_snapshot(snap, monkeypatch):
    snap.files = [media(1, ready=True)]
    monkeypatch.setattr(TE, "read_contract", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    text = th.build_editor_snapshot_for_chat()
    assert text.startswith("[Editor snapshot]") and "MEDIA_BIN:" in text and edit_state_of(text) is None


def test_the_snapshot_never_reads_the_media_index_from_disk(snap, monkeypatch):
    from classes.media_index import store

    def boom(*a, **k):
        raise AssertionError("the snapshot must not touch the media-index shelf (disk) on the GUI thread")
    monkeypatch.setattr(store.Shelf, "layer_ready", boom, raising=False)
    snap.files = [media(1, ready=True), media(2)]
    assert edit_state_of(th.build_editor_snapshot_for_chat()) is not None


def test_the_existing_snapshot_parts_are_unchanged(snap):
    snap.files = [media(1, ready=True)]
    text = th.build_editor_snapshot_for_chat()
    assert text.startswith("[Editor snapshot]\nProject files count: 1\nNOTE: media-bin files exist independently")
    assert "MEDIA_BIN:\n  media_bin_file_id=F1 name='clip1.mp4' duration=10.00s analyzed=False indexed=True" in text
    assert "TIMELINE:\nTL: 3 clips" in text and text.endswith("[/Editor snapshot]\n")
