"""The editing playbook: how to finish a whole piece (a vlog, a short, a film), not just run one edit.

One text, one source of truth. It is handed to every agent that edits in Zenvi:
- Claude Code and other MCP agents get it in the server's instructions (``agent_mcp_server``);
- the Zenvi Assistant gets it through the tool manifest (``scripts/export_editor_tool_manifest.py`` writes it, the backend's
  prompts include it), so both read exactly the same words.

It is guidance for judgement, not a script. The audit tool (``review_edit_tool``) measures; the agent decides.
Tool names here are exact; ``tests/test_edit_playbook.py`` checks every one of them is a registered tool.
"""

from __future__ import annotations

import hashlib

PLAYBOOK_VERSION = 1

PLAYBOOK = """EDITING PLAYBOOK: how to finish a piece. It is guidance, not a script: use judgement, reorder steps, and skip one only for a reason you can state.

SCOPE
- A narrow request ("make it warmer", "cut this clip", "add captions") gets only what was asked. Do not overshoot.
- A finished piece ("make a vlog / reel / short / trailer / ad / montage / recap / scene / film", "edit my footage into...", "with this vibe") has six layers, all on by default: STORY (what is used, order, pacing, the opening), PICTURE (consistency, exposure, grade), SOUND (voice, music, ambience, mix), TEXT (titles, captions), GRAPHICS/EFFECTS (only as the form needs), DELIVERY (format, length, loudness, export). Drop a layer only because the user banned it, review_edit_tool shows it is already fine, or it does not suit the form, and say so in your report.

THE LOOP (the audit is the one step not to skip before exporting a finished piece)
1. Brief: form, length, vibe or reference, bans, delivery. Save it with set_edit_brief_tool so a long job survives a long conversation; read get_edit_brief_tool when picking one up.
2. Survey: get_project_overview_tool shows what you have to cut with (amounts, days and places, best moments, music, look clusters, what is not indexed yet: wait_until_project_indexed_tool). Then search_footage_tool (usable_only, min_highlight, sort, dates, places) and get_segment_dossier_tool for detail. Missing footage: stock or generate it, and note that you did.
3. Plan: a beat map (acts, scenes, shots with intent), the music (analyze_music_tool; compare candidates with audition_music_tool; place the winner with stock_music(sound_id=..., preview_url=..., start_seconds=<a phrase point>) so it starts and ends on a phrase), the look target, the text. Keep the plan in the brief.
4. Assemble with the placement tools. When the edit is cut to music, sync_cuts_to_beats_tool puts the cuts on the beat.
5. Finish each layer. Picture: harmonize_look_tool for consistency, then a look (apply_look_tool, apply_color_tool); never clip_edit for grading. Sound: balance_mix_tool (voices even, music ducked, loudness set), remove_silence_tool, fades last. Text: add_captions_tool for speech on social forms, titles.
6. Audit: review_edit_tool (measured), look at frames (inspect_timeline_tool), and listen_tool for a second ear. Fix what 'needs' work and run it again.
7. Export with the platform preset. Report what you did, what you skipped and why, and anything still open.

WHEN EACH LAYER IS NEEDED (the audit measures; you decide)
- Colour: at least a consistency pass on any multi-clip edit; a style when a vibe is asked for or implied ("set a scene", "cinematic"); required for HDR footage.
- Voice: any speech gets level and clarity checks: voices even, music at least 10 dB under them.
- Music: on by default for a vlog, reel or short, montage, trailer, ad, scene or film; off by default for podcasts, interviews and tutorials unless a mood is asked for.
- Mix: always check loudness (online video about -14 LUFS, podcast -16, film -23, peaks under -1 dB) and dead air.
- Captions: speech in a vertical social form, or when asked.
- Cuts: on the beat for music-driven forms, varied shot lengths, the strongest shot first.

TRUST
- Measured facts (levels, cuts, camera moves, loudness, colour numbers) outrank model opinions (interest, mood, listen_tool findings). A measured defect (blurry, shaky, black) is never outvoted by a model's enthusiasm.
- listen_tool hears gross faults only (buried speech, clipping, noise, dead air); trust review_edit_tool for levels.

PRECISION AND ZOOM (look closer only when it changes the edit)
- Numbers first: the dossier, search results, analyze_music_tool and review_edit_tool answer most questions. To judge a found moment use get_moment_tool (one range: frames, words, quality, and what is above, below and around it on the timeline). view_frames_tool zooms: about 1 frame a second to find a moment, 5 to 10 a second or every_frame to choose a cut frame. view_audio_tool only where analyze_music_tool's spectrogramSuggested flags an uncertain section edge, or to check a finished mix (timeline=true).
- Cuts: refine_cut_tool for the exact frame of a cut; get_voice_edges_tool for where a voice really starts and stops (cut a little after it); find_retakes_tool to pick the best take; get_clip_context_tool before changing a clip (what is under, above and around it, and what would move).
- Vertical or other shapes: get_framing_tool then reframe_to_subject_tool (it keeps the subject in shot); check_media_health_tool for mixed frame rates, rotation and HDR.
- Blur, highlight or cut out something: locate_in_footage_tool with for_action, then enhance_file_with_comfyui_tool with the handoff it returns.
- People, only if the user turned recognition on: who_is_this_tool; search_footage_tool person=; locate_person_tool for edits to one person (change their clothes, replace them). Names come only from the user (name_person_tool); say "unsure" where a match is unsure, never guess.

LONG-FORM (a film)
- Work in acts and scenes. Give each scene a sub-brief and keep scene cards in the brief; finish and audit one scene at a time, then audit the whole for continuity (look across scenes, loudness range, recurring music).
- Ask the user only before expensive steps (generation, long renders). Otherwise keep going."""

PLAYBOOK_SHA256 = hashlib.sha256(PLAYBOOK.encode("utf-8")).hexdigest()


def playbook_manifest() -> dict:
    """What the tool manifest carries, so another repo can include exactly these words."""
    return {"version": PLAYBOOK_VERSION, "sha256": PLAYBOOK_SHA256, "text": PLAYBOOK}
