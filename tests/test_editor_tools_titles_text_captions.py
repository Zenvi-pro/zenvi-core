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
    video = editor.add_file("video", path=str(media))
    clip = editor.add_clip(video, position=10.0, layer=L1, start=2.0, end=7.0)       # 5 s on the timeline
    state = SimpleNamespace(commands=[], placed=[], place_kwargs=[], clip=clip, words=[
        {"text": "Hello", "timelineStartSec": 10.2, "timelineEndSec": 10.6},
        {"text": "there", "timelineStartSec": 10.6, "timelineEndSec": 11.1},
        {"text": "world", "timelineStartSec": 12.0, "timelineEndSec": 12.5},
    ])
    monkeypatch.setattr(captions, "transcript_words", lambda clip: state.words)

    def fake_run(project, program, args, **kw):
        state.commands.append((program, list(args)))
        root = hf.project_dir(project)
        if args[0] == "init":
            os.makedirs(root, exist_ok=True)
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
    assert any(a[0] == "remove-background" for _p, a in studio.commands)
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
