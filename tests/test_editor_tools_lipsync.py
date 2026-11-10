"""lip_sync_clip_tool and face_swap_clip_tool: consent, engine choice, one in-place replacement, one undo step."""

import json
import os

import pytest

from ai_generation_fakes import install
from classes import dubbing, tool_handlers
from classes.editor_tools import REGISTRY, coverage
from classes.editor_tools import ai_generation_hyperframes as hfx
from classes.editor_tools import ai_generation_lipsync as lipsync

LIP = "lip_sync_clip_tool"
FACE = "face_swap_clip_tool"
L1 = 1000000
PICKS = {"edtalk": {"backend": "cpu", "providers": ["CPUExecutionProvider"], "ms": 1.0, "cosine": 1.0}}
SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "settings", "_default.settings")


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


def _clip(editor, **video_overrides):
    video = editor.add_file("video", duration=6.0, **video_overrides)
    return editor.add_clip(video, position=0.0, layer=L1)


@pytest.fixture
def studio(editor, monkeypatch, tmp_path):
    from ai_generation_fakes import FakePoint

    install(editor, monkeypatch, tmp_path)           # timeline, Project Files and the TTS backend fakes
    monkeypatch.setattr(tool_handlers, "QPointF", FakePoint)     # the baked clip is placed through it
    state = {"lip": [], "swap": [], "features": []}
    monkeypatch.setattr(hfx, "_ask_install_consent", lambda steps, **kw: True)
    monkeypatch.setattr(lipsync, "ask_person_consent", lambda text: True)
    monkeypatch.setattr(dubbing, "ensure_local",
                        lambda features, consent, progress=None: state["features"].append(set(features))
                        or {"probe": PICKS})

    def finished(state_key):
        def run(**kw):
            state[state_key].append(kw)
            with open(kw["out"], "wb") as fh:
                fh.write(b"lip-synced")
            return {"out": kw["out"], "seconds": kw["end"] - kw["start"], "chunks": 1,
                    "cloud_chunks": 1, "local_chunks": 0}
        return run

    monkeypatch.setattr(dubbing, "lip_sync", finished("lip"))
    monkeypatch.setattr(dubbing, "face_swap", finished("swap"))

    def imported(video_path, **kw):
        from classes.query import File
        return File.get(id=editor.add_file("video", path=video_path)), None

    monkeypatch.setattr(tool_handlers, "_import_generated_video", imported)
    return state


def test_both_tools_are_background_safe_and_cover_their_capabilities():
    assert REGISTRY[LIP].background_safe and REGISTRY[LIP].covers == ("ai.lip_sync",)
    assert REGISTRY[FACE].background_safe and REGISTRY[FACE].covers == ("ai.face_swap",)
    assert "ai.voice_clone" in REGISTRY["generate_tts_and_add_to_timeline_tool"].covers
    listed = {c[0] for c in coverage.CAPABILITIES}
    assert {"ai.lip_sync", "ai.face_swap", "ai.voice_clone"} <= listed


def test_lip_sync_without_consent_is_refused_before_any_work(editor, studio):
    clip = _clip(editor)
    out = editor.call(LIP, clip_id=clip, text="Hello there.")
    assert out.startswith("Error") and "consent" in out
    assert studio["lip"] == [] and studio["features"] == [] and editor.undo_steps_since_mark() == 0


def test_face_swap_without_consent_is_refused_before_any_work(editor, studio):
    clip = _clip(editor)
    out = editor.call(FACE, clip_id=clip, face_image_file_id=editor.add_file("image"))
    assert out.startswith("Error") and "consent" in out
    assert studio["swap"] == [] and studio["features"] == [] and editor.undo_steps_since_mark() == 0


def test_lip_sync_needs_exactly_one_of_the_words_or_a_recording(editor, studio):
    clip = _clip(editor)
    assert editor.call(LIP, clip_id=clip, consent_confirmed=True).startswith("Error")
    both = editor.call(LIP, clip_id=clip, text="Hi.", audio_file_id=editor.add_file("audio"), consent_confirmed=True)
    assert both.startswith("Error")
    assert studio["lip"] == [] and editor.undo_steps_since_mark() == 0


def test_lip_sync_replaces_the_clip_in_place_as_one_undo_step(editor, studio):
    clip = _clip(editor)
    r = _receipt(editor.call(LIP, clip_id=clip, text="Hello there.", consent_confirmed=True))
    assert editor.clip(clip) is None and editor.clip(r["timeline_clip_id"]) is not None
    call = studio["lip"][0]
    assert call["engine"] == "cloud" and call["allow_local_fallback"] is True and call["text"] == "Hello there."
    assert call["start"] == 0.0 and call["end"] == pytest.approx(6.0)
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(clip) is not None and editor.clip(r["timeline_clip_id"]) is None


def test_a_recording_is_lip_synced_on_this_computer_with_only_the_face_models(editor, studio):
    clip = _clip(editor)
    _receipt(editor.call(LIP, clip_id=clip, audio_file_id=editor.add_file("audio"), consent_confirmed=True))
    call = studio["lip"][0]
    assert call["engine"] == "local" and "sample_audio" in call["audio"]
    assert studio["features"] == [{"face"}]


def test_cloud_cannot_take_a_recording(editor, studio):
    clip = _clip(editor)
    out = editor.call(LIP, clip_id=clip, audio_file_id=editor.add_file("audio"), engine="cloud", consent_confirmed=True)
    assert out.startswith("Error") and "Grok" in out and studio["lip"] == []


def test_a_lip_sync_failure_is_an_error_and_changes_nothing(editor, studio, monkeypatch):
    def out_of_credits(**kw):
        raise dubbing.DubbingError("Out of credits. Run this on this computer instead.")

    monkeypatch.setattr(dubbing, "lip_sync", out_of_credits)
    clip = _clip(editor)
    out = editor.call(LIP, clip_id=clip, text="Hi.", consent_confirmed=True)
    assert out.startswith("Error: Out of credits") and editor.clip(clip) is not None
    assert editor.undo_steps_since_mark() == 0


def test_face_swap_on_a_clip_with_speech_runs_on_this_computer(editor, studio):
    clip = _clip(editor, ai_metadata={"has_speech": True})
    r = _receipt(editor.call(FACE, clip_id=clip, face_image_file_id=editor.add_file("image"), consent_confirmed=True))
    assert studio["swap"][0]["engine"] == "local" and studio["swap"][0]["face_image"]
    assert editor.clip(clip) is None and editor.clip(r["timeline_clip_id"]) is not None
    assert editor.undo_steps_since_mark() == 1


def test_face_swap_on_a_silent_clip_uses_the_cloud_under_auto(editor, studio):
    clip = _clip(editor, ai_metadata={"has_speech": False})
    _receipt(editor.call(FACE, clip_id=clip, face_image_file_id=editor.add_file("image"), consent_confirmed=True))
    assert studio["swap"][0]["engine"] == "cloud" and studio["swap"][0]["allow_local_fallback"] is True


def test_the_dubbing_settings_are_dropdowns_with_auto_or_local_defaults():
    with open(SETTINGS, encoding="utf-8") as fh:
        rows = {r["setting"]: r for r in json.load(fh) if "setting" in r}
    assert [v["value"] for v in rows["lip-sync-engine"]["values"]] == ["auto", "cloud", "local"]
    assert [v["value"] for v in rows["face-swap-engine"]["values"]] == ["auto", "cloud", "local"]
    assert [v["value"] for v in rows["tts-engine"]["values"]] == ["local", "cloud"]
    assert rows["lip-sync-engine"]["value"] == rows["face-swap-engine"]["value"] == "auto"
    assert rows["tts-engine"]["value"] == "local"
    assert rows["dubbing-backend-picks"]["type"] == "hidden" and rows["dubbing-backend-picks"]["value"] == ""
    assert all(rows[k]["category"] == "AI" for k in ("lip-sync-engine", "face-swap-engine", "tts-engine"))


def test_face_swap_on_a_clip_not_yet_checked_for_speech_stays_on_this_computer(editor, studio):
    clip = _clip(editor)
    _receipt(editor.call(FACE, clip_id=clip, face_image_file_id=editor.add_file("image"), consent_confirmed=True))
    assert studio["swap"][0]["engine"] == "local"


def test_the_person_at_the_computer_must_confirm_even_when_the_agent_says_yes(editor, studio, monkeypatch):
    asked = []
    monkeypatch.setattr(lipsync, "ask_person_consent", lambda text: asked.append(text) or False)
    clip = _clip(editor)
    out = editor.call(LIP, clip_id=clip, text="Hello there.", consent_confirmed=True)
    assert out.startswith("Error") and "declined" in out and asked
    assert studio["lip"] == [] and editor.undo_steps_since_mark() == 0


def test_oversized_clips_and_scripts_are_refused_before_any_work(editor, studio):
    seconds = dubbing.MAX_JOB_S + 60
    video = editor.add_file("video", duration=seconds)
    long_clip = editor.add_clip(video, position=0.0, layer=L1, end=seconds)
    too_long = editor.call(LIP, clip_id=long_clip, text="Hello.", consent_confirmed=True)
    wordy = editor.call(LIP, clip_id=_clip(editor), text="word " * 1000, consent_confirmed=True)
    assert too_long.startswith("Error") and "too long" in too_long
    assert wordy.startswith("Error") and "too long" in wordy and studio["lip"] == []
