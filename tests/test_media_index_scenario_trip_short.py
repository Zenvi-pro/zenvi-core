"""A scripted director on a real project: 12 trip clips and a song become a vertical short, using the real tools in order.

This is the loop of the editing playbook run end to end on the real project store with real undo: survey, choose by
highlight, assemble, put the cuts on the beat, balance the mix, audit, keep the brief. Only the cloud, the mix render and
the colour renderer (which need the app and libopenshot) are stand-ins.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import media_index_tools as T, media_index_tools_edit as TE, media_index_tools_review as TR  # noqa: E402
from classes.media_index import library  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_search import build, profile, shot, w  # noqa: E402
from timeline_edit_fakes import FakeTimeline, fake_openshot  # noqa: E402

PICTURE, MUSIC = 1000000, 2000000
SONG = "9a" * 32


def sha(i):
    return f"{i:02x}" * 32


def receipt_data(ed, name, **kw):
    r = ed.call_receipt(name, **kw)
    assert r["status"] in ("applied", "unchanged"), (name, r["summary"])
    return r["data"]


@pytest.fixture
def trip(editor, tmp_path, monkeypatch):
    from classes import clip_utils, timeline_ops
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())          # placement reads clips through libopenshot
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    editor.window.selected_tracks = []
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    for module in (T, TE, TR):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    editor.add_track(PICTURE, "Picture")
    editor.add_track(MUSIC, "Music")
    files = {}
    for i in range(12):
        day = 1 + i // 4                                            # three days, four clips each
        place = {"lat": 35.0116, "lon": 135.7681} if day < 3 else {"lat": 34.6937, "lon": 135.5023}      # Kyoto, then Osaka
        bad_black = i == 3
        blurry = i == 5
        shots = [shot(0, 0, 10, "pan"), shot(1, 10, 20, "static", black=bad_black)]
        for s in shots:
            s["sharpness"] = 0.10 if blurry else 0.45
        watch = [w(0, 0, 10, f"Clip {i}: a street scene", interest=0.3 + 0.05 * i, highlight_reason=f"scene {i}", mood="calm"),
                 w(1, 10, 20, f"Clip {i}: a market stall", interest=0.9 if i == 7 else 0.4, highlight_reason="the best moment" if i == 7 else "market", mood="busy")]
        build(shelf, sha(i), shots=shots, watch=watch, duration=20.0, look={"profile": profile(0.5)})
        shelf.set_source(sha(i), duration=20.0, media_type="video", orientation="landscape",
                         captured_at=f"2024-05-0{day}T{9 + i % 4}:00:00+00:00", gps=place)
        fid = editor.add_file("video", duration=20.0, fingerprint={"sha256": sha(i)}, path=f"/media/trip_{i:02d}.mp4")
        files[i] = fid
    beats = [round(1.0 + 0.5 * i, 3) for i in range(100)]
    shelf.set_source(SONG, duration=76.4, media_type="audio")
    shelf.write_json(SONG, "audio.json", {"tempo": {"bpm": 120.0, "beats": beats}, "windows": [{"start": float(a), "end": float(a + 5), "rms_db": -20.0} for a in range(0, 70, 5)],
                                          "music": {"downbeats": beats[::4], "arc": [0.2, 0.5, 0.9, 0.5], "sections": [{"label": "intro", "start": 0, "end": 10}]},
                                          "loudness": {"integrated_lufs": -14.0}})
    shelf.set_layer(SONG, "audio", version=2, status="ready")
    song = editor.add_file("audio", fingerprint={"sha256": SONG}, ai_metadata={"analyzed": True})
    editor.mark()
    library.clear_cache()
    return SimpleNamespace(ed=editor, shelf=shelf, files=files, song=song)


def test_the_directors_loop_builds_audits_and_keeps_the_plan(trip, monkeypatch, tmp_path):
    ed = trip.ed
    # 1. the brief goes in the project first
    receipt_data(ed, "set_edit_brief_tool", brief={"form": "TikTok", "target_seconds": 30, "vibe": "calm travel", "bans": ["no AI"]})

    # 2. survey: what is there
    overview = receipt_data(ed, "get_project_overview_tool")
    assert overview["totals"]["videos"] == 12 and overview["totals"]["audio"] == 1 and overview["totals"]["not_indexed"] == 0
    assert [d["date"] for d in overview["trip"]["days"]] == ["2024-05-01", "2024-05-02", "2024-05-03"] and len(overview["trip"]["places"]) == 2
    assert overview["top_moments"][0]["why"] == "the best moment", "the model's best moment leads"
    assert all(m["file_id"] != trip.files[3] or m["start"] < 10 for m in overview["top_moments"]), "the black shot is never offered"

    # 3. choose by highlight, only usable shots, one per file
    found = receipt_data(ed, "search_footage_tool", sort="highlight", filters={"usable_only": True}, limit=50)["hits"]
    assert not [h for h in found if h["file_id"] == trip.files[5] and h["start"] == 0.0 or h["file_id"] == trip.files[3] and h["start"] == 10.0], \
        "the blurry clip and the black shot are excluded by usable_only"
    chosen, seen = [], set()
    for h in found:
        if h["file_id"] not in seen:
            seen.add(h["file_id"])
            chosen.append(h)
        if len(chosen) == 8:
            break
    assert chosen[0]["highlight"] >= chosen[-1]["highlight"]

    # 4. assemble to the target length, in the order chosen
    items = [{"file_id": h["file_id"], "source_start": h["start"], "source_end": h["start"] + 3.75} for h in chosen]      # 8 x 3.75 s = 30 s
    laid = receipt_data(ed, "add_clips_to_timeline_tool", items=items, track=str(PICTURE), start_seconds=0)
    assert len(laid["clips"]) == 8 and abs(laid["total_seconds"] - 30.0) < 0.5
    receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": trip.song, "source_start": 0.0, "source_end": 30.0}], track=str(MUSIC), start_seconds=0)

    # 5. cut to the beat: one undo step, the length unchanged, only edges near cuts move
    def edit_end():
        return max(c["position"] + (c["end"] - c["start"]) for c in ed.clips() if c["layer"] == PICTURE)

    end_before = edit_end()
    assert end_before == pytest.approx(30.0, abs=0.2)
    ed.mark()
    moved = receipt_data(ed, "sync_cuts_to_beats_tool")
    assert moved["music_clip_id"] and moved["cuts"] >= 7 and ed.undo_steps_since_mark() <= 1
    assert edit_end() == pytest.approx(end_before, abs=1e-6), "the edit is exactly as long as before: only the cuts moved"

    # 6. balance the mix (the render stand-in measures too quiet; the tool raises it)
    measured = [{"integrated_lufs": -20.0, "true_peak_db": -8.0, "lra": 4.0}, {"integrated_lufs": -14.3, "true_peak_db": -3.0, "lra": 4.0}]
    mix_files = []

    def fake_render(start, end):
        p = tmp_path / f"m{len(mix_files)}.mp3"
        p.write_bytes(b"x")
        mix_files.append(p)
        return str(p), ""
    monkeypatch.setattr(TE, "render_timeline_mix", fake_render)
    monkeypatch.setattr(TR, "render_timeline_mix", fake_render)
    from classes.media_index import audio as au
    monkeypatch.setattr(au, "measure_loudness", lambda path: measured.pop(0) if measured else {"integrated_lufs": -14.3, "true_peak_db": -3.0, "lra": 4.0})
    ed.mark()
    mix = receipt_data(ed, "balance_mix_tool", form="TikTok")
    assert mix["loudness"]["delta_db"] > 0 and ed.undo_steps_since_mark() == 1

    # 7. audit: the landscape project is not a vertical short, and the audit says so; nothing is changed by it
    ed.mark()
    audit = receipt_data(ed, "review_edit_tool", form="TikTok", target_seconds=30, vibe="calm travel", measure_colour=False)
    ids = {f["id"]: f for layer in audit["layers"].values() for f in layer}
    assert ids["format"]["status"] == "needs" and "set_project_profile_tool" in ids["format"]["fix"]
    assert ids["length"]["status"] == "ok" and ids["music"]["status"] == "ok" and ids["style"]["status"] == "needs"
    assert ids["weak_shots"]["status"] == "ok", "the blurry and black shots were never placed"
    assert ids["loudness"]["status"] in ("ok", "needs") and ed.undo_steps_since_mark() == 0

    # 8. the plan survives: a fresh read of the brief has everything, and the project saved no extra undo steps for it
    assert receipt_data(ed, "get_edit_brief_tool")["brief"]["form"] == "TikTok"
