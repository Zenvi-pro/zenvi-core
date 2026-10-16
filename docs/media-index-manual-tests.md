# Media Index v2: manual test script

Run these by hand in the app with the Assistant (or Claude Code connected to the editor). The prompts are written the way a person
types, not the way a test plan reads, so the AI has to work out which tools to use. The right-hand columns are for the tester, not
for the AI.

**Setup (once).** Start the backend from the matching branch, then `ZENVI_BACKEND_URL=http://localhost:8500 ./run.sh` and sign in with an
account that has indexing allowance. Import: one clip of someone talking (a few seconds of silence before they start), one song, three or
four ordinary footage clips (a phone clip or two), and a short video whose audio is not in your Mac's language. Wait until the Index
dock shows every file finished. **Use a brand-new Claude Code session for each test**; a resumed session reuses what it already read.

**How to read the results.** Each session log shows the tool calls. `python scripts/claude_session_stats.py <session>.jsonl` totals the
calls, pictures and tokens. Credits: note the balance before and after.

## One feature at a time

| # | What you type | What the AI should reach for | Pass |
|---|---|---|---|
| 1 | "hey, which of my clips are actually ready to search? anything still processing or missing?" | `index_status` | Every file listed with what is ready; nothing invented |
| 2 | "somewhere in the talking clip someone says \<a word you know is in it\>. where is it?" | `search_footage`, `get_transcript` | Right clip, time within about 0.3 s |
| 3 | "do I have any shot with \<something you can see in one clip\> in it?" | `search_footage` | The right clip with in and out times. If no descriptions exist it says so, not a random clip |
| 4 | "trim the talking clip so it starts right when she begins speaking, not before" | `get_voice_edges`, `slice_clips` | The cut is within about 0.05 s of where the voice starts; one undo puts it back |
| 5 | "cut that song clip up so every cut lands on the beat" | `slice_on_beats` | Pieces run from beat to beat; one undo restores it |
| 6 | "what happens in the Japanese video, shot by shot? exact timings please" | `storyboard` | One row per shot with its own time; the keyframe pictures come back; fades and dissolves are named |
| 7 | "that video has a song in it. line up the sung words with the scenes, but don't write the lyrics out for me" | `get_lyrics`, `storyboard` | Lines tied to shots by time; no lyrics pasted into the chat |
| 8 | "anything wrong with my clips I should know about before I start? blurry, shaky, weird frame rate?" | `check_media_health` | Specific findings per clip |
| 9 | "I need this as a vertical 9:16 for Reels but keep the person in the middle" | `get_framing`, `reframe_to_subject` | Subject stays centred across the clip |
| 10 | "my footage looks all over the place, can you make it feel like one video? and then make it look more like the Japanese one" | `harmonize_look`, `get_edit_style`, `match_color_to_reference` | Clips that strayed are pulled together; a visible shift toward the reference; one undo |
| 11 | "the music is drowning the talking, fix the mix and tell me how loud it ends up" | `analyze_timeline_audio`, `duck_under_speech`, `balance_mix` | Music sits under speech; the final loudness is given as a number |
| 12 | "I did a few takes of the same line, find them and keep the cleanest" | `find_retakes` | Names the takes and says why one is best |
| 13 | "look over the whole timeline and tell me honestly what still needs work" | `review_edit` | A list with measured numbers, not opinions |
| 14 | "export it as a 720p mp4" | `export_video` | The file plays, picture and sound in sync |

## Several tools together

| # | What you type | What the AI should reach for | Pass |
|---|---|---|---|
| 15 | "make me a 30 second vertical clip from my footage with that song, cut to the beat, keep it moody, then tell me if anything's off" | `search_footage`, `slice_on_beats` or `sync_cuts_to_beats`, `reframe_to_subject`, `harmonize_look`, `balance_mix`, `review_edit` | A timeline of about 30 s; cuts on beats; the audit lists real issues; the AI says what it skipped |
| 16 | "I filmed one long take and want it to feel like a music video, can you cut it on the beat of the song and add some punch to the colour?" | `slice_on_beats`, `apply_look` or `color_grade_clip` | Beat cuts, a grade applied, both undoable in separate steps |
| 17 | "clean up this interview: take out the long silences and the ums, then caption it" | `remove_silence`, `remove_words`, `add_captions` | Fewer pauses; captions follow the speech |

## The big one: recreate a video

> "I love how this Japanese video is cut. Could you recreate it with my own clips instead? Same rhythm, same cuts, same song if you can,
> and the same overall look. Use my footage as much as you can and trim it however you need. Where I don't have anything that fits,
> grab stock footage. Tell me at the end what you used where, what you skipped, and why."

- **Should reach for:** `storyboard` (list mode), `get_edit_style`, `search_footage`, `slice_clips`, `add_clips_to_timeline`,
  `import_stock_media`, `harmonize_look`, `balance_mix`, `review_edit`.
- **Pass:** cuts land within a frame of the original's cut times; a plain list of which shots came from your clips, which from stock and
  which were skipped; one undo restores the project; the closing audit has numbers.

If AI generation is on and you are happy to spend credits (note your balance first):

> "For the shots you couldn't fill with my clips or stock, make a short generated clip for each, 3 seconds is fine. Tell me what each one
> will cost before you make it."

- **Pass:** it names a price before spending; the generated clips land on the timeline in the right slots; the balance drops by about
  what it said.

## Things to try on purpose (the awkward ones)

| What you type | What should happen |
|---|---|
| "find the clip with the striped shirt" on an account with no indexing allowance | It says picture search isn't available yet and what still works. It must not return clips anyway |
| "cut the talking clip one frame after it starts" | It refuses the one-frame sliver and points to the voice-edge tool |
| "what are the lyrics in this song?" | It uses the lyrics tool and works with timing; it does not paste the full lyrics |
| "undo everything you just did" after test 15 | The timeline returns to how it started, in as few undo steps as there were requests |
| Import the same clip into a second project and ask for a search | Instant, with no second charge |

## What to write down for each run
The session id, the number of tool calls, pictures shown, model tokens, credits before and after, and one line on what was wrong or
missing. Add a row to `docs/experiments/media_index_vs_frames.md`.
