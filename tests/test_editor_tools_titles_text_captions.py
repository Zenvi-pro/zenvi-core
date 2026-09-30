"""Captions: classes.caption_cues and add/list/edit/remove_captions_tool."""

import os

import pytest

from classes import caption_cues
from classes.caption_cues import Cue
from titles_text_fakes import receipt, tt  # noqa: F401  (fixture)

SRT = """1
00:00:00,500 --> 00:00:03,000
Welcome back to my channel.

2
00:00:03,200 --> 00:00:07,500
Today we are exploring the city.

3
00:00:07,700 --> 00:00:11,000
First stop: my favorite coffee shop.
"""


# ---------------------------------------------------------------------------
# caption_cues
# ---------------------------------------------------------------------------

def test_caption_text_round_trip_in_the_docks_format():
    cues = [Cue(0.5, 3.0, "Welcome back"), Cue(3.2, 7.5, "Two\nlines")]
    text = caption_cues.build_caption_text(cues)
    assert text.startswith("00:00:00:500 --> 00:00:03:000\nWelcome back\n\n00:00:03:200 --> 00:00:07:500\nTwo\nlines")
    back = caption_cues.parse_caption_text(text)
    assert [(c.start, c.end, c.text) for c in back] == [(0.5, 3.0, "Welcome back"), (3.2, 7.5, "Two\nlines")]
    assert caption_cues.format_time(1.001) == "00:00:01:001"
    assert caption_cues.format_time(3725.5) == "01:02:05:500"


def test_parser_reads_raw_srt_like_libopenshot_does():
    cues = caption_cues.parse_caption_text(SRT)
    assert [c.start for c in cues] == [0.5, 3.2, 7.7]
    # the SRT index line "2" follows the first cue's text; libopenshot draws only lines longer than 1 char
    assert caption_cues.drawn_lines(cues[0].text) == ["Welcome back to my channel."]


def test_libopenshot_would_cut_a_cue_at_a_time_in_its_text_and_safe_text_prevents_it():
    raw = "00:00:01:000 --> 00:00:04:000\nMeet at 10:30 today\n\n"
    assert caption_cues.parse_caption_text(raw)[0].text == "Meet at"
    safe, changed = caption_cues.safe_text("Meet at 10:30 today")
    assert changed and safe == "Meet at 10\u200b:30 today"
    text = caption_cues.build_caption_text([Cue(1, 4, safe)])
    assert caption_cues.display_text(caption_cues.parse_caption_text(text)[0].text) == "Meet at 10:30 today"
    assert caption_cues.display_text("old 10\u223630 style") == "old 10:30 style"


def test_safe_text_keeps_one_letter_and_note_lines_drawable():
    safe, changed = caption_cues.safe_text("I\n\nNOTE: this matters")
    assert changed
    lines = safe.split("\n")
    assert caption_cues.drawn_lines(safe) == lines and len(lines) == 2
    assert caption_cues.display_text(safe) == "I\nNOTE: this matters"
    assert caption_cues.safe_text("🔥")[0] == "🔥"          # two UTF-16 units: already drawn


def test_subtitle_files_srt_and_vtt():
    cues = caption_cues.parse_subtitles("﻿" + SRT.replace("\n", "\r\n"))
    assert [(c.start, c.end) for c in cues] == [(0.5, 3.0), (3.2, 7.5), (7.7, 11.0)]
    assert cues[2].text == "First stop: my favorite coffee shop."
    vtt = ("WEBVTT - demo\n\nNOTE made by hand\n\nintro\n00:01.000 --> 00:02.500 align:start position:10%\n"
           "<b>Hello</b> &amp; welcome\n<i>line two</i>\n\n00:00:03.000 --> 00:00:04.000\n{\\an8}Top\n")
    v = caption_cues.parse_subtitles(vtt)
    assert [(c.start, c.end, c.text) for c in v] == [(1.0, 2.5, "Hello & welcome\nline two"), (3.0, 4.0, "Top")]
    assert caption_cues.parse_subtitles("no timings here") == []


def test_long_transcript_cues_are_phrased():
    cue = Cue(10.0, 20.0, "so today we are going to walk all the way across the city and find the best coffee")
    parts = caption_cues.split_cue(cue, max_words=8, max_chars=42)
    assert len(parts) >= 2
    assert all(len(p.text.split()) <= 8 and len(p.text) <= 42 for p in parts[:-1])
    assert parts[0].start == 10.0 and parts[-1].end == 20.0
    assert all(abs(a.end - b.start) < 1e-9 for a, b in zip(parts, parts[1:]))
    assert " ".join(p.text for p in parts) == cue.text
    assert caption_cues.split_cue(Cue(0, 1, "short one"), 8, 42)[0].text == "short one"


def test_phrasing_breaks_at_sentences_and_never_leaves_a_lone_word():
    # found live on the talking head: word-count splitting gave "downtown. The latte art here is" / "incredible."
    cue = Cue(0, 10, "First stop is my favorite coffee shop downtown. The latte art here is incredible.")
    texts = [c.text for c in caption_cues.split_cue(cue, 8, 42)]
    assert texts[-1] == "The latte art here is incredible."
    assert all(len(t.split()) > 1 for t in texts)
    assert "downtown. The" not in " | ".join(texts)


# ---------------------------------------------------------------------------
# add_captions_tool
# ---------------------------------------------------------------------------

def _speaker(tt, position=0.0, start=0.0, end=None, **file_overrides):
    f = tt.add_file("video", duration=22.7, **file_overrides)
    return tt.add_clip(f, position=position, start=start, end=end if end is not None else 22.7)


def _caption(tt, clip_id, n=0):
    effects = [e for e in tt.clip(clip_id)["effects"] if e["class_name"] == "Caption"]
    return effects[n] if len(effects) > n else None


def _srt_file(tt):
    path = tt.tmp_path / "captions.srt"
    path.write_text(SRT, encoding="utf-8")
    return str(path)


def test_cues_on_a_clip_use_timeline_seconds_and_one_undo(tt):
    clip = _speaker(tt, position=2.0)
    out = tt.call("add_captions_tool", timeline_clip_id=clip,
                  cues=[{"start": 3.0, "end": 5.0, "text": "Hello there"}, {"start": 5.5, "end": 7, "text": "Bye"}])
    rec = receipt(out)
    assert out.startswith("Added 2 caption(s) from cues on " + clip)
    effect = _caption(tt, clip)
    assert effect["apply_before_clip"] is False
    cues = caption_cues.parse_caption_text(effect["caption_text"])
    assert [(c.start, c.end) for c in cues] == [(1.0, 3.0), (3.5, 5.0)]     # clip source time
    assert rec["captioned"][0]["first"] == 3.0 and rec["captioned"][0]["last"] == 7.0
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert _caption(tt, clip) is None


def test_subtitle_file_on_a_trimmed_clip_keeps_media_time(tt):
    clip = _speaker(tt, position=5.0, start=1.0, end=9.0)
    rec = receipt(tt.call("add_captions_tool", srtPath=_srt_file(tt), clipId=clip))
    cues = caption_cues.parse_caption_text(_caption(tt, clip)["caption_text"])
    assert [c.start for c in cues] == [0.5, 3.2, 7.7]
    assert rec["captioned"][0]["first"] == 4.5          # 0.5 s of media at position 5 - trim 1
    rows = receipt(tt.call("list_captions_tool", timeline_clip_id=clip))["caption_tracks"][0]["cues"]
    assert [r["visible"] for r in rows] == [True, True, True]
    assert rows[1]["start"] == 7.2 and rows[1]["source_start"] == 3.2


def test_cues_outside_the_clip_are_skipped_or_refused(tt):
    clip = _speaker(tt, position=0.0, start=0.0, end=4.0)
    rec = receipt(tt.call("add_captions_tool", srtPath=_srt_file(tt), timeline_clip_id=clip))
    assert rec["captioned"][0]["cues"] == 2 and "outside" in rec["warnings"][0]
    tt.mark()
    out = tt.call("add_captions_tool", timeline_clip_id=clip, cues=[{"start": 30, "end": 31, "text": "late"}])
    assert out.startswith("Error") and "none of the 1 cue" in out
    assert tt.undo_steps_since_mark() == 0


def test_subtitles_without_a_clip_go_on_a_captions_overlay(tt):
    video = tt.add_file("video", duration=30.0)
    tt.add_clip(video, position=0.0, layer=1000000)
    rec = receipt(tt.call("add_captions_tool", srtPath=_srt_file(tt), style="yellow"))
    entry = rec["captioned"][0]
    assert entry["overlay"] is True and entry["layer"] == 2000000
    overlay = tt.clip(entry["timeline_clip_id"])
    assert overlay["zenvi_role"] == "captions" and overlay["title"] == "Captions"
    assert overlay["position"] == 0.5 and round(overlay["end"] - overlay["start"], 3) == 10.5
    cues = caption_cues.parse_caption_text(_caption(tt, overlay["id"])["caption_text"])
    assert cues[0].start == 0.0 and cues[-1].end == 10.5
    carrier = tt.file(overlay["file_id"])
    assert carrier["zenvi_role"] == "text_overlay" and os.path.exists(carrier["path"])
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert tt.clip(overlay["id"]) is None and tt.file(overlay["file_id"]) is None


def test_captions_from_the_indexed_transcript(tt):
    ai = {"analyzed": True, "transcript_cues": [
        {"start": 1.0, "end": 6.0, "text": "so today we are going to walk all the way across the city together"},
        {"start": 7.0, "end": 8.0, "text": "at 10:30"}]}
    clip = _speaker(tt, ai_metadata=ai)
    rec = receipt(tt.call("add_captions_tool", clipId=clip, maxWords=6))
    cues = caption_cues.parse_caption_text(_caption(tt, clip)["caption_text"])
    assert rec["source"] == "transcript" and len(cues) >= 3
    assert all(len(caption_cues.display_text(c.text).split()) <= 6 for c in cues)
    assert caption_cues.display_text(cues[-1].text) == "at 10:30" and "\u200b:" in cues[-1].text
    assert any("adjusted" in w for w in rec["warnings"])


def test_transcript_captions_for_every_speech_clip_and_refusal_without_one(tt):
    ai = {"analyzed": True, "transcript_cues": [{"start": 0.5, "end": 2.0, "text": "hello"}]}
    a = _speaker(tt, ai_metadata=ai)
    silent = tt.add_file("video", duration=10.0)
    tt.add_clip(silent, position=30.0)
    rec = receipt(tt.call("add_captions_tool"))
    assert [e["timeline_clip_id"] for e in rec["captioned"]] == [a]
    tt.mark()
    out = tt.call("add_captions_tool", timeline_clip_id=tt.clips()[-1]["id"])
    assert out.startswith("Error") and "no transcript" in out and "cues / srtPath" in out
    assert tt.undo_steps_since_mark() == 0


def test_replace_existing_or_add_a_second_track(tt):
    clip = _speaker(tt)
    tt.call("add_captions_tool", timeline_clip_id=clip, cues=[{"start": 1, "end": 2, "text": "first"}])
    tt.call("add_captions_tool", timeline_clip_id=clip, cues=[{"start": 3, "end": 4, "text": "second"}])
    assert _caption(tt, clip, 1) is None
    assert caption_cues.parse_caption_text(_caption(tt, clip)["caption_text"])[0].text == "second"
    tt.call("add_captions_tool", timeline_clip_id=clip, replace_existing=False,
            cues=[{"start": 5, "end": 6, "text": "third"}])
    assert _caption(tt, clip, 1) is not None


def test_styles_map_to_caption_properties(tt):
    tt.store._data["width"], tt.store._data["height"] = 1080, 1920
    clip = _speaker(tt)
    rec = receipt(tt.call("add_captions_tool", timeline_clip_id=clip, style="reels", uppercase=True,
                          cues=[{"start": 1, "end": 2, "text": "big words"}]))
    e = _caption(tt, clip)
    assert e["caption_font"] == "Arial Black"
    assert e["stroke_width"]["Points"][0]["co"]["Y"] > 0
    assert e["background_alpha"]["Points"][0]["co"]["Y"] == 0.0
    # 0.075 of the 1080 px short side = 81 px; Caption scales by width/600 -> 45
    assert e["font_size"]["Points"][0]["co"]["Y"] == 45.0
    assert caption_cues.parse_caption_text(e["caption_text"])[0].text == "BIG WORDS"
    top_bottom = e["top"]["Points"][0]["co"]["Y"]
    assert 0.6 < top_bottom < 0.8 and rec["look"]["position"] == "bottom"
    tt.call("edit_captions_tool", timeline_clip_id=clip, position="top")
    assert _caption(tt, clip)["top"]["Points"][0]["co"]["Y"] < 0.12


@pytest.mark.parametrize("args, needle", [
    ({"cues": [{"start": 1, "end": 2, "text": "a"}], "srtPath": "/x.srt"}, "either cues or srtPath"),
    ({"srtPath": "/definitely/missing.srt"}, "not found"),
    ({"cues": [{"start": 2, "end": 1, "text": "a"}]}, "0 <= start < end"),
    ({"cues": [{"start": 1, "end": 2, "text": " "}]}, "has no text"),
    ({"cues": [{"start": 1, "end": 2, "text": "a"}], "trackIndex": 1}, "trackIndex"),
    ({"cues": [{"start": 1, "end": 2, "text": "a"}], "time_base": "source"}, "needs the clip"),
    ({"style": "comic"}, "must be one of"),
    ({"cues": [{"start": 1, "end": 2, "text": "a"}], "text_color": "nope"}, "text_color"),
    ({"clipId": "nope", "cues": [{"start": 1, "end": 2, "text": "a"}]}, "no timeline clip"),
])
def test_add_captions_refusals(tt, args, needle):
    _speaker(tt)
    tt.mark()
    out = tt.call("add_captions_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert tt.undo_steps_since_mark() == 0


def test_locked_clip_is_refused(tt):
    clip = _speaker(tt)
    tt.lock_track(1000000)
    out = tt.call("add_captions_tool", timeline_clip_id=clip, cues=[{"start": 1, "end": 2, "text": "a"}])
    assert out.startswith("Error") and "locked" in out


# ---------------------------------------------------------------------------
# list / edit / remove
# ---------------------------------------------------------------------------

def _captioned(tt):
    clip = _speaker(tt, position=10.0)
    tt.call("add_captions_tool", timeline_clip_id=clip, cues=[
        {"start": 11, "end": 12, "text": "one"}, {"start": 13, "end": 14, "text": "two"},
        {"start": 15, "end": 16, "text": "three Tokio"}])
    tt.mark()
    return clip


def test_list_captions(tt):
    clip = _captioned(tt)
    out = receipt(tt.call("list_captions_tool"))
    [track] = out["caption_tracks"]
    assert track["timeline_clip_id"] == clip and [c["index"] for c in track["cues"]] == [1, 2, 3]
    assert track["cues"][0] == {"index": 1, "start": 11.0, "end": 12.0, "source_start": 1.0, "source_end": 2.0,
                                "text": "one", "visible": True}
    assert tt.undo_steps_since_mark() == 0


def test_edit_cues_text_timing_delete_add_find_shift_in_one_step(tt):
    clip = _captioned(tt)
    rec = receipt(tt.call("edit_captions_tool", timeline_clip_id=clip,
                          cue_edits=[{"index": 1, "text": "ONE"}, {"index": 2, "start": 12.5, "end": 14.5},
                                     {"index": 3, "delete": False}],
                          add_cues=[{"start": 20, "end": 21, "text": "four"}],
                          find="Tokio", replace="Tokyo", shift_seconds=0.5))
    texts = [c["text"] for c in rec["cues"]]
    assert texts == ["ONE", "two", "three Tokyo", "four"]
    assert [c["start"] for c in rec["cues"]] == [11.5, 13.0, 15.5, 20.5]
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert [c.text for c in caption_cues.parse_caption_text(_caption(tt, clip)["caption_text"])] == [
        "one", "two", "three Tokio"]
    rec = receipt(tt.call("edit_captions_tool", cue_edits=[{"index": 2, "delete": True}]))
    assert [c["text"] for c in rec["cues"]] == ["one", "three Tokio"]


def test_restyle_keeps_the_cues(tt):
    clip = _captioned(tt)
    before = _caption(tt, clip)["caption_text"]
    tt.call("edit_captions_tool", effect_id=_caption(tt, clip)["id"], text_color="#ffe14d", text_size=0.08)
    e = _caption(tt, clip)
    assert e["caption_text"] == before
    assert e["color"]["green"]["Points"][0]["co"]["Y"] == 225.0
    assert tt.undo_steps_since_mark() == 1


@pytest.mark.parametrize("args, needle", [
    ({}, "nothing to change"),
    ({"cue_edits": [{"index": 9, "text": "x"}]}, "there is no cue 9"),
    ({"cue_edits": [{"index": 1, "text": ""}]}, "empty text"),
    ({"cue_edits": [{"index": 1, "start": 12, "end": 11}]}, "end must be after start"),
    ({"find": "absent", "replace": "x"}, "no caption contains"),
    ({"replace": "x"}, "replace needs find"),
    ({"add_cues": [{"start": 50, "end": 51, "text": "x"}]}, "outside clip"),
    ({"cue_edits": [{"index": 1, "delete": True}, {"index": 2, "delete": True}, {"index": 3, "delete": True}]},
     "remove_captions_tool"),
])
def test_edit_captions_refusals(tt, args, needle):
    _captioned(tt)
    out = tt.call("edit_captions_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert tt.undo_steps_since_mark() == 0


def test_edit_needs_a_target_when_several_clips_have_captions(tt):
    _captioned(tt)
    other = _speaker(tt, position=40.0)
    tt.call("add_captions_tool", timeline_clip_id=other, cues=[{"start": 41, "end": 42, "text": "x"}])
    out = tt.call("edit_captions_tool", shift_seconds=1)
    assert out.startswith("Error") and "which captions" in out
    assert tt.call("edit_captions_tool", shift_seconds=1, timeline_clip_id=other).startswith("Updated captions")


def test_remove_captions_never_deletes_clips(tt):
    clip = _captioned(tt)
    n_clips = len(tt.clips())
    out = tt.call("remove_captions_tool", timeline_clip_id=clip)
    assert out.startswith("Removed 1 caption track(s)") and _caption(tt, clip) is None
    assert len(tt.clips()) == n_clips
    assert tt.undo_steps_since_mark() == 1
    tt.undo()
    assert _caption(tt, clip) is not None
    out = tt.call("remove_captions_tool", all_captions=True)
    assert out.startswith("Removed 1")
    assert tt.call("remove_captions_tool", all_captions=True).startswith("Error")


def test_remove_overlay_captions_reports_the_leftover_clip(tt):
    rec = receipt(tt.call("add_captions_tool", cues=[{"start": 1, "end": 2, "text": "hi"}]))
    overlay = rec["captioned"][0]["timeline_clip_id"]
    out = tt.call("remove_captions_tool", effect_id=rec["captioned"][0]["effect_id"])
    assert "delete_from_timeline_tool" in out and tt.clip(overlay) is not None
    assert receipt(out)["empty_overlay_clip_ids"] == [overlay]


def test_outlines_only_stay_heavy_on_heavy_fonts(tt, monkeypatch):
    # found live: the yellow preset's 0.07-em black outline on a regular font hid the yellow fill
    from classes.editor_tools import titles_text_captions as caps
    clip = _speaker(tt)
    monkeypatch.setattr(caps, "installed_font_families", lambda: ["Arial", "Arial Black"])
    rec = receipt(tt.call("add_captions_tool", timeline_clip_id=clip, style="yellow",
                          cues=[{"start": 1, "end": 2, "text": "yellow words"}]))
    assert rec["look"]["font"] == "Arial Black" and rec["look"]["stroke_width"] == 0.07
    monkeypatch.setattr(caps, "installed_font_families", lambda: ["Arial"])      # no heavy font here
    rec = receipt(tt.call("add_captions_tool", timeline_clip_id=clip, style="yellow",
                          cues=[{"start": 1, "end": 2, "text": "yellow words"}]))
    assert rec["look"]["font"] == "sans" and rec["look"]["stroke_width"] == caps.THIN_OUTLINE
    rec = receipt(tt.call("add_captions_tool", timeline_clip_id=clip, style="yellow", stroke_width=0.1,
                          cues=[{"start": 1, "end": 2, "text": "yellow words"}]))
    assert rec["look"]["stroke_width"] == 0.1                                     # an explicit request wins
