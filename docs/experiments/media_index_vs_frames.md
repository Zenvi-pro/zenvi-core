# Describing a video: sampling frames vs the media index

One file, one prompt, measured the same way each time.

- **File:** a 90 s anime ending (`【推しの子】…(888p).mp4`, 1920x888, 23.976 fps, 21.5 MB, Japanese title tag).
- **Prompt:** "watch the clip imported and tell me what happens scene by scene, be explicitly detailed".
- **Agent:** Claude Code (`claude-sonnet-5-5`) through the `zenvi-editor` MCP.
- **Numbers:** `python scripts/claude_session_stats.py <session>.jsonl` (calls, images shown, model tokens). Zenvi credits are read from
  the account balance before and after. Anything not measured says so.

## Ground truth for this file (from the index, not from an agent)
- 85 shots (cut every 0.4 to 4 s; many under half a second), 136 BPM, 197 beats, loudness -6.1 LUFS.
- Local layers (structure, look, audio, speech) built in under a minute. Descriptions + vectors (Gemini `gemini-3.5-flash-lite`):
  18,143 prompt tokens, 22,154 output tokens, 4 batches, saved on the shelf. Credits charged: **not measured** (read the balance).

## Runs

| # | Date | Way | Zenvi calls | Images shown | Model tokens (in / cache read / cache written / out) | Zenvi credits | Cuts found | Quality |
|---|------|-----|-------------|--------------|-------------------------------------------------------|---------------|-----------|---------|
| 1 | 2026-10-16 | Frames only: 3x `view_frames_tool` (all failed), 1 overview + 6 windows of `inspect_media_tool` | 10 (7 worked) | 73 | 10 / 138,477 / 50,458 / 5,413 | 0 | about 23 scenes, times good to +-1.25 s | Good story, right characters and palette. Missed the fish shots at 7.4 to 7.9 s (placed fish only at the end), most sub-second cuts, no audio, no beat timing, some garbled words. Not enough to recreate. |
| 2 | 2026-10-16 | Our tool by hand (not an agent run): `storyboard_tool`, 2 pages | 2 | 2 sheets (48 + 37 keyframes) | not an agent run: notes are 24,637 characters (about 6,200 tokens, estimated at 4 characters a token) | 0 for the tool itself (reads the shelf) | 85 shots, exact start and end of each | Every cut, camera move, shot type, look, and a description per shot. Wall time 1.8 s and 1.9 s a page. |
| 2b | 2026-10-16 | Our tool by hand: `storyboard_tool detail=list` (all 85 shots in one call, plus 2 keyframe sheets) | 1 | 2 sheets | not an agent run: notes are 10,700 characters (about 2,700 tokens at 4 characters a token, estimated) against 24,637 for the full mode in 2 pages | 0 | 85 shots, one row each with its own start, end and length | Same cuts as the full mode in 43% of the text and one call. |
| 3 | 2026-10-16 | Claude Code, same prompt, after the changes (session abbe3696): `storyboard_tool` page 1 (48 shots) and page 2 (37 shots), tile_width 480 | 2 (0 failed) | 0 (it worked from the text; the keyframe sheets were not opened) | 8 / 114,021 / 35,493 / 3,843 | not measured (needs the account balance before and after) | all 85 shots covered; 37 stated cut times each within about 0.05 s of the structure layer | Much better than run 1 (checked by the watcher against structure.json and watch.json, not by hand). About 20 shots of 0.3 to 0.5 s were grouped into entries without individual times, and a few descriptions inherit the auto-tagging's mistakes (it flagged a 'weapon-like prop' as possibly wrong). No invented scenes found. |
| 4 | 2026-10-16 | Same Claude Code session as run 3, asked again; no new Zenvi call: it re-used run 3's storyboard text and opened the 2 keyframe sheets with Read (so the new inline pictures, transition names and beat line were NOT tested) | 0 (2 file reads) | 2 | 4 / 98,641 / 13,694 / 6,461 (this turn only) | not measured | same 85 shots: 69 times stated, 55 within 0.06 s, 55 of 85 shots with own start | Looking at the pictures corrected the text: hair is purple or dark navy, not pink; the 'weapon-like prop' and 'falling panel' were a studio camera, tripod and floor shadow; the 'package with red hearts' was a beanie; 'crying, bleeding, collapsing' was overstated (blood on two or three faces, one crying boy); the 'city skyline through blinds' shot was not in the frames. Mentions the 136 BPM and the silence; does not mention fades or dissolves (it never saw that data). |

## What run 1 got wrong, and why
- The MCP server's own instructions told the agent to start with `inspect_media_tool`; nothing pointed at the index.
- `view_frames_tool` failed on this file: ffprobe printed the Japanese title tag and the app read it as ASCII (see the fix below).
  The same bug stopped the file from being indexed at import, so there was no shot list for the agent to find anyway.

## Fixes made because of it
1. `probe_media` and `measure_loudness` decode ffmpeg/ffprobe output as UTF-8 (regression test `tests/test_media_index_unicode_metadata.py`);
   the frame tools now say why a file is unreadable.
2. `storyboard_tool`: every shot, exact cuts, one stamped keyframe per shot, paginated, cached.
3. The MCP instructions, the shared playbook, and `inspect_media_tool` (its description and a note in its result when the file has a
   shot list) now start from the shot list.

## How accurate run 3's answer was (measured against the index)
- The answer states 66 distinct times. 56 are within 0.06 s of a real cut and 62 within 0.15 s. The rest are section headings rounded to
  a whole second (0:56, 1:13, 1:24) and the start of the silence (1:28.7), which is not a cut.
- 55 of the 85 shots have their own start time stated; 11 of the 19 shots shorter than half a second do. The rest are grouped into ranges.
- Spot check of a descriptions stretch against the frames (shots 3 to 6, 5.5 to 7.2 s): the text matches the picture. "Pink hair" is the
  side-light on lilac hair, not a mistake.
- The song's vocals are not transcribed (the speech layer has 0 words for this file), so no lyrics.
- The agent never opened the keyframe pictures: the tool gives them as a file path, not as an image in the result.

## Timing accuracy, same method for both final answers
Every m:ss time in the answer, compared with the nearest real cut in the index.
| | run 1 (frames) | run 3 (index) |
|---|---|---|
| times stated | 76 | 66 |
| within 0.06 s of a cut | 10 | 56 |
| within 0.5 s | 58 | 65 |
| largest error | 2.20 s | 1.37 s (the start of the silence, not a cut) |
| shots whose own start time is stated | 10 of 85 | 55 of 85 |
| mentions tempo / silence / snow / blood shots | no / no / no / no | yes / yes / yes / yes |

## Added after run 3
`storyboard_tool` now returns the keyframe sheets as pictures in the result, names fades and dissolves on the rows, and adds a line
counting how shots join and how many cuts land on a beat. For this file: 77 hard cuts, 5 fades, 2 dissolves; 16 of 84 cuts land on a
beat at 136 BPM (about 19%, below the 27% expected by chance), so this ending's cuts are not locked to the beat. Run 4 (Claude Code
with these changes) is still to be measured.

## Round after run 4: what was added, and what did not work
Added (all local, no credits): lyrics, camera speeds on the rows, `[unsure]` on hedging descriptions, `[no description]` on shots the
description pass left out, a header with the cut rhythm, the song's sections and far-apart look-alike shots, and an opt-in `look` column.
- **Lyrics.** The speech layer had 0 words for this file because Apple's engine listens in the machine's language. With locale ja-JP the
  same engine returns 243 characters in about 2 s (20 s of audio took 1.6 s). The language is taken from the script of the file name (kana
  means Japanese), tried once, and kept on the shelf; `get_lyrics_tool` does it on demand for any locale. It is a machine transcription of
  singing: parts read as real Japanese, other stretches are garbled, and there is no reference text here to score it against.
- **Why not try every language.** Apple allows 5 locales at once, and a new language took about 15 s on first use (the system fetching
  its model), so a blind scan per file would be slow and download models.
- **Tested and not shipped: description-against-picture agreement.** The description and the keyframe of a shot are both stored as vectors
  in one space, and matched pairs score 0.43 against 0.30 for mismatched ones. But the five known description errors ranked 9th to 70th
  of 84 by that score, so it cannot find them. The `[unsure]` mark (the model's own hedging) caught 1 of those 5 plus 3 shots that really are unclear.
- **Repeats.** The two fish at the start and the end score 0.86 and 0.87. Pairs that are neighbours score higher (a scene resembles itself),
  so only pairs at least 15% of the video apart are listed; 10 pairs, about 80 tokens.
- **A second bug of the same family as the Japanese title.** The speech helper's output was also read with the machine's default encoding,
  so the lyrics failed inside the app with an 'ascii' error while working in a terminal. Every place that reads ffmpeg, ffprobe or the
  helper as text now reads UTF-8.

## Run 1 vs run 3
| | run 1 (frames) | run 3 (index) | change |
|---|---|---|---|
| Zenvi calls | 10 (3 failed) | 2 (0 failed) | -8 |
| images shown | 73 | 0 | -73 |
| cache read tokens | 138,477 | 114,021 | -18% |
| cache written tokens | 50,458 | 35,493 | -30% |
| output tokens | 5,413 | 3,843 | -29% |
| cuts found / timing | about 23, +-1.25 s | all 85, about 0.05 s | |
Zenvi credits for the one-time description pass are not measured. They are spent once per file and not again for any later run.

## Still to do
- A single call (or a compact text-only list) for a whole file, so the agent does not page by hand; per-shot times for runs of very short shots.
- Read the account balance before and after indexing to put a credit figure on the description pass.
