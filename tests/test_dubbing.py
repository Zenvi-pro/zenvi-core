"""classes.dubbing: tag cleanup, engine choice, chunk planning and the cloud/local orchestration (fakes only)."""

import os
import types

import pytest

from classes import dubbing
from classes.dubbing import DubbingError

PICKS = {"edtalk": {"backend": "cpu", "providers": ["CPUExecutionProvider"], "ms": 1.0, "cosine": 1.0}}
SRC = "/media/source.mp4"
TWO = [{"start": 0.0, "end": 6.0, "text": "Sentence one is here."},
       {"start": 6.4, "end": 12.4, "text": "Sentence two follows."}]


def _cue(start, end, text):
    return {"start": start, "end": end, "text": text}


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


# --- tags -------------------------------------------------------------------------------

def test_the_supported_tags_are_the_fourteen_exact_case_tags():
    assert len(dubbing.SUPPORTED_TAGS) == 14
    assert "[Surprise-oh]" in dubbing.SUPPORTED_TAGS and "[Laughter]" in dubbing.SUPPORTED_TAGS


@pytest.mark.parametrize("text, expected", [
    ("Hi [chuckle] there.", "Hi [Laughter] there."),
    ("Hi [LAUGHS] there.", "Hi [Laughter] there."),
    ("Hi [sighs] there.", "Hi [Sigh] there."),
    ("Hi [surprise-AH] there.", "Hi [Surprise-ah] there."),
    ("Hi [Confirmation-en] there.", "Hi [Confirmation-en] there."),
    ("Hi [Foo] there.", "Hi there."),
])
def test_known_tags_are_case_fixed_and_aliased_and_unknown_ones_are_dropped(text, expected):
    assert dubbing.sanitize_tags(text) == expected


def test_a_tag_at_the_start_of_a_sentence_moves_to_after_its_first_word():
    assert dubbing.sanitize_tags("[chuckle] Hello there.") == "Hello [Laughter] there."
    assert dubbing.sanitize_tags("Hello there. [Sigh] Well, okay.") == "Hello there. Well, [Sigh] okay."
    assert dubbing.sanitize_tags("Hi there [Sigh] friend.") == "Hi there [Sigh] friend."


def test_strip_tags_removes_every_bracket_tag_for_the_cloud_voice():
    assert dubbing.strip_tags("Hi [Laughter] there [Sigh].") == "Hi there."


# --- engine choice ----------------------------------------------------------------------

@pytest.mark.parametrize("feature, setting, kwargs, expected", [
    ("lip_sync", "auto", {}, "cloud"),
    ("lip_sync", "auto", {"keep_own_audio": True}, "local"),
    ("lip_sync", "auto", {"out_of_credits": True}, "local"),
    ("lip_sync", "cloud", {"out_of_credits": True}, "cloud"),
    ("lip_sync", "local", {}, "local"),
    ("face_swap", "auto", {}, "cloud"),
    ("face_swap", "auto", {"has_speech": True}, "local"),
    ("face_swap", "auto", {"out_of_credits": True}, "local"),
    ("face_swap", "cloud", {"has_speech": True}, "cloud"),
    ("face_swap", "local", {}, "local"),
    ("tts", "local", {}, "local"),
    ("tts", "cloud", {}, "cloud"),
])
def test_engine_for_follows_the_rule_table(feature, setting, kwargs, expected):
    assert dubbing.engine_for(feature, setting, **kwargs) == expected


# --- chunk planning ---------------------------------------------------------------------

def test_chunks_break_at_sentence_ends_and_pauses_near_the_target_length():
    cues = [_cue(0, 3, "One two three."), _cue(3.5, 6.5, "Four five six."),
            _cue(7, 10, "Seven eight nine."), _cue(10.5, 13.5, "Ten eleven twelve.")]
    chunks = dubbing.plan_chunks(cues, cut_times=[])
    assert [(c["start"], c["end"]) for c in chunks] == [(0, 7.0), (7.0, 13.5)]
    assert [c["text"] for c in chunks] == ["One two three. Four five six.", "Seven eight nine. Ten eleven twelve."]
    assert [c["duration"] for c in chunks] == [7, 7]


def test_no_chunk_window_crosses_a_shot_cut():
    chunks = dubbing.plan_chunks([_cue(0, 4, "A b c d."), _cue(4.5, 9, "E f g h.")], cut_times=[6.0])
    assert [(c["start"], c["end"]) for c in chunks] == [(0, 6.0), (6.0, 9)]
    assert all(not (c["start"] < 6.0 < c["end"]) for c in chunks)


def test_durations_are_whole_seconds_within_one_to_fifteen():
    assert [c["duration"] for c in dubbing.plan_chunks([_cue(0, 0.4, "Hi.")], [])] == [1]
    assert [c["duration"] for c in dubbing.plan_chunks([_cue(0, 6.2, "Six seconds of words here.")], [])] == [7]


def test_a_long_sentence_splits_at_the_pause_nearest_the_target():
    # 30 half-second words, 0.1 s apart, one 0.5 s pause after the tenth word, no full stop.
    words, t = [], 0.0
    for i in range(30):
        words.append(_cue(t, t + 0.5, "w%d" % i))
        t += 0.5 + (0.5 if i == 9 else 0.1)
    chunks = dubbing.plan_chunks(words, cut_times=[])
    assert len(chunks) == 2
    assert chunks[0]["end"] == pytest.approx(words[10]["start"])     # the split falls in the pause
    assert all(1 <= c["duration"] <= 15 for c in chunks)


def test_a_sentence_with_no_pause_splits_at_the_word_nearest_eight_seconds():
    words = [_cue(0.5 * i, 0.5 * (i + 1), "w%d" % i) for i in range(40)]
    chunks = dubbing.plan_chunks(words, cut_times=[])
    assert len(chunks) == 2 and chunks[0]["end"] == 8.0


def test_a_clip_with_no_words_is_cut_into_pieces_of_at_most_fifteen_seconds():
    assert [c["duration"] for c in dubbing.plan_chunks([_cue(0, 20, "")], cut_times=[])] == [10, 10]


# --- ffmpeg stitch and the validation helpers ------------------------------------------

def test_stitch_takes_one_video_stream_per_clip_resamples_and_crossfades_the_audio():
    args = dubbing.stitch_args(["a.mp4", "b.mp4"], "out.mp4", 24, size=(1280, 720), seconds=[7, 8])
    graph = args[args.index("-filter_complex") + 1]
    assert "[0:v:0]fps=24" in graph and "[1:v:0]fps=24" in graph
    assert "concat=n=2" in graph and "acrossfade=d=0.15" in graph
    assert args.count("-t") == 2 and args[-1] == "out.mp4"


def test_stitch_with_audio_replaces_the_clip_audio_and_stops_at_the_shorter_stream():
    args = dubbing.stitch_args(["a.mp4"], "out.mp4", 24, audio="voice.wav")
    graph = args[args.index("-filter_complex") + 1]
    assert "-shortest" in args and "1:a:0?" in args and "acrossfade" not in graph


def test_word_error_rate_ignores_case_and_punctuation():
    assert dubbing.word_error_rate("Hello there, friend.", "hello there friend") == 0
    assert dubbing.word_error_rate("one two three four", "one two") == pytest.approx(0.5)
    assert dubbing.word_error_rate("", "anything") == 0


def test_keyframes_sit_on_the_third_second_grid_inside_the_window():
    assert dubbing.keyframe_times(6) == [2.0, 4.0]
    assert dubbing.keyframe_times(1) == pytest.approx([1 / 3, 2 / 3])


def test_picks_are_the_cached_probe_or_none_before_the_probe_has_run():
    assert dubbing.picks_of({"probe": PICKS}) == PICKS
    assert dubbing.picks_of({"probe": None}) is None


# --- fakes for the runtime, the backend and ffmpeg -------------------------------------

class FakeRuntime:
    DubbingRuntimeError = type("DubbingRuntimeError", (Exception,), {})

    def __init__(self):
        self.status_value = {"env": True, "pack": "cpu", "face_models": True, "ming": True, "probe": PICKS}
        self.installed = []
        self.worker_calls = []

    def status(self):
        return dict(self.status_value)

    def install(self, features, progress=None, cancel=None):
        self.installed.append(set(features))
        self.status_value["env"] = True
        if "face" in features:
            self.status_value["face_models"] = True
        if "ming" in features:
            self.status_value["ming"] = True
        return self.status()

    def run_worker(self, command, args, progress=None, cancel=None, timeout=7200):
        self.worker_calls.append((command, dict(args)))
        if command == "swap-frames":
            paths = []
            for src in args["images"]:
                dst = os.path.join(args["out_dir"], "swapped-" + os.path.basename(src))
                _write(dst, b"swapped")
                paths.append(dst)
            return {"images": paths}
        _write(args["out"], command.encode())
        return {"out": args["out"], "seconds": 2.0, "frames": 10, "frames_with_face": 10}


class FakeClient:
    """Backend stand-in: replies pop from the list (None = a good video that says the dialogue)."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def upload_media_file(self, local_path, file_id="", filename=None, session=None):
        return {"success": True}

    def generate_video(self, prompt, duration_seconds=5, **kwargs):
        self.calls.append(dict(kwargs, prompt=prompt, duration_seconds=duration_seconds))
        reply = self.replies.pop(0) if self.replies else None
        if reply is not None:
            return reply
        return {"video_url": "fake://" + kwargs.get("dialogue_text", "")}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    rt = FakeRuntime()
    ffmpeg = []

    def fake_ffmpeg(args):                  # every ffmpeg call writes its last argument
        ffmpeg.append(list(args))
        _write(args[-1], b"media")

    def fake_download(url, path):           # the "video" is the URL text
        _write(path, url.encode())

    def fake_heard(path):                   # what the recogniser hears in a downloaded video
        with open(path, "rb") as fh:
            text = fh.read().decode()
        return text[len("fake://"):] if text.startswith("fake://") else text

    monkeypatch.setattr(dubbing, "_runtime", lambda: rt)
    monkeypatch.setattr(dubbing, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(dubbing, "_download", fake_download)
    monkeypatch.setattr(dubbing, "heard", fake_heard)
    monkeypatch.setattr(dubbing, "shot_cuts", lambda source, start, seconds: [])
    return types.SimpleNamespace(rt=rt, ffmpeg=ffmpeg, tmp=tmp_path)


def _lip(rig, client, cues=TWO, **overrides):
    args = dict(source=SRC, start=0.0, end=cues[-1]["end"], size=(1280, 720), fps=24,
                out=str(rig.tmp / "out.mp4"), text=" ".join(c["text"] for c in cues), cues=cues,
                voice="leo", engine="cloud", allow_local_fallback=True, picks=PICKS, client=client)
    args.update(overrides)
    return dubbing.lip_sync(**args)


def _commands(rig):
    return [command for command, _ in rig.rt.worker_calls]


# --- cloud lip sync -----------------------------------------------------------------------

def test_dialogue_chunks_send_the_script_the_voice_and_pinned_source_frames(rig):
    client = FakeClient()
    _lip(rig, client)
    first = client.calls[0]
    assert first["mode"] == "dialogue" and first["generate_audio"] is True and first["voice_id"] == "leo"
    assert first["dialogue_text"] == "Sentence one is here." and first["duration_seconds"] == 7
    assert sorted(f["frame"] for f in first["frame_images"]) == ["first", "last"]
    assert [k["timestamp_s"] for k in first["keyframes"]] == pytest.approx([7 / 3, 14 / 3])
    assert any(SRC in call for call in rig.ffmpeg)


def test_cloud_lip_sync_always_sends_a_voice_preset(rig):
    client = FakeClient()
    _lip(rig, client, voice="")
    assert [c["voice_id"] for c in client.calls] == [dubbing.DEFAULT_VOICE] * 2
    assert dubbing.DEFAULT_VOICE == "leo"


def test_out_of_credits_under_auto_runs_the_rest_on_this_computer(rig):
    client = FakeClient([{"error": "Out of credits.", "credits": True}])
    result = _lip(rig, client)
    assert len(client.calls) == 1                       # a credits refusal is not retried, and no cloud for the rest
    assert _commands(rig) == ["tts", "lipsync", "tts", "lipsync"]
    assert (result["cloud_chunks"], result["local_chunks"]) == (0, 2)


def test_out_of_credits_under_cloud_is_an_error_and_nothing_runs_on_this_computer(rig):
    client = FakeClient([{"error": "Out of credits.", "credits": True}])
    with pytest.raises(DubbingError, match="Out of credits"):
        _lip(rig, client, allow_local_fallback=False)
    assert rig.rt.worker_calls == []


def test_the_local_runtime_is_resolved_only_when_a_chunk_needs_it(rig):
    asked = []

    def resolve():
        asked.append(True)
        return PICKS

    _lip(rig, FakeClient(), picks=None, resolve_picks=resolve)
    assert asked == []
    _lip(rig, FakeClient([{"error": "Out of credits.", "credits": True}]), picks=None, resolve_picks=resolve)
    assert asked == [True]


def test_a_failing_chunk_is_retried_once_then_runs_on_this_computer(rig):
    client = FakeClient([{"error": "Grok busy"}, {"error": "Grok busy"}])
    result = _lip(rig, client)
    assert [c["dialogue_text"] for c in client.calls] == ["Sentence one is here.", "Sentence one is here.",
                                                          "Sentence two follows."]
    assert _commands(rig) == ["tts", "lipsync"]
    assert (result["cloud_chunks"], result["local_chunks"]) == (1, 1)


def test_words_that_do_not_match_the_script_are_a_failed_attempt(rig):
    client = FakeClient([{"video_url": "fake://Something else entirely"}])
    result = _lip(rig, client)
    assert len(client.calls) == 3
    assert (result["cloud_chunks"], result["local_chunks"]) == (2, 0)
    assert rig.rt.worker_calls == []


def test_a_cloud_lip_sync_under_cloud_with_no_failures_never_touches_this_computer(rig):
    result = _lip(rig, FakeClient(), allow_local_fallback=False)
    assert rig.rt.worker_calls == []
    assert os.path.exists(result["out"]) and result["seconds"] == pytest.approx(12.4)


# --- local lip sync and speech ------------------------------------------------------------

def test_a_recording_is_lip_synced_on_this_computer_with_the_cached_picks(rig, tmp_path):
    result = dubbing.lip_sync(source=SRC, start=0.0, end=5.0, size=(1280, 720), fps=24,
                              out=str(tmp_path / "out.mp4"), audio="/media/voice.wav", engine="local",
                              picks=PICKS)
    (command, args), = rig.rt.worker_calls
    assert command == "lipsync" and args["audio"] == "/media/voice.wav" and args["model"] == "edtalk"
    assert args["picks"] == PICKS and args["detect_every"] == 10
    assert os.path.exists(result["out"]) and result["local_chunks"] == 1


def test_local_speech_is_ming_then_lip_sync_over_the_whole_clip(rig, tmp_path):
    dubbing.lip_sync(source=SRC, start=0.0, end=5.0, size=(1280, 720), fps=24, out=str(tmp_path / "out.mp4"),
                     text="Hello [chuckle] there.", engine="local", picks=PICKS)
    assert _commands(rig) == ["tts", "lipsync"]
    speech = rig.rt.worker_calls[0][1]
    assert speech["text"] == "Hello [Laughter] there." and speech["task"] == "TTS"


def test_tts_local_is_plain_speech_unless_an_instruction_or_a_reference_is_given(rig, tmp_path):
    out = str(tmp_path / "n.wav")
    dubbing.tts_local("Hello [chuckle] there.", out, PICKS)
    dubbing.tts_local("Hi there.", out, PICKS, instruction="slow and clear",
                      prompt_wav="/media/ref.wav", prompt_text="Reference words.")
    plain, free = [args for _, args in rig.rt.worker_calls]
    assert plain["task"] == "TTS" and "instruction" not in plain and plain["max_decode_steps"] == 300
    assert free["task"] == "FREEFORM" and free["instruction"] == "slow and clear"
    assert free["prompt_wav"] == "/media/ref.wav" and free["prompt_text"] == "Reference words."


# --- face swap ----------------------------------------------------------------------------

def test_local_face_swap_keeps_the_clips_own_audio(rig, tmp_path):
    out = str(tmp_path / "swap.mp4")
    result = dubbing.face_swap(source=SRC, start=0.0, end=5.0, size=(1280, 720), fps=24, out=out,
                               face_image="/media/face.jpg", engine="local", picks=PICKS)
    (command, args), = rig.rt.worker_calls
    assert command == "faceswap" and args["face_image"] == "/media/face.jpg" and args["picks"] == PICKS
    assert result["out"] == out and os.path.exists(out) and "-shortest" in rig.ffmpeg[-1]


def test_cloud_face_swap_pins_swapped_frames_and_sends_the_face_as_a_reference(rig, tmp_path):
    client = FakeClient()
    dubbing.face_swap(source=SRC, start=0.0, end=6.0, size=(1280, 720), fps=24, out=str(tmp_path / "swap.mp4"),
                      face_image="/media/face.jpg", engine="cloud", picks=PICKS, client=client)
    call = client.calls[0]
    assert call["mode"] == "reference" and call["generate_audio"] is False
    assert len(call["reference_images"]) == 1 and call["duration_seconds"] == 6
    assert _commands(rig)[0] == "swap-frames"


def test_a_face_swap_chunk_the_cloud_refuses_is_swapped_on_this_computer(rig, tmp_path):
    client = FakeClient([{"error": "Out of credits.", "credits": True}])
    result = dubbing.face_swap(source=SRC, start=0.0, end=6.0, size=(1280, 720), fps=24,
                               out=str(tmp_path / "swap.mp4"), face_image="/media/face.jpg", engine="cloud",
                               picks=PICKS, client=client)
    assert _commands(rig) == ["swap-frames", "faceswap"]
    assert len(client.calls) == 1 and (result["cloud_chunks"], result["local_chunks"]) == (0, 1)
    assert os.path.exists(result["out"])


# --- local runtime consent ----------------------------------------------------------------

def test_missing_local_parts_are_installed_only_after_the_person_agrees(rig):
    rig.rt.status_value.update(env=False, face_models=False, ming=False, probe=None)
    asked = []
    with pytest.raises(DubbingError, match="declined"):
        dubbing.ensure_local({"face"}, consent=lambda steps: asked.append(steps) or False)
    assert asked and rig.rt.installed == []
    dubbing.ensure_local({"face"}, consent=lambda steps: True)
    assert rig.rt.installed == [{"face"}]


def test_installed_parts_ask_nothing(rig):
    asked = []
    dubbing.ensure_local({"face", "ming"}, consent=lambda steps: asked.append(steps) or True)
    assert asked == [] and rig.rt.installed == []


# --- the backend's 402 is a credits refusal -----------------------------------------------

def test_a_402_from_video_or_tts_is_a_credits_refusal_with_the_server_message(monkeypatch):
    from unittest.mock import MagicMock

    from classes import api_client
    from classes.api_client import ZenviBackendClient

    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    session = MagicMock()
    session.post.return_value.status_code = 402
    session.post.return_value.json.return_value = {"detail": {"message": "Out of credits. Add credits."}}
    client._session = session
    monkeypatch.setattr(client, "_auth_token", lambda: "jwt")
    monkeypatch.setattr(client, "_apply_bearer", lambda s: None)
    monkeypatch.setattr(api_client, "_refresh_credits_after_backend_billing", lambda: None)
    assert client.generate_video("x", duration_seconds=5) == {"error": "Out of credits. Add credits.", "credits": True}
    tts = client.generate_tts("hi")
    assert tts["credits"] is True and tts["success"] is False and tts["error"] == "Out of credits. Add credits."


def test_shot_cuts_are_read_from_ffmpeg_scene_detection_output():
    log = ("[Parsed_showinfo_1 @ 0x1] n:   0 pts:  36000 pts_time:1.5   duration: 1\n"
           "[Parsed_showinfo_1 @ 0x1] n:   1 pts:  96000 pts_time:4     duration: 1\n")
    assert dubbing.scene_times(log) == [1.5, 4.0]


def test_cloud_chunks_never_cross_a_shot_cut_found_in_the_clip(rig, monkeypatch):
    one = [_cue(0, 12, "one two three four five six seven eight nine ten eleven twelve.")]
    without = _lip(rig, FakeClient(), cues=one)["chunks"]
    monkeypatch.setattr(dubbing, "shot_cuts", lambda source, start, seconds: [6.0])
    assert _lip(rig, FakeClient(), cues=one)["chunks"] > without


def test_a_sentence_spanning_a_cut_is_split_at_the_cut_and_keeps_both_halves():
    chunks = dubbing.plan_chunks([_cue(0, 12, "one two three four five six seven eight.")], cut_times=[6.0],
                                 total=12.0)
    assert [(c["start"], c["end"]) for c in chunks] == [(0.0, 6.0), (6.0, 12.0)]
    assert " ".join(c["text"] for c in chunks).split() == "one two three four five six seven eight.".split()


def test_chunks_cover_the_whole_clip_with_no_gaps():
    chunks = dubbing.plan_chunks([_cue(0.5, 6, "Sentence one."), _cue(6.4, 12.4, "Sentence two.")], cut_times=[],
                                 total=13.0)
    assert chunks[0]["start"] == 0.0 and chunks[-1]["end"] == 13.0
    assert all(a["end"] == b["start"] for a, b in zip(chunks, chunks[1:]))
