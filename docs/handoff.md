# Zenvi Handoff (desktop side)

Zenvi exchanges projects with After Effects, Premiere Pro, Remotion and
HyperFrames, and keeps **linked clips**: rendered compositions that remember
their source so they can be re-rendered, edited and opened from Zenvi
("Adobe Dynamic Link, but for code"). This page covers the desktop
(zenvi-core) side. The Adobe extension (Zenvi Link) and the `zenvi` CLI/MCP
live in zenvi-web.

```
 File menu ── Export Project / Import Project / Send To ──┐     Zenvi Assistant / MCP
 Clip & Project Files menus ── Linked Source ─────────────┤     (editor tools)
 Toolbar pill ── render progress, "N linked clips changed"┤            │
                                                          ▼            ▼
                    classes.handoff  (core, mostly pure Python, no Qt)
   timeline_view · keyframes · transform ─ read the project for exporters
   linked_media ─ zenvi_link schema, providers, add/swap/re-render/unlink (1 undo step each)
   jobs ─ handoff executor + running-job registry      alpha ─ never-WebM rule (ffmpeg)
   node_runtime ─ find Node, run npm/npx               open_source ─ file:line in an editor
   adobe_link ─ Zenvi Link discovery + MCP client      plugins / ui_registry ─ packages plug in
        │                  │                    │
        ▼                  ▼                    ▼
   node + Remotion / HyperFrames CLIs     After Effects / Premiere Pro (Zenvi Link panel)
```

## Packages and where they plug in

| Package | Docs | Core module | Editor tools | Menu entries |
| --- | --- | --- | --- | --- |
| Shared (this page) | — | `classes/handoff/*` | `editor_tools/handoff.py` | Linked Source submenu, Send To, toolbar pill |
| After Effects export | [handoff-after-effects.md](handoff-after-effects.md) | `classes/handoff/after_effects.py` (+ `after_effects_export.py`) | `editor_tools/handoff_after_effects.py` | Export Project, Send To |
| Premiere export/import | [handoff-premiere.md](handoff-premiere.md) | `classes/handoff/premiere.py` | `editor_tools/handoff_premiere.py` | Export / Import Project, Send To |
| Remotion | [handoff-remotion.md](handoff-remotion.md) | `classes/handoff/remotion/` | `editor_tools/handoff_remotion.py` | Export / Import Project |
| HyperFrames | [handoff-hyperframes.md](handoff-hyperframes.md) | `classes/handoff/hyperframes/` | `editor_tools/handoff_hyperframes.py` | Export / Import Project |
| AE linked clips | (this page) | `classes/handoff/aftereffects_link.py` | (shared tools) | (Linked Source) |

Each package documents its own handoff (what it maps, its limits, how to test it)
in `docs/handoff-<package>.md`; this page covers the shared core they build on.

`classes.handoff.plugins.load_plugins()` imports each package module that is
present in the build (at startup, from `install_handoff_menus`); a package
registers its link provider (`linked_media.register_provider`) and menu
entries (`ui_registry.register_export_action` / `register_import_action` /
`register_send_action`) when imported. `editor_tools/handoff.py` loads the
packages' tool modules the same way. Nobody edits a shared list.

## Reading the project: `TimelineSnapshot`

`TimelineSnapshot.from_project(project_dict, project_path)` (or `from_app()`
on the GUI thread) is an immutable deep copy exporters read from any thread:
fps as a `Fraction`, tracks bottom → top, clips by position, files with
absolute paths, the speed a clip's `time` curve implies (normal / constant
xN / reversed / freeze / variable), effects, transitions (Mask brightness
and contrast curves), markers.

Keyframes are `Curve` objects evaluated exactly like libopenshot 1.0
(golden values from the web engine's parity-tested port):

* X is a 1-based **clip frame that includes the trimmed start** — the first
  visible frame of a clip with `start = 2 s` at 30 fps is X = 61. `Curve`
  points carry `time` (timeline seconds) and `local` (seconds since the
  clip's first visible frame), so exporters never do this math.
* A segment is drawn with its **right** point's interpolation (bezier,
  linear, constant/hold); a bezier segment's control points are the left
  point's `handle_right` and the right point's `handle_left` — exactly the
  CSS `cubic-bezier(x1, y1, x2, y2)` of that segment.

`transform.geometry()` / `clip_geometry()` port libopenshot 1.0's
`Clip::get_transform`: layout box minus margin, scale mode (FIT / CROP /
STRETCH / NONE, integer `QSize::scaled`), gravity, location (CROP moves by
`location × (anchored + size)` / `location × (canvas − anchored)`), rotation
and shear about the origin point. The result gives the centre, size, scale
per source pixel, anchor (origin on the canvas — an Adobe layer's Position)
and the full matrix.

## Linked clips

A linked clip is an ordinary project file whose `path` is the rendered
movie, plus `zenvi_link` on the file record (all clips of the file share it):

```json
"zenvi_link": {
  "version": 1,
  "kind": "remotion",
  "source": {"project_dir": "/abs/my-video", "entry": "src/index.ts", "composition": "Intro",
             "composition_key": null, "file": "src/Intro.tsx", "line": 12, "aep": null},
  "props": {"title": "Hello"},
  "render": {"codec": "prores4444", "width": 1920, "height": 1080, "fps": {"num": 30, "den": 1},
             "duration_frames": 150, "output": "@assets/links/remotion/Intro-3f9a1c2b.mov",
             "rendered_at": "2026-10-04T21:00:00Z", "fingerprint": "sha256:..."},
  "state": "fresh",
  "error": null
}
```

* **Never** use the keys `path`, `image`, `resource`, `protobuf_data_path` or
  `lut_path` inside `zenvi_link`: the project file rewrites every JSON string
  under those names as a media path on save/load. `normalize_link` refuses
  them; props that contain them are stored JSON-encoded
  (`{"$zenvi_json": "..."}`) — read props through `read_link()` /
  `link_props()`. Unknown keys are kept.
* Renders live in `<project>_assets/links/<kind>/<composition>-<hash>.<ext>`.
  An unsaved project renders into `~/.openshot_qt/links/<kind>/`; the first
  save moves those into the assets folder (Save As copies, so the old project
  keeps its renders). Providers render into a hidden staging folder next to
  the target; the finished file is moved in, so a failed render leaves
  nothing behind.
* **Freshness:** `fresh` (rendered from the current source), `stale` (the
  provider's fingerprint of sources + props + render settings changed, or the
  render is missing), `rendering`, `error` (the last render failed this
  session; the project is untouched), `missing_source` (project folder or
  `.aep` gone). Zenvi re-checks off the GUI thread when it becomes the active
  app and every 30 s while the project has linked files; a pill on the main
  toolbar then says **N linked clips changed — Re-render** (the themes hide
  the status bar, so render progress with **Cancel** and short results show
  in the same pill).
* **Undo:** adding a linked clip (file + clip), re-rendering (the media swap
  for every clip of the file), editing props and unlinking are each one undo
  step. Re-rendering several stale clips from the toolbar pill is one step.
  A refused, failed or cancelled operation adds nothing.

### The alpha rule

libopenshot 1.0 decodes VP9 WebM with its native decoder, which drops alpha
(a 50 % red VP9-alpha frame decodes with α = 255), but keeps alpha for ProRes
4444 (`yuva444p10le`) and QuickTime Animation (qtrle, `argb`). So linked
media is **never WebM**: codec `auto` renders one still first and picks
ProRes 4444 when any pixel is not fully opaque, else H.264 (CRF ≤ 18,
yuv420p, BT.709); a WebM from another tool is re-encoded to ProRes 4444
before it is linked (`handoff.alpha`).

### Colour: linked media is treated like any other media

Zenvi does **no colour conversion** on linked renders — a Remotion, HyperFrames or
After Effects render goes through exactly the same path as any imported video. This
is deliberate (measured 2026-10-05):

* libopenshot 1.0 decodes every YUV source with the BT.601 matrix, whatever the
  file is tagged with. A BT.709-tagged 720p test pattern compares at 26.7 dB PSNR
  between Zenvi's preview and an ffmpeg reference decode; the same source
  pre-converted to BT.601 compares at 32.8 dB. So BT.709 footage — camera files
  and linked renders alike — looks slightly shifted in the **preview**.
* libopenshot's exports re-encode with the same BT.601 matrix and write untagged
  yuv420p, so regular BT.709 footage comes out with its original YUV values and
  plays correctly in players that treat untagged HD as BT.709.
* Converting a linked render to BT.601 for Zenvi would therefore make it look right
  in the preview but play shifted in every final export.

The preview shift is a pre-existing libopenshot issue (its YUV→RGB conversion
ignores the colour tags) and should be fixed there, once, for all media — not worked
around per provider. Providers render with their tool's normal BT.709 settings
(`handoff.alpha.H264_ARGS` tags H.264 as BT.709).

### Providers

```python
class LinkProvider(Protocol):
    kind: str; label: str; supports_studio: bool
    def fingerprint(self, link) -> str             # "sha256:..."; SourceMissing when the source is gone
    def render(self, link, out_dir, *, on_progress, should_cancel) -> RenderResult  # ONE file in out_dir
    def open_source(self, link) -> None            # code editor at file:line
    def open_studio(self, link) -> None            # Remotion Studio / HyperFrames preview (optional)
    def editable_props(self, link) -> dict         # {name: current value}
```

All provider methods block and run off the GUI thread.
`linked_media.fingerprint_sources(root, include=..., extra=...)` hashes a
source tree (content for files ≤ 2 MB, size + mtime above; skips
`node_modules`, `.git`, build output). The After Effects provider
fingerprints the saved `.aep`, renders through Zenvi Link's
`ae_render_for_zenvi`, and opens the `.aep`.

### Opening the source

Linked Source > Open Code opens the source file at its line. Preferences >
General > *Code Editor for Linked Clips* (`handoff-code-editor`): `auto`
(Cursor, then VS Code — their command-line launchers on PATH or inside the
app bundle / install folder — then the system app), `cursor`, `vscode`,
`system`, or a command template such as `subl {file}:{line}` (`{file}`,
`{line}`, `{folder}`).

## Node.js

Zenvi does not ship Node. `node_runtime.find_node(min_major)` searches
`ZENVI_NODE`, Zenvi's private copy (`~/.openshot_qt/hyperframes/node`, from
the local HyperFrames setup), every `node` on PATH, then the folders a GUI
app's PATH misses (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.volta/bin`,
the newest `~/.nvm/versions/node/*/bin`, fnm's default alias,
`%APPDATA%\npm`, `%ProgramFiles%\nodejs`). npm and npx run through their JS
entry next to node when present (no `.cmd` shims, no shell on Windows).
`run_node` streams output lines, kills the whole process tree on cancel or
timeout and never opens a console window.

## Adobe hosts (Zenvi Link)

The Zenvi Link CEP extension serves a loopback MCP endpoint inside After
Effects and Premiere Pro and writes `~/.openshot_qt/link/aftereffects.json`
/ `premiere.json` (`url`, `token_file`, `pid`, `project`, `app_version`,
`last_active_at`, ...). The desktop trusts a file only when its pid is alive,
its URL is loopback and one `initialize` with the bearer token answers within
1.5 s; it never probes other ports and never deletes another process's file.
Calls are JSON-RPC POSTs (`initialize`, `tools/list`, `tools/call`) with
`Authorization: Bearer <token>`; replies are JSON (a `text/event-stream`
reply is read up to its first event). A tool's result is a contract-3
receipt (`status`, `summary`, `data`, `warnings`, `undoSteps`, `error`) plus
PNG frame captures. The active host is the connected one with the most
recent `last_active_at`.

File > Send To lists the packages' send entries; each is enabled only while
its host is connected (discovery is re-read off the GUI thread when the menu
opens) and says how to connect otherwise.

## Editor tools (shared)

| Tool | Arguments | What it does |
| --- | --- | --- |
| `list_link_hosts_tool` | `include_tools`? (false), `tools`? (names) | Adobe hosts: connected, active, version, project, how to connect. `include_tools=true`: each connected host's tools as `{name, title}` (both apps about 4 KB). `tools=[names]`: those tools in full (`name, title, description, inputSchema, annotations`); names a host lacks come back in its `unknown_tools`, and entries beyond about 32 KB per reply in `deferred_tools` (ask again), because the Assistant's harness keeps only 51,200 bytes of a tool result. A host whose tool list fails reports `tools_error`. Catalogs are fetched once per host session. Before `call_link_host_tool`, the Assistant calls it once with `include_tools=true`, then with `tools=[...]` for the tools it will call. Read-only. |
| `call_link_host_tool` | `host` (`aftereffects`/`premiere`), `tool`, `arguments` | Runs an `ae_*` / `premiere_*` tool in the connected app; returns its receipt and frame images. Changes the Adobe app only. |
| `import_linked_media_tool` | `path`, `link`, `position`?, `track`?, `name`? | Adds a render with its link as one undo step (AE → Zenvi uses it). The file is moved from the temp folder or copied into the links folder; WebM is re-encoded to ProRes 4444. |
| `get_linked_clip_tool` | `clip_id` or `file_id` | Link, props, render settings, freshness, editable props, clips. Read-only. |
| `update_linked_clip_tool` | `clip_id`/`file_id`, `props`?, `rerender`? (true) | Merges props and re-renders (one undo step), or only stores them (stale). |
| `rerender_linked_clip_tool` | `clip_id`/`file_id` | Re-renders from source; one undo step; a failure changes nothing. |
| `open_linked_source_tool` | `clip_id`/`file_id`, `target`? (`code`/`studio`) | Opens the source in the code editor or the studio. |
| `unlink_clip_tool` | `clip_id`/`file_id` | Drops the link, keeps the media; one undo step. |

Renders block the tool call on its worker thread (like
`add_animated_title_tool`); while they run, a job keyed by the file id makes
`get_linked_clip_tool` report `rendering` and the toolbar pill show progress
with Cancel. The packages add `export_to_after_effects_tool`,
`send_to_after_effects_tool`, `export_to_premiere_tool`,
`send_to_premiere_tool`, `import_timeline_xml_tool`,
`list_remotion_compositions_tool`, `import_remotion_project_tool`,
`export_to_remotion_tool`, `import_hyperframes_project_tool` and
`export_to_hyperframes_tool`.

## Tests

* Headless: `.venv/bin/python -m pytest tests/test_handoff_*.py tests/test_editor_tools_handoff.py -q`
  (`tests/handoff_fakes.py`: the `linked` fixture, `FakeProvider`,
  `FakeProbe`, `FakeHost` — a loopback Zenvi Link host on port 0 — and
  `write_discovery`).
* Real Qt (menus, toolbar pill, props dialog):
  `QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python .venv/bin/python -m pytest tests/test_handoff_ui_qt.py -q`
