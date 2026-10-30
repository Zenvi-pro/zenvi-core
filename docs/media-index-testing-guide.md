# Media Index and "recreate a video": how to test it properly

This is the full test plan. `docs/media-index-manual-tests.md` is the short prompt list; this guide says how to set up, what is local
and what is cloud, how to prove each, and how to run the big end-to-end test with a known right answer.

Every test has three parts: **what you do**, **what should happen**, **how to see it** (a log line, a file, a number). A test that cannot
fail is not a test, so each one says what a failure looks like.

## 0. Two kinds of testing

| Kind | Run | Catches |
|---|---|---|
| Automated (no Qt, no libopenshot, no network) | `pip install -r requirements-dev.txt && pytest` and `ruff check src/classes` and `pyright` | logic: planner, checker, tool contracts, undo, batching |
| By hand in the running app | this guide | the real thing: real media, real libopenshot, the cloud, downloads, the preview, the AI agent actually choosing tools |

The automated suite stubs PyQt5 and libopenshot, so it cannot tell you the preview is smooth or that a download works. Do both.

## 1. Setup

1. Backend (for descriptions, picture vectors, stock search, credits): start it from the matching branch; the editor needs
   `ZENVI_BACKEND_URL=http://localhost:8500`. Sign in with an account that has indexing allowance.
2. Editor from source: `ZENVI_DEPS=$HOME/zenvi-deps-1.0 ZENVI_BACKEND_URL=http://localhost:8500 ./run.sh`.
3. Watch the log in a second terminal: `tail -f ~/.openshot_qt/openshot-qt.log`. Useful filters: `grep -E "media_index|speech|whisper|update_batch|proxy|stock"`.
4. Footage. Use **both**:
   - **The synthetic kit** (known answers, see section 5): `python scripts/make_recreation_test_kit.py ~/kit` (needs only ffmpeg, 10 s).
   - **Your own real clips**: one person talking (a few seconds of silence first), one song with sung words, 6-10 ordinary phone clips
     (include at least one HEVC .MOV and one 4K clip), one short video whose speech is not in your computer's language, and a finished
     edited video you like (30-90 s, 15+ cuts) as the reference.
5. A **new** Claude Code session for every test (a resumed one reuses what it already read), and a new Zenvi Assistant chat.
6. To drive the app without an AI (scripted tests, timings): the app writes `~/.openshot_qt/gui_mcp.json` (port) and `mcp_token`.
   ```bash
   T=$(cat ~/.openshot_qt/mcp_token)
   curl -s -X POST http://127.0.0.1:7434/mcp -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"index_status_tool","arguments":{}}}'
   ```

## 2. What runs where (and how to prove it)

| Layer / feature | Where it runs | Needs network | Cost | How to prove it |
|---|---|---|---|---|
| Shots, cuts, fades, motion (structure) | this computer | no | free | Turn Wi-Fi off, import a clip: `index_status_tool` shows structure ready |
| Look (brightness, colour, contrast) | this computer | no | free | same; `get_look_profile_tool` returns numbers offline |
| Audio facts (beats, loudness, silences) | this computer | no | free | offline: `slice_on_beats_tool`, `analyze_timeline_audio_tool` work |
| Speech to text, English-like | this computer (Apple SpeechAnalyzer on macOS, bundled whisper.cpp elsewhere) | no | free | offline: `get_transcript_tool` returns words with times |
| Speech in another language, lyrics | this computer | first use may download a model | free | see "downloads" below; `get_lyrics_tool` |
| Faces / people (opt-in, preference `media-index-people`) | this computer (onnxruntime) | model download once | free | off by default: `get_framing_tool` says it used saliency only; on: it says faces |
| Scene descriptions (watch) and picture vectors | cloud (Gemini via the backend) | **yes** | credits, once per file content | the file shelf shows `watch` and `vectors` ready; a second import of the same file costs nothing |
| Stock search | backend (Pexels key stays on the server) | yes | free to search; credits to download | `search_stock_video_tool` offline says it needs sign-in/network, never an invented result |
| Recreation plan / build / check, compare pictures | this computer (reads the saved index) | no | free | offline: `plan_recreation_tool(stock="off")` works on indexed files |
| Optimized preview copies (proxies) | this computer (ffmpeg via libopenshot) | no | free | `~/.openshot_qt/optimized/` fills up |

**Offline test (5 minutes).** Turn the network off. Expect: structure, look, audio, speech, search over already-saved layers, plan,
build, check, compare, proxies all work. Expect clear refusals (not made-up results) for: descriptions of a new file, stock search,
credits. Turn the network on; the queued cloud jobs should finish by themselves (`index_status_tool`).

**No double charge.** Import a file, wait for indexing, note the credit balance. Remove it from the project, import the **same file
again** (or copy it elsewhere and import the copy). Expect: finished instantly, balance unchanged. Failure: balance drops again.

**Downloads (do each on a machine that has not done it):**
- *Whisper model* (non-Mac, or when Apple speech is unavailable): ask for a transcript of a talking clip. Expect a one-time download to
  `~/.openshot_qt/whisper/` (checked against a pinned SHA-256), then the transcript. Failure: "faster-whisper is not installed" or a
  file that is not checked. Log: `grep whisper`.
- *Language fallback*: a clip whose speech is in another language than the computer's: `get_lyrics_tool(language="ja-JP")` (or the
  right locale). Expect words in that language, kept with the file's index.
- *Stock clip*: `import_stock_media_tool(source="pexels", video_id=...)` lands in `~/.openshot_qt/Downloads/pexels_<id>.mp4`, is added
  to the project and is indexed. Importing the same id again says "already in project".
- *People models* (only after turning the preference on): downloaded once, size and licence in `docs/THIRD_PARTY_MODELS.md`.

## 3. One feature at a time (type these as a person would)

Pass = the thing in the third column, checked against the numbers or the picture, not against the AI's say-so.

| Feature | What you type | What should happen |
|---|---|---|
| Index status | "which of my clips are ready to search? anything stuck?" | every file listed with which layers are ready; nothing invented |
| Reindex | "the second clip looks half-indexed, redo it" | job queued; status moves; no duplicate charge |
| Find by words | "where does she say \<a word you know\>?" | right clip, within 0.3 s |
| Find by picture | "any shot of \<something visible in one clip\>?" | the right clip with in/out; if descriptions are missing it says so |
| Whole file, shot by shot | "walk me through that video shot by shot with exact times" | one row per shot; keyframe pictures come back; fades and dissolves named |
| Words against scenes | "line the sung words up with the scenes, don't write the lyrics out" | lines tied to shots by time; no lyrics pasted |
| Lyrics (other language) | "that song is Japanese; get me the words with times" | words appear; says they are a machine transcription |
| Voice edges | "trim so it starts right as she begins speaking" | cut within 0.05 s of the voice; one undo restores |
| Beats | "cut that song clip so every cut lands on the beat" | pieces run beat to beat; one undo restores |
| Silence / filler | "take out the long silences and the ums" | fewer pauses; undo restores |
| Mix | "music is drowning the talking, fix it and tell me the loudness" | music under speech; a loudness number |
| Health | "anything wrong with my clips? blurry, shaky, odd frame rate?" | specific findings per clip |
| Colour match | "make my clips feel like one video, then like the reference" | strays pulled together; visible shift; one undo |
| Framing | "make it vertical 9:16 and keep the person in the middle" | subject stays centred over the clip |
| Style | "how is the reference cut? pace, transitions, how much movement?" | numbers (average shot, cuts a minute, share of hard cuts) |
| Review | "tell me honestly what still needs work" | list with measured numbers |
| Export | "export a 720p mp4" | file plays, picture and sound in sync |

Credits: note the balance before and after any of these that call the cloud.

## 4. Recreation, piece by piece

Use the kit (section 5) or your own footage. Each prompt targets one tool; the AI should reach for the tool in the second column.

| What you type | Tool | Pass |
|---|---|---|
| "plan a recreation of the reference from my clips, don't build anything yet" | `plan_recreation_tool` | a plan id, a line per shot, "matched / weak / unfilled" counts, most uses of one moment = 1 |
| "which shots can't my footage cover?" | `plan_recreation_tool` | the weak shots are named with a stock search phrase in real-world words (no "anime") |
| "find stock for those and show me the options" | `search_stock_video_tool` | ids, lengths, resolution, a credit price; never an invented clip |
| "bring that one in" | `import_stock_media_tool` | in the bin, indexed |
| "build it" | `build_recreation_tool` | one undo step; clips at the reference's cut times; grades, zooms, transitions reported; the song on its own track |
| "how good is it, honestly?" | `check_recreation_tool` | a score with six-seven areas, failing shots each with a concrete fix |
| "show me the worst shots next to the reference" | `check_recreation_tool` | a picture: reference left, your footage right |
| "fix the worst ones" | `build_recreation_tool(only_shots, overrides)` then check | the score does not go down; only those shots change |
| "do the whole thing and tell me what you used, skipped and why" | all of the above | closing report quotes the scores and names stock, approximated and skipped shots |

Checks that must hold in every recreation (look at the timeline, not the AI's summary):
- No footage moment shown twice **unless the reference itself repeats an image** at that point.
- A shot nothing fits is stock, or left out and reported, never filled with unrelated footage.
- Every cut within one frame of the reference's (the check fails the whole run if one is more than 6 frames off).
- The reference's sound on its own track, the footage's own sound off.
- One undo reverts the whole build.

## 5. The end-to-end test with a known right answer

Real footage cannot tell you whether the match is *correct*, only whether it looks plausible. The kit can.

```bash
python scripts/make_recreation_test_kit.py ~/kit      # reference.mp4 + footage_1..6.mp4 + README.txt (the answer key)
```
- `reference.mp4`: 16.5 s, 14 shots on a 120 bpm click. Eight distinct patterns ("scenes"); six come back later.
- `footage_1..6.mp4`: three scenes each, tinted differently so none is a pixel copy of the reference.
- Scene 5 (reference shot 5, 1.5 s) is in **no** footage clip, on purpose.

Import all seven files, wait for indexing, then type (new session):

> "Recreate reference.mp4 using only footage_1 to footage_6. Same cuts, same song, same look. Don't repeat footage to fill a gap: if my
> clips can't cover a shot, say so. Tell me what you used where, what you skipped and why."

**Pass (measured on this repo's code, stock off):** plan in under a second; build in about 4 s; 13 of 14 shots use footage holding the
same scene (open the comparison picture: same pattern both sides); the repeats appear only at the answer key's repeat shots; **shot 5
is flagged** ("nothing in the footage looks like this shot") and is stock, left out, or at least reported, not silently filled; check
score about 95; timing 100; one undo reverts. **Fail:** shot 5 reported as a good match, a scene repeated somewhere the key does not
allow, a cut off by more than a frame, a second song clip on the Song track after rebuilding.

Then run the **same prompt with stock allowed**. Expect `search_stock_video_tool` for shot 5 with a plain description of the pattern, an
import, and a rebuild of just shot 5.

Then the **real-footage version** with your own reference and clips. Expect the plan to say how much of the reference your footage can
cover (for example "your 8 clips hold 47 s of footage for an 85 s video"), stock found for weak shots, and an honest closing report.
Look at the comparison pictures yourself: the numbers cannot tell you the park looks like a bedroom.

### Harder cases (each should fail gracefully, not crash or invent)
1. **Not enough footage:** one 4 s clip for a 60 s reference. Expect most shots unfilled and reported; no repeats.
2. **Reference with a long dissolve and a fade to black:** expect the dissolve and the fade at the same cuts; timing still passes.
3. **Reference not indexed yet:** expect "has no saved index yet" and a pointer to `index_status_tool`.
4. **Rebuild twice:** the Recreation track is replaced, not stacked; one Song clip only.
5. **Cancel midway / failing build:** kill a placement (disconnect disk) or use a source file that is shorter than its slot: nothing half-placed is left, the old clips are restored.
6. **Old attempts under the new one:** the build names the other tracks that are still being drawn.

### Same prompt in both agents
Run the identical prompt in **Claude Code** (connected to the editor) and in the **Zenvi Assistant**, new session each. Record for each: the
tool calls in order (did it plan, build, check, compare, fix?), pictures it looked at, wall time, tokens
(`python scripts/claude_session_stats.py <session>.jsonl`), credits, the final check score, and whether its closing report matches what the
timeline really holds. Add the row to `docs/experiments/media_index_vs_frames.md`.

## 6. Does it play smoothly? (performance)

1. Build the recreation, save the project, then:
   ```bash
   ZENVI_DEPS=$HOME/zenvi-deps-1.0 python scripts/bench_playback.py ~/project.zvn --seconds 30 --layers <Recreation,Song layer numbers>
   ```
   It renders frames in order like the player and prints mean / 95th / 99th / worst frame time and the share over 33 ms.
2. Compare with optimized copies: `manage_optimized_previews_tool(action="optimize", all_videos=true, wait_seconds=600)`, save again, add `--proxies`.
3. Measured on an 80-cut timeline of phone footage (720p project): originals mean 22 ms, 7% of frames over 33 ms, worst 470 ms (the first
   frame after a cut, seeking into long-GOP files); optimized copies mean 13 ms, 2% over, worst 165 ms. With zoom, ramps and transitions
   on, originals reach 16% over and 665 ms; copies stay at 2% and 140 ms. `build_recreation_tool` and large placements now start the copies
   themselves for heavy footage (HEVC or taller than 720p) and say so.
4. Stacked tracks: leave an old 85-clip attempt under the new one and re-run without `--layers`; expect it to be several times slower.
   The build names such tracks.
5. In the app: scrub, play from the start, play through a transition. Judge by eye and by the status bar; note any freeze longer than a
   second. While a build runs, try scrolling the timeline: the window should stay responsive (the build runs off the GUI thread and
   updates the timeline once per batch).

Failure to report: a freeze over a second while idle, frames that stall at every cut after optimizing, a build that takes more than a
minute for 80 shots.

## 7. Safety and undo

- One undo after any single tool call (`undoSteps` in the receipt is 1; a failed or refused call is 0).
- Build, undo, redo: the timeline returns to the same state (undo of a very large build is slow, about 0.4 s a clip: note the time).
- A failed call leaves history untouched.
- Nothing outside the project changes: the Recreation and Song tracks are ours by name; your other tracks are not touched.
- Project file saved and reopened keeps the clips, grades, zooms and transitions.

## 8. What to write down for each run

| Field | Example |
|---|---|
| Date, branch, commit | 2026-10-16, nilay-media-index-v2-pr, 77535db |
| Footage | kit / own (list) |
| Agent | Claude Code / Zenvi Assistant |
| Prompt | exact text |
| Tool calls in order | `storyboard`, `plan_recreation`, ... |
| Check score before / after each fix round | 78.1 -> 87.4 |
| Shots matched / weak / skipped / stock | 24 / 36 / 20 / 25 |
| Repeats | most uses of one moment: 1 |
| Build time, check time | 4 s, 0.4 s |
| Playback: mean / over 33 ms / worst | 13 ms / 2% / 165 ms |
| Credits before / after | 1200 / 1180 |
| Anything the AI claimed that the timeline does not show | none / "said stock used for shot 5, timeline has footage" |
| Undo works | yes |
