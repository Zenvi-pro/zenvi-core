"""generate_music_tool (ACE-Step 1.5 / YuE2 on this computer) and the stock-music bed fallback."""

import json
import os

import pytest

from classes import music_local as ml
from classes import tool_handlers
from classes.editor_tools import REGISTRY
from classes.editor_tools import ai_generation_hyperframes as hfx


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    state = {"installed": set(), "asked": [], "generated": [], "imported": []}
    monkeypatch.setattr(ml, "status", lambda engine: {"runtime": engine in state["installed"],
                                                      "models": engine in state["installed"]})
    monkeypatch.setattr(ml, "install", lambda engine: state["installed"].add(engine))

    def fake_generate(engine, style, lyrics="", duration=30.0, seed=-1):
        state["generated"].append((engine, style, lyrics, duration))
        out = os.path.join(str(tmp_path), "%s-%d.mp3" % (engine, len(state["generated"])))
        open(out, "wb").write(b"ID3" + b"0" * 2048)
        return out

    def fake_add(local_path="", **kwargs):
        state["imported"].append(local_path)
        return "Added to project and indexed: %s (file_id=F%d)." % (local_path, len(state["imported"]))

    monkeypatch.setattr(ml, "generate", fake_generate)
    monkeypatch.setattr(tool_handlers, "add_stock_media_to_project", fake_add)
    monkeypatch.setattr(hfx, "_ask_install_consent", lambda steps, **kw: state["asked"].append(steps) or True)
    return state


def test_one_music_tool_is_registered():
    assert "generate_music_tool" in REGISTRY


def test_a_bed_installs_with_consent_generates_and_imports(editor, studio):
    out = _receipt(editor.call("generate_music_tool", prompt="calm lofi piano", duration_seconds=30))
    assert studio["asked"] and studio["installed"] == {"ace_step"}
    assert studio["generated"] == [("ace_step", "calm lofi piano", "", 30.0)]
    assert out["file_id"] == "F1" and out["engine"] == "ace_step"


def test_yue2_songs_carry_lyrics_and_its_licence(editor, studio):
    out = _receipt(editor.call("generate_music_tool", prompt="indie pop", lyrics="[Verse]\nCity lights",
                               duration_seconds=60, engine="yue2"))
    assert studio["generated"][0][0] == "yue2" and "City lights" in studio["generated"][0][2]
    assert "non-commercial" in out["licence"].lower()


def test_declined_install_generates_nothing(editor, studio, monkeypatch):
    monkeypatch.setattr(hfx, "_ask_install_consent", lambda steps, **kw: False)
    out = editor.call("generate_music_tool", prompt="lofi")
    assert out.startswith("Error") and "declined" in out
    assert studio["generated"] == []


def test_stock_music_with_no_hit_generates_a_bed_from_the_query(editor, studio):
    out = tool_handlers.import_stock_media(source="freesound", sound_id="", preview_url="", query="ocean waves")
    assert "file_id=F1" in out and "generated locally" in out.lower()
    engine, style, lyrics, duration = studio["generated"][0]
    assert engine == "ace_step" and "ocean waves" in style and lyrics == ""


def test_stock_music_download_failure_also_falls_back(editor, studio, monkeypatch):
    monkeypatch.setattr(tool_handlers, "download_freesound_music_tool",
                        lambda **kw: "Error: Freesound download failed: 429 Too Many Requests")
    out = tool_handlers.import_stock_media(source="freesound", sound_id="7", preview_url="https://x/a.mp3",
                                           query="rain")
    assert "file_id=F1" in out


def test_without_a_query_a_missing_hit_is_still_an_error(editor, studio):
    out = tool_handlers.import_stock_media(source="freesound", sound_id="", preview_url="")
    assert out.startswith("Error") and studio["generated"] == []
