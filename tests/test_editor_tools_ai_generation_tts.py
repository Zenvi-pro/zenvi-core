"""generate_tts_and_add_to_timeline_tool: narration on a real track with its real duration, one undo step."""

import json
import os

import pytest

from ai_generation_fakes import FakeBackend, install
from classes import tool_handlers
from classes.editor_tools import REGISTRY
from classes.editor_tools.ai_generation_tts import estimated_cues, pick_narration_track

TOOL = "generate_tts_and_add_to_timeline_tool"
L1, L2, L3, L4, L5 = 1000000, 2000000, 3000000, 4000000, 5000000


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def tts(editor, monkeypatch, tmp_path):
    editor.settings["tts-engine"] = "cloud"              # the backend voice; the default is this computer
    return install(editor, monkeypatch, tmp_path, duration=3.2)


def test_narration_lands_on_the_lowest_free_track_above_the_picture(editor, tts):
    video = editor.add_file("video")
    editor.add_clip(video, position=0.0, layer=L1)

    out = editor.call(TOOL, text="Welcome to Lisbon. The city of seven hills!", position_seconds=2.0)
    r = _receipt(out)

    assert r["layer"] == L2 and r["track"] == 2 and r["created_track"] is False
    assert r["position"] == 2.0 and r["duration"] == pytest.approx(3.2, abs=1 / 30)
    clip = editor.clip(r["timeline_clip_id"])
    assert clip["layer"] == L2 and clip["end"] - clip["start"] == pytest.approx(3.2, abs=1 / 30)
    # probed through files_model.add_files, never a raw unprobed insert
    call = tts.files_model.calls[-1]
    assert call["quiet"] is True and call["skip_indexing"] is True and call["prevent_recent_folder"] is True
    f = editor.file(r["file_id"])
    assert f["name"].startswith("Narration - Welcome to Lisbon.")
    ai = f["ai_metadata"]
    assert ai["source"] == "tts" and ai["has_speech"] is True and ai["transcript"].startswith("Welcome")
    assert [c["text"] for c in ai["transcript_cues"]] == ["Welcome to Lisbon.", "The city of seven hills!"]
    assert tts.backend.tts_calls == [{"text": "Welcome to Lisbon. The city of seven hills!", "voice": "alloy",
                                      "model": "tts-1", "speed": 1.0}]
    assert os.path.isfile(r["path"])
    assert os.path.basename(r["path"]).startswith("narration_welcome_to_lisbon_the_")
    assert tts.timeline.calls[-1]["call_manual_move"] is False
    # the clip carries the narration's name, and Project Files is told to refresh the renamed row
    assert editor.clip(r["timeline_clip_id"])["title"] == f["name"]
    editor.window.FileUpdated.emit.assert_any_call(r["file_id"])


def test_one_undo_step_removes_clip_and_file_and_redo_restores(editor, tts):
    r = _receipt(editor.call(TOOL, text="Hello there.", position_seconds=0))
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(r["timeline_clip_id"]) is None and editor.file(r["file_id"]) is None
    editor.redo()
    assert editor.clip(r["timeline_clip_id"]) is not None and editor.file(r["file_id"]) is not None


def test_every_track_busy_creates_a_narration_track_above_the_lowest_in_the_same_step(editor, tts):
    video = editor.add_file("video")
    for layer in (L1, L2, L3, L4, L5):
        editor.add_clip(video, position=0.0, layer=layer)
    layers_before = [t["number"] for t in editor.get("layers")]

    r = _receipt(editor.call(TOOL, text="Chapter one.", position_seconds=1.0))

    assert r["created_track"] is True and r["track"] == 2 and r["track_name"].startswith("Narration")
    assert r["layer"] not in layers_before and L1 < r["layer"] < L2
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert [t["number"] for t in editor.get("layers")] == layers_before
    assert editor.clip(r["timeline_clip_id"]) is None


def test_second_narration_reuses_the_narration_track_when_it_is_free(editor, tts):
    video = editor.add_file("video")
    editor.add_clip(video, position=0.0, layer=L1)
    first = _receipt(editor.call(TOOL, text="One.", position_seconds=0.0))
    # make track 3 non-empty so "lowest empty" would not pick L2 by accident
    second = _receipt(editor.call(TOOL, text="Two.", position_seconds=10.0))
    assert first["layer"] == second["layer"] == L2
    assert second["track_choice"] == "the narration track"


def test_a_busy_narration_track_is_skipped(editor, tts):
    video = editor.add_file("video")
    editor.add_clip(video, position=0.0, layer=L1)
    first = _receipt(editor.call(TOOL, text="One.", position_seconds=0.0))
    second = _receipt(editor.call(TOOL, text="Two.", position_seconds=1.0))  # overlaps the first
    assert first["layer"] == L2 and second["layer"] == L3


def test_explicit_ui_track_number_and_overlap_warning(editor, tts):
    music = editor.add_file("audio")
    editor.add_clip(music, position=0.0, layer=L3)
    out = editor.call(TOOL, text="Over the music.", track="3", position_seconds=1.0)
    r = _receipt(out)
    assert r["layer"] == L3 and r["track"] == 3 and len(r["overlaps"]) == 1
    assert "Warning: it overlaps 1 clip" in out.split("\n", 1)[0]


def test_track_zero_and_legacy_position_mean_the_defaults(editor, tts):
    r = _receipt(editor.call(TOOL, text="Legacy call.", track="0", position=4.0))
    assert r["position"] == 4.0 and r["layer"] == L2


def test_position_defaults_to_the_playhead(editor, tts):
    editor.window.preview_thread.player.Position.return_value = 91  # frame 91 at 30 fps = 3.0 s
    r = _receipt(editor.call(TOOL, text="At the playhead."))
    assert r["position"] == pytest.approx(3.0)


@pytest.mark.parametrize("args, message", [
    ({"text": "   "}, "text is empty"),
    ({"text": "x" * 4097}, "limit is 4096"),
    ({"text": "Hi.", "voice": "Deep Voice!"}, "not a voice name"),
    ({"text": "Hi.", "model": "tts 1"}, "not a model name"),
    ({"text": "Hi.", "track": "Nope"}, "Error"),
    ({"text": "Hi.", "position": 1.0, "position_seconds": 2.0}, "disagree"),
    ({"text": "Hi.", "speed": 9}, "must be <= 4.0"),
    ({"text": "Hi.", "position_seconds": -1}, "must be >= 0"),
])
def test_bad_arguments_are_refused_before_any_backend_call(editor, tts, args, message):
    out = editor.call(TOOL, **args)
    assert out.startswith("Error") and message in out, out
    assert tts.backend.tts_calls == []
    assert editor.undo_steps_since_mark() == 0 and editor.clips() == []


def test_locked_track_is_refused_before_spending_credits(editor, tts):
    editor.lock_track(L2)
    out = editor.call(TOOL, text="Hi.", track="2")
    assert out.startswith("Error") and "locked" in out
    assert tts.backend.tts_calls == [] and editor.undo_steps_since_mark() == 0


def test_locked_tracks_are_skipped_by_the_default_rule(editor, tts):
    editor.lock_track(L2)
    r = _receipt(editor.call(TOOL, text="Hi.", position_seconds=0))
    assert r["layer"] == L3


def test_backend_failure_is_an_error_and_changes_nothing(editor, monkeypatch, tmp_path):
    editor.settings["tts-engine"] = "cloud"
    fakes = install(editor, monkeypatch, tmp_path, backend=FakeBackend(error="402 Payment Required"))
    out = editor.call(TOOL, text="Hi.")
    assert out.startswith("Error: text-to-speech failed: 402 Payment Required")
    assert editor.undo_steps_since_mark() == 0 and editor.get("files") == []
    assert fakes.files_model.calls == []


def test_empty_audio_is_an_error(editor, monkeypatch, tmp_path):
    editor.settings["tts-engine"] = "cloud"
    install(editor, monkeypatch, tmp_path, backend=FakeBackend(audio=b""))
    out = editor.call(TOOL, text="Hi.")
    assert out.startswith("Error: text-to-speech returned empty audio")


def test_unreadable_audio_is_an_error_and_the_mp3_is_removed(editor, tts, tmp_path):
    tts.files_model.fail_paths.add(str(tmp_path / "narration_hi_001.mp3"))
    out = editor.call(TOOL, text="Hi.")
    assert out.startswith("Error: the narration audio could not be imported")
    assert not (tmp_path / "narration_hi_001.mp3").exists()
    assert editor.undo_steps_since_mark() == 0


def test_the_legacy_handler_is_gone_and_the_registry_tool_is_background_safe():
    assert not hasattr(tool_handlers, "add_tts_audio_to_timeline")
    assert tool_handlers.AGENT_TOOL_HANDLERS[TOOL] is REGISTRY[TOOL].func
    assert TOOL in tool_handlers.BACKGROUND_SAFE_TOOLS


# --- the track rule and cue estimate on their own -----------------------------

def _layers(*numbers, locked=()):
    return [{"id": f"L{i}", "number": n, "label": "", "lock": n in locked} for i, n in enumerate(numbers, 1)]


def test_pick_track_needs_a_track_above_the_lowest():
    assert pick_narration_track(_layers(L1), [], 0, 1)[0] is None
    assert pick_narration_track(_layers(L1, L2), [], 0, 1)[0] == L2
    assert pick_narration_track(_layers(L1, L2, locked=(L2,)), [], 0, 1)[0] is None


def test_pick_track_prefers_a_free_narration_track_over_an_empty_one():
    clips = [{"layer": L3, "file_id": "tts1", "position": 0.0, "start": 0.0, "end": 2.0}]
    assert pick_narration_track(_layers(L1, L2, L3), clips, 5, 6, ["tts1"]) == (L3, "the narration track")
    # busy at 1..2 -> the empty L2
    assert pick_narration_track(_layers(L1, L2, L3), clips, 1, 2, ["tts1"])[0] == L2
    # a track with music is never a narration track
    music = [{"layer": L2, "file_id": "song", "position": 0.0, "start": 0.0, "end": 2.0}]
    assert pick_narration_track(_layers(L1, L2), music, 5, 6, ["tts1"])[0] is None


def test_estimated_cues_cover_the_whole_duration():
    cues = estimated_cues("Short. A much longer second sentence here!", 6.0)
    assert cues[0]["start"] == 0.0 and cues[-1]["end"] == 6.0
    assert cues[0]["end"] < 3.0 < cues[1]["end"]
    assert estimated_cues("no punctuation at all", 2.0) == [{"start": 0.0, "end": 2.0, "text": "no punctuation at all"}]


# --- the backend call carries the signed-in user's token ------------------------------------------

def test_backend_requests_carry_the_users_bearer_token():
    """Regression: /generation/tts answers 401 'missing Authorization: Bearer header' without it."""
    requests = pytest.importorskip("requests")
    from classes.api_client import ZenviBackendClient

    client = ZenviBackendClient(base_url="https://api.example.invalid")
    client._auth_token = lambda: "user-jwt"
    prepared = client.session.prepare_request(
        requests.Request("POST", client.api_url + "/generation/tts", json={"text": "hi"}))
    assert prepared.headers["Authorization"] == "Bearer user-jwt"
    client._auth_token = lambda: None
    signed_out = client.session.prepare_request(requests.Request("GET", client.api_url + "/models"))
    assert "Authorization" not in signed_out.headers


# --- the local voice (Ming, on this computer) and voice cloning --------------------------------

@pytest.fixture
def local_voice(editor, monkeypatch, tmp_path):
    """The Speech setting at its default (this computer); the runtime and Ming are fakes that write a WAV."""
    from classes import dubbing

    fakes = install(editor, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(dubbing, "ensure_local", lambda features, consent, progress=None: {"probe": {"ming": {}}})

    def fake_tts(text, out, picks, instruction="", prompt_wav="", prompt_text=""):
        calls.append({"text": text, "instruction": instruction, "prompt_wav": prompt_wav, "prompt_text": prompt_text})
        with open(out, "wb") as fh:
            fh.write(b"RIFF-fake")
        return {"out": out, "seconds": 3.2}

    monkeypatch.setattr(dubbing, "tts_local", fake_tts)
    monkeypatch.setattr(dubbing, "heard", lambda path: "the reference words")
    return fakes, calls


def test_this_computer_is_the_default_speech_engine_and_spends_no_credits(editor, local_voice):
    fakes, calls = local_voice
    r = _receipt(editor.call(TOOL, text="Welcome [chuckle] to Lisbon."))
    assert fakes.backend.tts_calls == []
    assert calls[0]["text"] == "Welcome [Laughter] to Lisbon."
    assert r["voice"] == "ming" and r["model"] == "ming-3.0-tts-0.5b"
    assert r["duration"] == pytest.approx(3.2, abs=1 / 30)


def test_a_voice_reference_needs_consent_and_runs_on_this_computer(editor, local_voice):
    fakes, calls = local_voice
    ref = editor.add_file("audio", path="/media/ref.wav")
    out = editor.call(TOOL, text="Hi there.", voice_reference_file_id=ref)
    assert out.startswith("Error") and "consent" in out
    assert calls == [] and fakes.backend.tts_calls == [] and editor.undo_steps_since_mark() == 0
    r = _receipt(editor.call(TOOL, text="Hi there.", voice_reference_file_id=ref, consent_confirmed=True))
    assert calls[0]["prompt_wav"].endswith("ref.wav") and calls[0]["prompt_text"] == "the reference words"
    assert r["voice"] == "clone" and editor.file(r["file_id"])["ai_metadata"]["voice_cloned"] is True


def test_a_cloud_voice_cannot_copy_a_voice(editor, local_voice):
    fakes, calls = local_voice
    ref = editor.add_file("audio", path="/media/ref.wav")
    out = editor.call(TOOL, text="Hi.", engine="cloud", voice_reference_file_id=ref, consent_confirmed=True)
    assert out.startswith("Error") and "engine='local'" in out
    assert fakes.backend.tts_calls == [] and calls == []


def test_an_instruction_speaks_the_local_voice_freeform(editor, local_voice):
    _fakes, calls = local_voice
    _receipt(editor.call(TOOL, text="Good morning.", instruction="slow and clear"))
    assert calls[0]["instruction"] == "slow and clear"


def test_the_cloud_voice_strips_the_tags_it_cannot_read(editor, monkeypatch, tmp_path):
    fakes = install(editor, monkeypatch, tmp_path)
    editor.settings["tts-engine"] = "cloud"
    _receipt(editor.call(TOOL, text="Welcome [chuckle] to Lisbon."))
    assert fakes.backend.tts_calls[0]["text"] == "Welcome to Lisbon."


def test_cloud_out_of_credits_says_to_run_it_on_this_computer(editor, monkeypatch, tmp_path):
    from classes import api_client

    install(editor, monkeypatch, tmp_path)
    editor.settings["tts-engine"] = "cloud"

    class Broke:
        def generate_tts(self, **kwargs):
            return {"success": False, "error": "Out of credits.", "credits": True}

    monkeypatch.setattr(api_client, "get_backend_client", lambda: Broke())
    out = editor.call(TOOL, text="Hi.")
    assert out.startswith("Error") and "Out of credits" in out and "engine='local'" in out
    assert editor.undo_steps_since_mark() == 0
