# Edit contract: what the Zenvi Assistant backend should do

The desktop (this repo) enforces the *edit contract* in the one tool executor that every agent shares, so the rules below
already hold for Claude Code, Codex, ChatGPT, Cursor CLI, OpenCode and Hermes. The hosted Assistant's brain
(`zenvi-backend`: system prompts, planner, model catalog) is **not in this repo**, so nothing here can be built or tested from
core. This is the spec for the backend side. Nothing in it is required for the desktop to work: with an old backend the
desktop gate still protects the timeline, and every field below is optional and ignorable.

## 1. What the desktop already does (you get it for free)

- The desktop starts the contract itself from the user's message (a whole-piece ask on an empty timeline), so the gate does not depend on the model calling anything.
- A project-level contract, stored with the edit brief (`edit_contract`), says `free`, `finished_piece` or `recreate`, and the
  phase (`idle`, `surveying`, `briefed`, `assembling`, `finishing`, `audited`, `done`).
- In a `finished_piece`, `add_clips_to_timeline_tool` / `add_clip_to_timeline_tool` are refused until the project has a brief
  with a form and a target length. The refusal names the fix. Rollout flag `edit_contract` = `off` | `warn` (default) | `enforce`.
- `begin_edit_tool` (core) surveys the footage and writes brief defaults in one call.
- Every chat turn already carries an `[Editor snapshot]` with an `EDIT_STATE:` line: `contract=finished_piece/briefed · brief: form=vlog
  target=210s · index: videos=32 ready=20 analyzed=8 not_indexed=4 · audit: needs=2 rounds=1` (a free edit with no brief gets the
  `index:` part only).
- The WS `user_message` may carry an optional `edit_contract_mode` (`free` | `finished_piece` | `recreate`) when the desktop knows it.
  Treat it as a hint; absent means unknown.

## 2. What the backend should add

1. **Turn preamble for finished pieces.** When the ask is a whole piece (or `edit_contract_mode == "finished_piece"`), the first
   tool call of the turn is `begin_edit_tool`, before any fan-out of parallel tool calls (the desktop runs up to 4 at once).
   Its receipt carries `contract`, `brief`, `survey_digest` and `next_tools`; plan from that instead of calling the overview again.
2. **Plan mode.** The planner's prompt should be: `begin_edit_tool` digest, then at most 2 questions that change the film
   (length, aspect), then a plan whose steps are executable intents (place, grade, captions, review) and whose brief is saved with
   `set_edit_brief_tool`. Allow `begin_edit_tool`, `get_edit_brief_tool` and `set_edit_brief_tool` in `PLANNING_ALLOWLIST`
   (the desktop mirror, `_PLANNING_SAFE_TOOLS` in `windows/ai_chat_ui.py`, already does).
3. **Model bands, not model names.** Route per turn *before* it starts (nothing can swap a model mid-turn):

   | Turn | Balance default |
   |---|---|
   | narrow tweak, survey, audit | cheap band |
   | plan, freestyle assemble with no brief, fix after story needs | strong band |
   | execute a tight plan, finish layers | mid band |

   Heuristics first (contract mode, timeline empty, brief present, open `needs`); learn a classifier later from the outcome log.
   Keep an explicit cheap model in the pool or Balance silently becomes "always frontier".
4. **Escalation is a new turn.** If `review_edit_tool` still names story or pacing needs after a fix turn, the next turn may use
   the next band up with the needs injected.
5. **Do not** run two agents that both place or grade on the same timeline. One parent per turn; parallel *tool calls* are fine.
6. **Do not** rewrite the user's prompt with a model. The structured brief is the enrichment.

## 3. Outcome signals worth logging (for a later router)

Undo within one turn, an immediate re-ask, `review_edit_tool` needs cleared, export reached, thumbs. Log per turn with the chosen
band. The desktop keeps a local-only log of the same events; nothing leaves the machine from core.

## 4. Compatibility

Old backend + new desktop: the gate works; `begin_edit_tool` appears in the tool manifest's `core_tools`; unknown WS fields are
ignored. New backend + old desktop: `begin_edit_tool` does not exist, so the preamble must be skipped when the manifest's
`core_tools` does not list it.

## 5. Checked against the backend code (read-only, 2026-11-17)

What was verified, and what it means for whoever implements the backend side:

- **An extra WS field is safe.** The chat route reads the `user_message` payload field by field; it does not validate the whole payload,
  so `edit_contract_mode` is ignored today. Read it next to `agent_mode` when you want it.
- **The tool list is a checked-in copy.** The backend keeps a manifest file (exported by the desktop's
  `scripts/export_editor_tool_manifest.py`) with the tools, `core_tools` (19 today) and the playbook with its sha. Until it is
  re-exported from a desktop build that has this change, the Assistant does not see `begin_edit_tool` or the new playbook line.
  Nothing breaks: the desktop gate still protects the timeline, and the refusal also names `set_edit_brief_tool` (already in the
  manifest) as a way to unlock placement. The backend's own tests only check the manifest against itself (hash equals text), so a
  re-export is a plain file replacement.
- **Plan mode on the backend blocks the new tool.** Its planning allowlist is a fixed set plus the manifest's read-only tools.
  `get_edit_brief_tool` is read-only so it is included, but `begin_edit_tool` and `set_edit_brief_tool` write (untracked project
  keys, never the timeline) and must be added by name, mirroring the desktop's `_PLANNING_SAFE_TOOLS`.
- **Order of work:** merge the desktop change, re-export the manifest into the backend, add the two names to the allowlist, then
  consider moving the default flag from `warn` to `enforce`.
- **Where to measure:** the separate agent-benchmark repo's vague-asks set has a `looked_first` check (the media index read before
  the first edit). Run it with the contract off and on (never tune on its held-out split); that is the real before/after.
