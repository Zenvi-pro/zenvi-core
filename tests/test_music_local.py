"""Local music generation: ACE-Step 1.5 (acestep.cpp) and YuE2 (yue2.cpp) on this computer.

Beds (instrumental) render DiT-only on ACE-Step; songs with lyrics run its LM
first. Every run tiles the VAE small (the iGPU otherwise runs out of pinned
memory and a 30 s decode takes minutes). Runtimes and models are pinned
downloads; a failed run surfaces the tool's own output.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes import music_local as ml  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "home", lambda: str(tmp_path))
    monkeypatch.setattr(ml, "status", lambda engine: {"runtime": True, "models": True})
    return tmp_path


def _fake_run(calls, outputs=()):
    """Record argv + the request JSON each binary read; write the files it would write."""
    def run(argv, cwd, timeout):
        req_path = argv[argv.index("--request") + 1]
        with open(req_path, encoding="utf-8") as fh:
            calls.append((os.path.basename(argv[0]), argv, json.load(fh)))
        name = os.path.basename(argv[0])
        if name.startswith("ace-lm"):
            with open(req_path[:-5] + "0.json", "w", encoding="utf-8") as fh:
                json.dump({"audio_codes": "1,2,3"}, fh)
        elif name.startswith("ace-synth"):
            with open(req_path[:-5] + "0.mp3", "wb") as fh:
                fh.write(b"ID3")
        else:
            with open(argv[argv.index("--out") + 1], "wb") as fh:
                fh.write(b"ID3")
        return 0, "ok"
    return run


def test_ace_bed_is_dit_only_with_small_vae_tiles(home):
    calls = []
    with patch.object(ml, "_run", side_effect=_fake_run(calls)):
        out = ml.generate("ace_step", "calm lofi piano", duration=30)
    assert [c[0].split(".")[0] for c in calls] == ["ace-synth"]
    _, argv, req = calls[0]
    assert req["lyrics"] == "[Instrumental]"
    assert "lm_model" not in req
    assert req["duration"] == 30
    assert argv[argv.index("--vae-chunk") + 1] == "128"
    assert out.endswith(".mp3") and os.path.getsize(out) > 0


def test_ace_song_runs_the_lm_then_renders_its_codes(home):
    calls = []
    with patch.object(ml, "_run", side_effect=_fake_run(calls)):
        ml.generate("ace_step", "indie pop, female vocal", lyrics="[Verse]\nCity lights", duration=60)
    assert [c[0].split(".")[0] for c in calls] == ["ace-lm", "ace-synth"]
    assert calls[0][2]["lm_model"] == ml.ACE_LM
    assert calls[1][2] == {"audio_codes": "1,2,3"}  # synth renders the LM's own output


def test_yue2_uses_the_memory_capped_settings(home):
    calls = []
    with patch.object(ml, "_run", side_effect=_fake_run(calls)):
        ml.generate("yue2", "indie pop", lyrics="[Verse]\nCity lights", duration=60)
    name, argv, req = calls[0]
    assert name.startswith("yue-synth")
    assert argv[argv.index("--max-seq") + 1] == "8192"
    assert argv[argv.index("--vae-core") + 1] == "128"
    assert req["output_format"] == "mp3" and req["duration"] == 60
    assert os.path.basename(argv[argv.index("--model") + 1]) == ml.YUE_MODEL


def test_a_failed_run_reports_the_tools_output(home):
    with patch.object(ml, "_run", return_value=(1, "ggml_vulkan: device lost")):
        with pytest.raises(ml.MusicError, match="device lost"):
            ml.generate("ace_step", "lofi")


def test_unknown_engine_and_empty_prompt_are_refused(home):
    with pytest.raises(ml.MusicError):
        ml.generate("musicgen", "lofi")
    with pytest.raises(ml.MusicError):
        ml.generate("ace_step", "  ")


def test_install_downloads_pinned_models_and_the_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "home", lambda: str(tmp_path))
    got = []
    monkeypatch.setattr(ml, "_download_pinned", lambda folder, files, what: got.extend(files))
    monkeypatch.setattr(ml, "_unpack_runtime", lambda engine: None)
    monkeypatch.setattr(ml, "status", lambda engine: {"runtime": False, "models": False})
    ml.install("ace_step")
    names = {name for _, name, _ in got}
    assert {ml.ACE_DIT, ml.ACE_LM, ml.ACE_VAE, ml.ACE_TEXT} <= names
    for url, name, digest in got:
        assert len(digest) == 64
        if name.endswith(".gguf"):
            assert "/resolve/" + ml.ACE_REVISION + "/" in url  # a commit, never a moving branch


def test_install_plan_lists_sizes_and_yue2_licence(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "status", lambda engine: {"runtime": False, "models": False})
    labels = " | ".join(s["label"] for s in ml.install_plan("yue2"))
    assert "GB" in labels
    assert "non-commercial" in labels.lower()
