"""add / list / remove captions: HyperFrames caption styles from Zenvi's own transcript."""

import json
import os
from types import SimpleNamespace

import pytest

from classes import hyperframes_local as hf
from classes import tool_handlers
from classes.editor_tools import REGISTRY
from classes.editor_tools import titles_text_captions as captions

L1, L2, L3 = 1000000, 2000000, 3000000
COMPONENT = """<div data-composition-id="x" data-duration="8"></div>
<script>var TRANSCRIPT = [
  { text: "Every", start: 0.0, end: 0.3 },
];</script>"""


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    """A clip with a transcript, a fake HyperFrames CLI and a recording placement."""
    monkeypatch.setattr(hf, "home", lambda: str(tmp_path / "hyperframes"))
    monkeypatch.setattr(hf, "status", lambda: {"ready": True})
    media = tmp_path / "interview.mp4"
    media.write_bytes(b"0" * 64)
    video = editor.add_file("video", path=str(media), has_audio=True)
    clip = editor.add_clip(video, position=10.0, layer=L1, start=2.0, end=7.0)       # 5 s on the timeline
    state = SimpleNamespace(commands=[], placed=[], place_kwargs=[], clip=clip, tmp=tmp_path, mattes=[], words=[
        {"text": "Hello", "timelineStartSec": 10.2, "timelineEndSec": 10.6},
        {"text": "there", "timelineStartSec": 10.6, "timelineEndSec": 11.1},
        {"text": "world", "timelineStartSec": 12.0, "timelineEndSec": 12.5},
    ])
    monkeypatch.setattr(captions, "transcript_words", lambda clip: state.words)
    monkeypatch.setattr(captions, "_subject_coverage", lambda project, media, at: 0.4)     # a speaker is in shot
    monkeypatch.setattr(captions, "_visible_band", lambda project, rel: None)

    def fake_matte(project, src, out, timeout=0):
        state.mattes.append((project, src, out))
        path = os.path.join(hf.project_dir(project), out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"\x1a\x45\xdf\xa3" + b"0" * 512)
        return {"frames": 125, "provider": "dml", "ms_per_frame": 40}

    monkeypatch.setattr(hf, "matting", lambda: "rvm")
    monkeypatch.setattr(hf, "rvm_ready", lambda: True)
    monkeypatch.setattr(hf, "matte", fake_matte)

    def fake_run(project, program, args, **kw):
        state.commands.append((program, list(args)))
        root = hf.project_dir(project)
        if args[0] == "init":
            os.makedirs(root, exist_ok=True)
            if os.listdir(root):                      # like the real CLI
                return 1, "Directory already exists and is not empty: ."
            open(os.path.join(root, "hyperframes.json"), "w").write("{}")
        elif args[0] == "add":
            path = os.path.join(root, "compositions", "components", args[1] + ".html")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(COMPONENT)
        elif "-o" in args or args[0] == "remove-background":
            out = os.path.join(root, args[args.index("-o") + 1])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            open(out, "wb").write(b"\x1a\x45\xdf\xa3" + b"0" * 512)
        elif program == "ffmpeg":
            open(os.path.join(root, args[-1]), "wb").write(b"0" * 64)
        return 0, "ok"

    def fake_import(path, *, preserve_alpha=None):
        from classes.query import File
        return File.get(id=editor.add_file("video", path=path)), None

    def fake_place(file_id="", position_seconds="", duration_seconds="", mode="", **_kw):
        state.place_kwargs.append(_kw)
        if str(file_id) == "F-any":
            return "Added clip to timeline timeline_clip_id=X (watched)."
        layer = L2 + L1 * len([p for p in state.placed if p["alive"]])
        cid = editor.add_clip(file_id, position=float(position_seconds), layer=layer, start=0.0,
                              end=float(duration_seconds))
        state.placed.append({"clip": cid, "file": file_id, "mode": mode, "alive": True})
        return "Added clip to timeline timeline_clip_id=%s (watched)." % cid

    monkeypatch.setattr(hf, "run", fake_run)
    monkeypatch.setattr(tool_handlers, "_import_generated_video", fake_import)
    monkeypatch.setattr(tool_handlers, "place_motion_graphic", fake_place)
    return state


def _compiled(studio, style="pill-karaoke"):
    root = hf.project_dir("captions-" + studio.clip.lower())
    return open(os.path.join(root, "compositions", "components", "caption-%s.html" % style)).read()


def test_the_old_caption_effect_tools_are_gone():
    assert {"add_captions_tool", "list_captions_tool", "remove_captions_tool"} <= set(REGISTRY)
    assert "edit_captions_tool" not in tool_handlers.AGENT_TOOL_HANDLERS
    assert "srtPath" not in REGISTRY["add_captions_tool"].schema["properties"]


def test_captions_are_rendered_from_the_clips_own_words_and_placed_over_it(editor, studio):
    r = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="highlight"))

    html = _compiled(studio, "highlight")
    assert '"Hello"' in html and '"start": 0.2' in html and 'data-duration="5"' in html and "Every" not in html
    program, args = [c for c in studio.commands if c[1][0] == "render"][0]
    assert program == "hyperframes" and args[args.index("--format") + 1] == "webm"
    assert studio.placed[0]["mode"] == "overlay"
    placed = editor.clip(r["timeline_clip_id"])
    assert placed["position"] == 10.0 and placed["end"] - placed["start"] == 5.0
    f = editor.file(r["file_id"])
    assert f["ai_metadata"]["captions"] == {"clip_id": studio.clip, "style": "highlight", "behind_subject": False,
                                            "role": "captions", "text": "Hello there world"}
    assert f["ai_metadata"]["source"] == "hyperframes_captions" and r["words"] == 3


def test_a_cached_transcript_in_the_clips_folder_does_not_block_the_first_render(editor, studio):
    root = hf.project_dir("captions-" + studio.clip.lower())
    os.makedirs(root)
    open(os.path.join(root, "speech.wav"), "wb").write(b"0" * 64)       # the Captions dock read the words first

    _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="highlight"))

    assert os.path.isfile(os.path.join(root, "hyperframes.json")) and os.path.isfile(os.path.join(root, "speech.wav"))


def test_captioning_again_replaces_the_captions_and_can_fix_words(editor, studio):
    first = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip))
    second = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="kinetic-slam",
                                  text="Hallo there, World"))
    assert second["replaced"] == 1 and editor.clip(first["timeline_clip_id"]) is None
    html = _compiled(studio, "kinetic-slam")
    assert '"Hallo"' in html and '"World"' in html and '"start": 2.0' in html      # timings kept
    listing = _receipt(editor.call("list_captions_tool"))
    assert [c["style"] for c in listing["captions"]] == ["kinetic-slam"]
    assert {"pill-karaoke", "highlight"} <= {s["style"] for s in listing["styles"]}


def test_behind_the_subject_adds_a_cutout_above_the_captions(editor, studio):
    r = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    trim = [a for p, a in studio.commands if p == "ffmpeg"][0]
    assert trim[trim.index("-ss") + 1] == "2.0" and trim[trim.index("-t") + 1] == "5.0"     # the clip's own range
    assert trim[trim.index("-fps_mode") + 1] == "cfr" and "/" in trim[trim.index("-r") + 1]  # the project's rate
    assert [m[1] for m in studio.mattes] == ["subject-source.mp4"]                          # RVM by default
    assert not any(a[0] == "remove-background" for _p, a in studio.commands)
    captions_clip, subject_clip = editor.clip(r["timeline_clip_id"]), editor.clip(r["subject_clip_id"])
    assert subject_clip["layer"] > captions_clip["layer"] > L1
    assert editor.file(r["subject_file_id"])["ai_metadata"]["captions"]["role"] == "subject"

    out = _receipt(editor.call("remove_captions_tool", timeline_clip_id=studio.clip))
    assert out["removed"] == 2 and editor.clip(r["timeline_clip_id"]) is None
    assert editor.clip(studio.clip) is not None                                  # the footage is untouched


def test_refusals_leave_the_timeline_alone(editor, studio, monkeypatch):
    before = len(editor.clips())
    assert editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="comic-sans").startswith("Error")
    studio.words = []
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip)
    assert out.startswith("Error") and "no spoken words" in out
    monkeypatch.setattr(hf, "status", lambda: {"ready": False})
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip)
    assert out.startswith("Error") and "hyperframes_setup_tool" in out
    assert editor.call("remove_captions_tool").startswith("Error")
    assert len(editor.clips()) == before and studio.placed == []


def test_without_the_on_device_recogniser_the_indexed_transcript_is_used(editor, studio, monkeypatch):
    from classes.agent_tools import transcript
    from classes.agent_tools.receipt import ToolReceipt

    monkeypatch.undo()                                   # the real transcript_words, the rest re-patched below
    monkeypatch.setattr(hf, "home", lambda: str(studio.tmp / "hyperframes"))   # never the real install
    monkeypatch.setattr(captions, "_speech_words", lambda clip: [])           # no on-device recogniser here
    monkeypatch.setattr(transcript, "get_transcript", lambda **kw: ToolReceipt.error(
        "get_transcript_tool", "Error: faster-whisper is not installed").to_json())
    # stored at indexing time, in the media's own seconds; the clip plays 2.0-7.0 of it at 10.0 s
    monkeypatch.setattr(tool_handlers, "_clip_transcript_cues", lambda clip_id: [
        {"text": "Hello there", "source_start": 3.0, "source_end": 4.0}])
    from classes.query import Clip
    words = captions.clip_caption_words(Clip.get(id=studio.clip))
    assert words == [{"text": "Hello", "start": 1.0, "end": 1.5}, {"text": "there", "start": 1.5, "end": 2.0}]

    monkeypatch.setattr(tool_handlers, "_clip_transcript_cues", lambda clip_id: [])
    with pytest.raises(captions.ToolError, match="faster-whisper"):
        captions.clip_caption_words(Clip.get(id=studio.clip))


def test_a_failed_subject_cutout_leaves_the_existing_captions_in_place(editor, studio, monkeypatch):
    first = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip))
    real_run = hf.run

    def failing_matte(project, program, args, **kw):
        if args[0] == "remove-background":
            return 1, "model download failed"
        return real_run(project, program, args, **kw)

    monkeypatch.setattr(hf, "run", failing_matte)
    monkeypatch.setattr(hf, "matte", lambda *a, **k: (_ for _ in ()).throw(hf.HyperframesError("no GPU")))
    # RVM fails, so HyperFrames' own is tried, and that fails too
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="highlight", behind_subject=True)
    assert out.startswith("Error") and "model download failed" in out
    assert editor.clip(first["timeline_clip_id"]) is not None           # nothing was replaced
    assert len(studio.placed) == 1


def test_retimed_clips_are_refused_instead_of_captioned_out_of_sync(editor, studio):
    from classes.query import Clip
    clip = Clip.get(id=studio.clip)
    clip.data["time"] = {"Points": [{"co": {"X": 1, "Y": 1}}, {"co": {"X": 300, "Y": 150}}]}
    clip.save()
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip)
    assert out.startswith("Error") and "speed" in out and studio.placed == []


def test_long_clips_are_captioned_for_their_whole_length(editor, studio):
    assert studio.placed == []
    captions._place("F-any", 0.0, 240.0)
    assert studio.place_kwargs[-1]["max_duration_seconds"] >= 240.0


def test_a_failed_subject_import_leaves_no_orphan_caption_file(editor, studio, monkeypatch):
    from classes.query import File

    real = tool_handlers._import_generated_video
    monkeypatch.setattr(tool_handlers, "_import_generated_video", lambda path, **kw: (
        (None, "no alpha channel") if "subject-" in os.path.basename(path) else real(path, **kw)))
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True)
    assert out.startswith("Error") and "no alpha channel" in out
    assert not [f for f in File.filter() if (f.data.get("ai_metadata") or {}).get("source") == captions.SOURCE]


def test_a_failed_import_leaves_the_existing_captions_in_place(editor, studio, monkeypatch):
    first = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip))
    monkeypatch.setattr(tool_handlers, "_import_generated_video", lambda path, **kw: (None, "disk full"))
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="highlight")
    assert out.startswith("Error") and "disk full" in out
    assert editor.clip(first["timeline_clip_id"]) is not None


def test_the_dock_shows_the_receipt_summary_not_the_raw_receipt():
    from windows.views.captions_dock import status_line
    receipt = json.dumps({"contract": 3, "status": "ok", "summary": "Captions added.\nmore", "data": {}})
    assert status_line(receipt) == "Captions added."
    assert status_line("Error: no clip") == "Error: no clip"
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "captions_dock.py")).read()
    assert "PyQt5" not in source                    # qt_api picks the binding


def test_overlays_are_placed_whole_with_no_visual_search(editor, studio):
    # The placement "watch" trimmed a 20 s caption render to 2-18 s: two seconds late and four short.
    editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True)
    assert [kw.get("full_file") for kw in studio.place_kwargs] == ["true", "true"]


def test_the_cutout_sits_exactly_over_the_speaker_and_the_captions_rise_behind_them(editor, studio, monkeypatch):
    from classes.query import Clip
    source = Clip.get(id=studio.clip)
    source.data.update({"scale": 0, "gravity": 4, "location_x": {"Points": [{"co": {"X": 1, "Y": 0.1}}]}})
    source.save()
    monkeypatch.setattr(captions, "_visible_band", lambda project, rel: (0.75, 0.87))      # a pill near the bottom

    r = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    subject, overlay = editor.clip(r["subject_clip_id"]), editor.clip(r["timeline_clip_id"])
    assert subject["scale"] == 0 and subject["location_x"]["Points"][0]["co"]["Y"] == 0.1
    lift = overlay["location_y"]["Points"][0]["co"]["Y"]
    assert lift == pytest.approx(captions.BEHIND_SUBJECT_CENTRE - 0.81)                    # up, to head height

    plain = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip))
    assert editor.clip(plain["timeline_clip_id"])["location_y"]["Points"][0]["co"]["Y"] == 0.0


def test_with_no_speaker_in_shot_the_captions_go_on_top_as_usual(editor, studio, monkeypatch):
    monkeypatch.setattr(captions, "_subject_coverage", lambda project, media, at: 0.0)
    from classes.query import Clip
    assert captions.has_subject(Clip.get(id=studio.clip)) is False
    r = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    assert r["behind_subject"] is False and "subject_clip_id" not in r and len(studio.placed) == 1
    assert not any(a[0] == "remove-background" for _p, a in studio.commands)


def test_the_files_index_says_whether_anyone_is_on_camera(editor, studio, monkeypatch):
    from classes.query import Clip, File
    probes = []
    monkeypatch.setattr(captions, "_subject_coverage", lambda project, media, at: probes.append(at) or 0.0)
    clip = Clip.get(id=studio.clip)                                   # plays 2.0-7.0 of the file
    f = File.get(id=clip.data["file_id"])

    def indexed(chapters):
        f.data["ai_metadata"] = {"chapters": chapters}
        f.save()
        return captions.has_subject(Clip.get(id=studio.clip))

    # The speaker walks in half way, then two people: any of it is enough, and no frame is probed.
    assert indexed([{"start": 0, "end": 4, "people": 0}, {"start": 4, "end": 9, "people": 2}]) is True
    assert indexed([{"start": 0, "end": 9, "people": 0}]) is False                     # b-roll throughout
    assert indexed([{"start": 0, "end": 2, "people": 1}, {"start": 2, "end": 9, "people": 0}]) is False
    assert probes == []
    assert indexed([{"start": 0, "end": 9, "title": "indexed before people was recorded"}]) is False
    assert probes == [4.5, 3.0, 6.0]                                  # three frames across the clip


def test_the_bundled_recogniser_times_the_words_when_zenvis_own_is_missing(editor, studio, monkeypatch):
    from classes.agent_tools import transcript
    from classes.agent_tools.receipt import ToolReceipt
    from classes.query import Clip

    monkeypatch.undo()
    monkeypatch.setattr(hf, "home", lambda: str(studio.tmp / "hyperframes"))
    monkeypatch.setattr(transcript, "get_transcript", lambda **kw: ToolReceipt.error(
        "get_transcript_tool", "Error: faster-whisper is not installed").to_json())
    monkeypatch.setattr(tool_handlers, "_clip_transcript_cues", lambda clip_id: [
        {"text": "evenly spread guess", "source_start": 2.0, "source_end": 7.0}])
    ran, heard, state = [], [], {"speech": False, "parakeet": False, "installable": True, "whistle_here": True}

    def fake_run(project, program, args, **kw):
        ran.append((program, list(args)))
        os.makedirs(hf.project_dir(project), exist_ok=True)
        if args[0] == "transcribe":
            open(os.path.join(hf.project_dir(project), "transcript.json"), "w").write(json.dumps(
                [{"text": "Hello,", "start": 0.3, "end": 0.5}, {"text": "there.", "start": 0.6, "end": 1.1}]))
        return 0, "ok"

    def fake_install():
        if not state["installable"]:
            raise hf.HyperframesError("no speech recogniser is published for this computer")
        state["speech"] = True

    monkeypatch.setattr(hf, "run", fake_run)
    monkeypatch.setattr(hf, "status", lambda: dict(state, ready=True))
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: state["speech"])
    monkeypatch.setattr(hf, "install_speech", fake_install)
    monkeypatch.setattr(hf, "speech_supported", lambda: state["whistle_here"])
    monkeypatch.setattr(hf, "install_parakeet", lambda: state.update(parakeet=True))
    monkeypatch.setattr(hf, "transcribe", lambda wav: heard.append(wav) or [
        {"text": "hello", "start": 0.31, "end": 0.52}, {"text": "there", "start": 0.6, "end": 1.0}])

    words = captions.clip_caption_words(Clip.get(id=studio.clip))
    assert words == [{"text": "hello", "start": 0.31, "end": 0.52}, {"text": "there", "start": 0.6, "end": 1.0}]
    assert state["speech"] is True                                                         # fetched on first use
    cut = [a for p, a in ran if p == "ffmpeg"][0]
    assert cut[cut.index("-ss") + 1] == "2.0" and cut[cut.index("-t") + 1] == "5.0"       # the clip's own audio
    captions.clip_caption_words(Clip.get(id=studio.clip))
    assert len(heard) == 1 and not any(a[0] == "transcribe" for _p, a in ran)              # kept for next time

    monkeypatch.setattr(hf, "recogniser", lambda: "parakeet" if state.get("chosen") else
                        "whistle" if state["whistle_here"] else "parakeet")
    state["parakeet"] = True
    words = captions.clip_caption_words(Clip.get(id=studio.clip))
    assert [w["text"] for w in words] == ["hello", "there"] and len(heard) == 1          # installed, not chosen
    state["chosen"] = True                                      # the user picked the larger recogniser
    words = captions.clip_caption_words(Clip.get(id=studio.clip))
    assert [w["text"] for w in words] == ["Hello,", "there."] and len(heard) == 1
    asked = [a for _p, a in ran if a[0] == "transcribe"][0]
    assert asked[asked.index("--engine") + 1] == "parakeet"

    # A computer Whistle has no engine for (Intel Mac): Parakeet is used instead, and because it
    # is a 900 MB download the user is asked first.
    from classes.editor_tools import ai_generation_hyperframes as setup
    state.update(speech=False, parakeet=False, whistle_here=False)
    asked = []
    monkeypatch.setattr(hf, "install_plan", lambda st, **kw: [{"id": "parakeet", "label": "Parakeet, 900 MB"}])
    monkeypatch.setattr(setup, "_ask_install_consent", lambda steps: asked.append(steps) or False)
    mac = editor.add_clip(Clip.get(id=studio.clip).data["file_id"], position=50.0, layer=L1, start=2.0, end=7.0)
    assert [w["text"] for w in captions.clip_caption_words(Clip.get(id=mac))] == ["evenly", "spread", "guess"]
    assert state["parakeet"] is False and asked[0][0]["id"] == "parakeet"          # declined: nothing fetched
    monkeypatch.setattr(setup, "_ask_install_consent", lambda steps: True)
    assert [w["text"] for w in captions.clip_caption_words(Clip.get(id=mac))] == ["Hello,", "there."]
    assert state["parakeet"] is True and state["speech"] is False and len(heard) == 1

    state.update(speech=False, parakeet=False, installable=False, whistle_here=True, chosen=False)  # neither: fall back
    other = editor.add_clip(Clip.get(id=studio.clip).data["file_id"], position=30.0, layer=L1, start=2.0, end=7.0)
    assert [w["text"] for w in captions.clip_caption_words(Clip.get(id=other))] == ["evenly", "spread", "guess"]


def test_the_whole_project_is_captioned_clip_by_clip(editor, studio):
    from classes.query import Clip
    file_id = Clip.get(id=studio.clip).data["file_id"]
    silent = editor.add_clip(file_id, position=30.0, layer=L1, start=0.0, end=4.0)      # no words fall in it

    r = _receipt(editor.call("add_captions_tool", whole_project=True, style="highlight"))
    assert [c["clip_id"] for c in r["captioned"]] == [studio.clip]
    assert [(s["clip_id"], "no spoken words" in s["reason"]) for s in r["skipped"]] == [(silent, True)]
    assert len(studio.placed) == 1

    # Again: the caption overlay is not itself a clip to caption, and the old captions are replaced.
    again = _receipt(editor.call("add_captions_tool", whole_project=True))
    assert [c["clip_id"] for c in again["captioned"]] == [studio.clip] and again["captioned"][0]["replaced"] == 1
    assert len(_receipt(editor.call("list_captions_tool"))["captions"]) == 1

    studio.words = []
    out = editor.call("add_captions_tool", whole_project=True)
    assert out.startswith("Error") and "no spoken words" in out


def test_each_stage_is_reported_while_captions_are_built(editor, studio, monkeypatch):
    stages = []
    monkeypatch.setattr(captions, "on_progress", stages.append)
    editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True)
    joined = " | ".join(stages).lower()
    assert joined.index("speech") < joined.index("rendering") < joined.index("cutting out") < joined.index("placing")

    monkeypatch.setattr(captions, "on_progress", lambda message: 1 / 0)          # a broken listener is not fatal
    assert not editor.call("add_captions_tool", timeline_clip_id=studio.clip).startswith("Error")


def test_the_dock_loads_words_on_selection_and_can_caption_everything():
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "captions_dock.py")).read()
    selection = source[source.index("def selection_changed"):source.index("def _check_subject")]
    assert "self.load_words()" in selection
    assert '"whole_project": True' in source and "tool.on_progress" in source


@pytest.mark.parametrize("fail_on", [1, 2])
def test_a_failed_placement_puts_the_old_captions_back(editor, studio, monkeypatch, fail_on):
    _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="highlight"))
    real_place, calls = tool_handlers.place_motion_graphic, []

    def flaky(**kw):
        calls.append(kw)
        return "Error: the timeline is locked" if len(calls) == fail_on else real_place(**kw)

    monkeypatch.setattr(tool_handlers, "place_motion_graphic", flaky)
    out = editor.call("add_captions_tool", timeline_clip_id=studio.clip, style="kinetic-slam", behind_subject=True)
    assert out.startswith("Error") and "locked" in out
    listing = _receipt(editor.call("list_captions_tool"))["captions"]
    assert [(c["style"], c["role"]) for c in listing] == [("highlight", "captions")]      # none of the new pair
    from classes.query import File
    styles = [f.data["ai_metadata"]["captions"]["style"] for f in File.filter()
              if "captions" in (f.data.get("ai_metadata") or {})]
    assert styles == ["highlight"]                                                        # nor their files


def test_a_failed_subject_check_does_not_rule_the_speaker_out(editor, studio, monkeypatch):
    from classes.query import Clip
    answers = iter([None, 0.0, 0.0])
    monkeypatch.setattr(captions, "_subject_coverage", lambda project, media, at: next(answers))
    assert captions.has_subject(Clip.get(id=studio.clip)) is False       # one failure, two empty frames
    monkeypatch.setattr(captions, "_subject_coverage", lambda project, media, at: None)
    assert captions.has_subject(Clip.get(id=studio.clip)) is True        # nothing could be checked


def test_the_cut_out_falls_back_to_hyperframes_own_and_can_be_chosen(editor, studio, monkeypatch):
    def broken(project, src, out, timeout=0):
        raise hf.HyperframesError("the GPU driver crashed")

    monkeypatch.setattr(hf, "matte", broken)
    r = _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    assert "subject_clip_id" in r and any(a[0] == "remove-background" for _p, a in studio.commands)

    studio.commands.clear()
    monkeypatch.setattr(hf, "matting", lambda: "u2net")                  # the user picked HyperFrames' own
    _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    assert any(a[0] == "remove-background" for _p, a in studio.commands)

    # RVM chosen but not on this computer yet: it is fetched first (small, like the speech model)
    fetched = []
    monkeypatch.setattr(hf, "matting", lambda: "rvm")
    monkeypatch.setattr(hf, "rvm_ready", lambda: bool(fetched))
    monkeypatch.setattr(hf, "install_rvm", lambda: fetched.append(1))
    def matte(project, src, out, timeout=0):
        path = os.path.join(hf.project_dir(project), out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"0" * 64)
        studio.mattes.append(src)
        return {"frames": 1, "provider": "cpu", "ms_per_frame": 1}

    monkeypatch.setattr(hf, "matte", matte)
    studio.commands.clear()
    _receipt(editor.call("add_captions_tool", timeline_clip_id=studio.clip, behind_subject=True))
    assert fetched == [1] and not any(a[0] == "remove-background" for _p, a in studio.commands)
