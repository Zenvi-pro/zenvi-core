"""detect_beats uses the media index's tempo analysis: one beat implementation."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index.store import Shelf  # noqa: E402
from classes.speech import beats as B  # noqa: E402
from test_media_index_audio import SR, clicks, tone, write_wav  # noqa: E402


@pytest.fixture(autouse=True)
def shelf(tmp_path, monkeypatch):
    import classes.media_index as mi
    s = Shelf(str(tmp_path / "shelf"))
    monkeypatch.setattr(mi, "default_shelf", lambda: s)
    B.reset_beat_factory()
    yield s
    B.reset_beat_factory()


def test_a_click_track_gets_its_real_tempo_beats_and_bars(tmp_path):
    x, times = clicks(24, 120)
    out = B.detect_beats(write_wav(tmp_path / "c.wav", x))
    assert out["source"] == "media-index" and out["rhythmic"] is True and out["bpm"] == pytest.approx(120.0, abs=2.0)
    got = [b["timeSec"] for b in out["beats"]]
    assert abs(got[0] - times[0]) < 0.06 and abs(got[-1] - times[-1]) < 0.12
    downs = [d["timeSec"] for d in out["downbeats"]]
    assert len(downs) >= 4 and all(min(abs(d - g) for g in got) < 1e-6 for d in downs), "every downbeat is one of the beats"
    assert abs((downs[1] - downs[0]) - 2.0) < 0.1, "bars are four beats (2 s at 120 BPM)"


def test_a_steady_tone_has_no_rhythm_and_gets_no_invented_beats(tmp_path):
    out = B.detect_beats(write_wav(tmp_path / "t.wav", tone(12, 440.0, -20.0)))
    assert out == {"beats": [], "downbeats": [], "bpm": 0.0, "source": "media-index", "rhythmic": False}


def test_an_indexed_file_is_not_analysed_again(tmp_path, shelf, monkeypatch):
    path = write_wav(tmp_path / "c.wav", clicks(12, 100)[0])
    from classes.media_fingerprint import fingerprint
    sha = fingerprint(path)["sha256"]
    shelf.write_json(sha, "audio.json", {"tempo": {"bpm": 100.0, "beats": [0.5, 1.1, 1.7, 2.3, 2.9]}, "music": {"downbeats": [0.5, 2.9]}})
    shelf.set_layer(sha, "audio", version=2, status="ready")
    from classes.media_index import audio as index_audio
    monkeypatch.setattr(index_audio, "analyze_audio", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must read the shelf")))
    out = B.detect_beats(path)
    assert [b["timeSec"] for b in out["beats"]] == [0.5, 1.1, 1.7, 2.3, 2.9] and [d["timeSec"] for d in out["downbeats"]] == [0.5, 2.9] and out["bpm"] == 100.0


def test_an_older_saved_analysis_without_bars_falls_back_to_every_fourth_beat(tmp_path, shelf):
    path = write_wav(tmp_path / "c.wav", clicks(12, 100)[0])
    from classes.media_fingerprint import fingerprint
    sha = fingerprint(path)["sha256"]
    shelf.write_json(sha, "audio.json", {"tempo": {"bpm": 100.0, "beats": [round(0.5 + 0.6 * i, 2) for i in range(10)]}})
    shelf.set_layer(sha, "audio", version=1, status="ready")
    assert [d["timeSec"] for d in B.detect_beats(path)["downbeats"]] == [0.5, 2.9, 5.3]


def temp_wav_of(path, tmp_path):
    """What extract_mono_16k_wav returns: a temporary copy that the engine deletes when done (never the source file)."""
    import shutil
    def extract(p):
        copy = tmp_path / "extracted.wav"
        shutil.copy(path, copy)
        return str(copy), ""
    return extract


def test_when_the_index_cannot_read_the_file_the_older_engine_still_answers(tmp_path, monkeypatch):
    path = write_wav(tmp_path / "c.wav", clicks(6, 120)[0])
    from classes.media_index import audio as index_audio
    monkeypatch.setattr(index_audio, "analyze_audio", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("decoder exploded")))
    monkeypatch.setattr(B, "extract_mono_16k_wav", temp_wav_of(path, tmp_path))
    out = B.detect_beats(path)
    assert "source" not in out and out["bpm"] > 0 and out["beats"], "the energy engine ran"


def test_use_index_false_and_a_custom_engine_both_skip_the_index(tmp_path, monkeypatch):
    path = write_wav(tmp_path / "c.wav", clicks(6, 120)[0])
    monkeypatch.setattr(B, "extract_mono_16k_wav", temp_wav_of(path, tmp_path))
    assert "source" not in B.detect_beats(path, use_index=False)
    assert Path(path).exists(), "the source file is never touched"

    class Fake:
        def detect(self, wav_path, *, token):
            return {"beats": [{"timeSec": 1.0, "strength": 1.0}], "downbeats": [], "bpm": 77.0}

    B.set_beat_factory(Fake)
    assert B.detect_beats(path)["bpm"] == 77.0, "an installed engine is honoured"
