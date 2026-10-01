# Editor tools for the Zenvi Assistant

Everything a person can do in the editor should be something the Zenvi Assistant can do
by calling a tool. This page is the contract for writing those tools.

## How a tool reaches the assistant

```
chat message ─▶ zenvi-backend (OpenCode harness) ─▶ backend MCP /harness/mcp ─▶ tool stub
      ▲                                                                            │ WebSocket
      └──────────────── result text ◀── execute_tool(name, args) ◀─────────────────┘
                                          (src/classes/tool_handlers.py)
```

* **Desktop:** tools live in `src/classes/editor_tools/<workstream>.py` and register with
  `@editor_tool`. `tool_handlers` merges the registry into `AGENT_TOOL_HANDLERS`, so
  `execute_tool` dispatches them and the in-app MCP server (`agent_mcp_server.py`, used by
  Claude Code and Codex) lists them with their explicit schema and full description.
* **Backend:** `scripts/export_editor_tool_manifest.py` writes the same names, descriptions
  and schemas as JSON. zenvi-backend turns that manifest into its frontend-delegated tool
  stubs, so the two sides cannot drift. Regenerate it whenever a tool is added or changed.

## Writing a tool

```python
from classes.editor_tools._base import (CLIP_TARGET, ToolError, number, enum, obj, ok,
                                        on_main, resolve_clip, ensure_unlocked)
from classes.editor_tools._registry import editor_tool

@editor_tool(
    "set_clip_speed_tool",
    label="Change clip speed",
    schema=obj({**CLIP_TARGET,
                "speed": number("Playback speed factor: 2 = twice as fast, 0.5 = half speed.", 1.0,
                                minimum=0.0625, maximum=16)}),
    covers=("clip.speed",),
)
def set_clip_speed(timeline_clip_id="", clip_query="", track="", speed=1.0):
    """Speed a clip up or slow it down, keeping its audio in sync ... (written for the model)."""
    clip = resolve_clip(timeline_clip_id, clip_query, track)   # raises ToolError
    ensure_unlocked(clip.data["layer"])                          # validate before mutating
    ...mutate through the editor's own code path...
    return ok(f"Clip {clip.id} now plays at {speed}x.", timeline_clip_id=clip.id, speed=speed)
```

Rules (the contract tests in `tests/test_editor_tools_*.py` enforce most of them):

1. **Intent-level.** One tool per thing a user asks for. Variants are enum arguments
   (one `apply_clip_preset_tool` for every Fade/Motion/Layout preset, not twenty tools).
2. **Explicit schema.** `additionalProperties: false`, real types, enums for choices, a
   `description` on every property, defaults matching the handler. The registry coerces
   what models send (`"1.5"`, `"true"`, `'["a"]'`) and refuses unknown arguments.
3. **The docstring is the model's documentation.** Say when to use the tool and when not
   to, what each argument means in user terms, units (timeline seconds unless the name
   says `source_`), and give an example for anything non-obvious.
4. **Validate before mutating.** Raise `ToolError` (returned as `Error: ...`). A refused or
   no-op call must add nothing to undo history.
5. **One call, one undo step.** `execute_tool` groups the whole call. Join it with
   `tool_handlers._transaction(app)`; never set `updates.transaction_id = None`.
6. **Reuse the editor's code path.** Call the menu/dialog handler (for example
   `timeline.Fade_Triggered`, `retime.retime_clip`, `color_presets.apply_color_grade_preset`)
   or extract its core so both call it. Do not write a second copy of a domain rule.
7. **Mind the GUI thread.** Mutating tools run on it by default (30 s cap): keep them to
   project edits. Slow work (renders, processing, file writes, downloads) is
   `background_safe=True` and marshals only its Qt/project touches with `on_main`.
   Read-only tools (`read_only=True`) run on the calling worker thread.
8. **Receipts.** Return `ok(summary, **data)`: a one-line summary plus JSON with the ids and
   values the next call needs. Failures always start with `Error:` (the backend, the chat
   UI and the MCP server treat anything else as success).
9. **Names.** End in `_tool`, at most 58 characters, not a backend alias
   (`_registry.RESERVED_NAMES`). Only `delete_from_timeline_tool` deletes timeline clips;
   removing an effect, transition, marker, track lane, file or keyframes uses a tool that
   names that noun and says it never deletes clips.
10. **Coverage.** `covers=` lists the ids from `coverage.CAPABILITIES` the tool fulfils.
    A workstream with any registered tool must cover all of its capabilities or move one to
    `OUT_OF_SCOPE` with a concrete reason.

## Conventions the model relies on

* **Tracks:** UI track 1 is the bottom of the stack. Tools accept a UI track number, a
  track name, a track id (`L3`) or a layer number (`normalize_track_or_layer_arg`).
* **Clips:** target by `timeline_clip_id` (preferred), `clip_query` (a description), or for
  batch tools `timeline_clip_ids` / `scope` = `selected` | `track` | `all`.
* **Time:** timeline seconds. Keyframe X in project data is a 1-based clip-local frame that
  includes the trimmed start (`_base.clip_frame_at`).
* **Keyframe interpolation:** `bezier` (default), `linear`, `constant`.

## Testing

* **Unit tests:** use the `editor` fixture (`tests/editor_tools_harness.py`): the real
  project store and undo machinery, production-shaped clips/files/effects, and
  `editor.call(tool, **args)` through `execute_tool`. Cover success, every refusal, one
  undo step per call (`editor.undo_steps_since_mark()`), undo/redo round trips, and edge
  cases (locked track, empty timeline, trimmed clips, images, audio-only, other fps).
* **Live checks:** run the app, then call tools over the in-app MCP server. The MCP-only
  `capture_editor_screenshot_tool` saves a PNG of the editor or one panel, so you can see
  the result the way a person would. Undo and redo every edit you check.
