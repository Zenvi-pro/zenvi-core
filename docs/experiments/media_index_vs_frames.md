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
| 3 | 2026-10-16 | Claude Code, same prompt, after the changes (session abbe3696): `storyboard_tool` page 1 (48 shots) and page 2 (37 shots), tile_width 480 | 2 (0 failed) | 0 (it worked from the text; the keyframe sheets were not opened) | 8 / 114,021 / 35,493 / 3,843 | not measured (needs the account balance before and after) | all 85 shots covered; 37 stated cut times each within about 0.05 s of the structure layer | Much better than run 1 (checked by the watcher against structure.json and watch.json, not by hand). About 20 shots of 0.3 to 0.5 s were grouped into entries without individual times, and a few descriptions inherit the auto-tagging's mistakes (it flagged a 'weapon-like prop' as possibly wrong). No invented scenes found. |

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
