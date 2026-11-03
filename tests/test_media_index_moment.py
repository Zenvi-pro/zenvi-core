"""A moment pack: one range, bounded, with the pictures only when they help."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import moment as M, review as R  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from eval import corpus  # noqa: E402


def make_shots(n=20, length=3.0):
    return [{"id": i, "start": i * length, "end": (i + 1) * length,
             "watch": {"description": f"shot {i}", "shot_type": "wide"}, "motion": {"class": "static"},
             "quality": {"flags": ["shaky"] if i == 4 else [], "score": 0.8, "sharpness": 100.0, "shake": 0.1}, "look": None, "look_extras": None} for i in range(n)]


class FakeIndex:
    def __init__(self, shots, duration, media_type="video", audio=None, sentences=()):
        self.shots, self.duration, self.media_type, self.audio, self.rows, self.sha = shots, duration, media_type, audio or {}, [1], "a" * 64
        self.sentences, self.warnings, self.layers, self.not_applicable, self.name, self.file_id = list(sentences), [], {}, [], "cuts.mp4", "V1"
        self.orientation, self.look_file = "landscape", {}

    def shots_between(self, a, b):
        return [s for s in self.shots if s["end"] > a and s["start"] < b]


# ============================ the pure parts ============================
def test_a_range_must_be_given_and_is_at_most_a_minute_and_held_to_the_file():
    with pytest.raises(ValueError, match="one range"):
        M.check_range(None, 5)
    with pytest.raises(ValueError, match="at most 60"):
        M.check_range(0, 90)
    with pytest.raises(ValueError, match="empty or outside"):
        M.check_range(100, 110, duration=60)
    assert M.check_range(10, 500, duration=60) == (10.0, 60.0)


def test_shots_are_the_ones_touching_the_range_and_trimmed_to_a_bound():
    fi = FakeIndex(make_shots(), 60.0)
    got = M.shots_in(fi, 4.0, 11.0)
    assert [s["id"] for s in got["shots"]] == [1, 2, 3] and got["total"] == 3 and got["truncated"] is None
    assert got["shots"][0]["inside"] == [4.0, 6.0] and got["shots"][2]["inside"] == [9.0, 11.0]
    assert [s["quality"]["flags"] for s in M.shots_in(fi, 12.0, 15.0)["shots"]] == [["shaky"]]
    big = M.shots_in(FakeIndex(make_shots(40, 1.0), 40.0), 0, 40)
    assert len(big["shots"]) == M.MAX_SHOTS and big["total"] == 40 and big["truncated"] == 28


def test_words_are_the_ones_in_the_range_and_long_speech_is_cut_with_where_it_stopped():
    words = [{"text": f"w{i}", "startSec": i * 0.5, "endSec": i * 0.5 + 0.4} for i in range(10)]
    got = M.words_in(words, 1.0, 3.0)
    assert got["text"] == "w2 w3 w4 w5" and got["count"] == 4 and got["first"] == 1.0 and got["last"] == 2.9 and got["truncated_at"] is None
    assert M.words_in(words, 50, 60)["count"] == 0
    cut = M.words_in(words, 0, 5, max_chars=10)
    assert cut["text"] == "w0 w1 w2" and cut["truncated_at"] == 1.5 and cut["count"] == 10 and cut["last"] == 1.4


def test_the_picture_of_sound_is_included_only_when_asked_or_when_an_edge_here_is_uncertain():
    sections = [{"label": "intro", "start": 0}, {"label": "build", "start": 20, "confidence": 0.4, "source": "ramp"}, {"label": "peak", "start": 40, "confidence": 1.0}]
    assert M.audio_picture_worth(sections, 15, 25, 60, False)["include"] is True
    off = M.audio_picture_worth(sections, 30, 38, 60, False)
    assert off["include"] is False and "numbers" in off["why"]
    assert M.audio_picture_worth(sections, 30, 38, 60, True) == {"include": True, "why": "asked for"}
    assert M.audio_picture_worth([], 0, 10, 60, False)["include"] is False


def test_timeline_uses_are_the_clips_playing_some_of_the_range_in_order_and_bounded():
    def clip(i, start, src_in, src_out, fid="V1"):
        return R.TimelineClip(id=i, file_id=fid, start=start, end=start + src_out - src_in, src_in=src_in, src_out=src_out)
    clips = [clip("b", 20, 8, 12), clip("a", 0, 0, 5), clip("other", 5, 0, 30, fid="V2"), clip("c", 30, 3, 9), clip("d", 40, 4, 6), clip("e", 50, 4, 6)]
    got = M.timeline_uses(clips, "V1", 4.0, 10.0)
    assert [c.id for c in got] == ["a", "b", "c"] and len(M.timeline_uses(clips, "V1", 0, 100)) == M.MAX_TIMELINE_USES
    assert M.timeline_uses(clips, "V1", 50, 60) == []


# ============================ the tool ============================
@pytest.fixture(autouse=True)
def need_ffmpeg():
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    import subprocess
    silent, _ = corpus.hard_cuts()
    path = tmp_path / "cuts_audio.mp4"
    subprocess.run([corpus.ffmpeg(), "-y", "-v", "error", "-i", str(silent), "-f", "lavfi", "-i", "sine=frequency=440:duration=24", "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)], check=True)
    shelf = Shelf(str(tmp_path / "shelf"))
    sha = "f" * 64
    shelf.set_source(sha, duration=24.0, has_audio=True, media_type="video")
    shelf.write_json(sha, "speech.json", {"words": [{"text": "hello", "startSec": 5.0, "endSec": 5.4}, {"text": "there", "startSec": 5.5, "endSec": 5.9}]})
    video = SimpleNamespace(id="V1", data={"name": "cuts.mp4", "path": str(path), "media_type": "video", "fingerprint": {"sha256": sha}})
    fi = FakeIndex(make_shots(8, 3.0), 24.0)
    state = SimpleNamespace(fi=fi, shelf=shelf, timeline=[R.TimelineClip(id="T1", name="cuts", file_id="V1", layer=1, kind="video", start=10.0, end=18.0, src_in=4.0, src_out=12.0,
                                                                          has_audio=True, role="speech", gain_db=0.0)])
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [video])
    monkeypatch.setattr(P, "_index_for", lambda f, *a, **k: state.fi)
    monkeypatch.setattr(P, "_speech_layer", lambda sh, s: shelf.read_json(s, "speech.json") or {})
    monkeypatch.setattr(P, "_timeline_facts", lambda: (state.timeline, {1: P.context.Track(layer=1, label="V1", locked=False)}, [], {}, None))
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    return state


def call(**kw):
    out = REGISTRY["get_moment_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


def test_a_moment_has_frames_shots_words_notes_and_where_it_is_used(setup):
    head, r = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0)
    assert r["changed"] is False and r["range"] == [4.0, 10.0] and [s["id"] for s in r["shots"]] == [1, 2, 3]
    assert r["words"]["text"] == "hello there" and Path(r["frames"]["image_path"]).is_file() and len(r["frames"]["tiles"]) == 8
    assert "shot 1" in r["notes"] and "FILE " not in r["notes"]
    use = r["timeline"]["uses"][0]
    assert use["timeline_clip_id"] == "T1" and use["source"] == [4.0, 12.0] and use["track_locked"] is False
    assert head.startswith("cuts.mp4 4.00-10.00 s: 3 shot(s), 2 word(s), frames")
    assert r["audio_picture"]["included"] is False and "numbers" in r["audio_picture"]["why"]


def test_frames_can_be_left_out_and_the_range_is_required_and_bounded(setup):
    _, r = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0, frames=0)
    assert r["frames"] is None
    assert "missing required" in REGISTRY["get_moment_tool"].func(file_ids=["V1"], end_seconds=5.0)
    setup.fi.duration = 300.0
    assert "at most 60" in REGISTRY["get_moment_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=100.0)


def test_a_spectrogram_comes_when_asked_and_when_the_numbers_are_unsure(setup):
    _, asked = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0, frames=0, audio_picture=True)
    assert asked["audio_picture"]["included"] is True and Path(asked["audio_picture"]["image_path"]).is_file()
    setup.fi.audio = {"music": {"sections": [{"label": "intro", "start": 0}, {"label": "build", "start": 7.0, "confidence": 0.4, "source": "ramp"}]}}
    _, unsure = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0, frames=0)
    assert unsure["audio_picture"]["included"] is True and "uncertain" in unsure["audio_picture"]["why"]
    _, far = call(file_ids=["V1"], start_seconds=14.0, end_seconds=20.0, frames=0)
    assert far["audio_picture"]["included"] is False


def test_a_part_that_is_not_on_the_timeline_says_so_and_a_missing_timeline_does_not_spoil_the_pack(setup, monkeypatch):
    _, r = call(file_ids=["V1"], start_seconds=18.0, end_seconds=22.0, frames=0)
    assert r["timeline"] == {"uses": [], "note": "this part of the file is not on the timeline"}
    from classes.editor_tools import media_index_tools_precision as P
    monkeypatch.setattr(P, "_timeline_facts", lambda: (_ for _ in ()).throw(RuntimeError("no project open")))
    _, r2 = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0, frames=0)
    assert "no project open" in r2["timeline"]["error"] and len(r2["shots"]) == 3


def test_a_frame_failure_is_reported_inside_the_pack_not_raised(setup, monkeypatch):
    from classes.media_index import contact
    monkeypatch.setattr(contact, "decode_frames", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad read")))
    _, r = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0)
    assert "bad read" in r["frames"]["error"] and r["words"]["count"] == 2


def test_an_unindexed_file_is_an_error(setup):
    setup.fi = None
    assert "no saved index" in REGISTRY["get_moment_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=5.0)


def test_asking_for_a_spectrogram_of_a_file_without_sound_says_why_there_is_none(setup):
    setup.shelf.set_source("f" * 64, duration=24.0, has_audio=False, media_type="video")
    _, r = call(file_ids=["V1"], start_seconds=4.0, end_seconds=10.0, frames=0, audio_picture=True)
    assert r["audio_picture"] == {"included": False, "why": "this file has no audio track"}
