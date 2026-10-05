# Remotion ⇄ Zenvi

[Remotion](https://www.remotion.dev) projects open in Zenvi as **linked clips**: each composition is rendered with
the project's own Remotion, and the clip remembers its source, so you can edit its props, open its code, open it in
Remotion Studio and re-render it. In the other direction, any Zenvi timeline exports as a working Remotion project.
Bring that project back and it restores as native, editable Zenvi clips, including the edits made in it.

This page covers the desktop (zenvi-core) side. The shared linked-clip machinery (the Linked Source menu, the props
dialog, freshness, the toolbar pill) is described in [handoff.md](handoff.md).

```
 File > Import Project > Remotion Project...   File > Export Project > Remotion Project...
 import_remotion_project_tool / list_...       export_to_remotion_tool
        │                                             │
        ▼                                             ▼
 classes/handoff/remotion/                      exporter.py + template/   ──►  <folder>/ (Remotion 4.0.532 project)
   detect · sources · importer · provider              │                         src/zenvi/timeline.json
        │                                             │                                │
        ▼                                             │      re-import  ◄──────────────┘
 helper.mjs  ──  the project's own @remotion/bundler + @remotion/renderer   restore.py + edits.py ──► native clips
```

## What you need

- **Node.js 18 or newer.** Zenvi searches the usual places (Homebrew, nvm, Volta, fnm, the PATH); set
  `ZENVI_NODE` to the `node` executable if it lives somewhere else.
- **A Remotion 4 project with its dependencies installed** (`node_modules`). Zenvi never ships Remotion. It runs
  the copy the project installed, so the render matches `npx remotion render` exactly. When `node_modules` is
  missing, the import dialog offers **Install Dependencies**; it runs `npm install --prefer-offline`, or pnpm / yarn
  / bun when the project's lockfile asks for one and that tool is installed.
- **ffmpeg** (Zenvi already needs it) for the transparency check and for QuickTime Animation renders.

## Import a Remotion project

**File > Import Project > Remotion Project...** and pick the folder that holds the project's `package.json`. A
folder inside it, such as `src/`, works too.

1. Zenvi reads the project off the editor's thread: its entry point, then its compositions. To find the entry point
   it checks, in order, `Config.setEntryPoint()` in `remotion.config.*`, a `remotion studio|render <entry>` script
   in `package.json`, and the CLI's defaults (`src/index.ts`, ...). The compositions come from the project's own
   bundle; the first read takes the longest because webpack runs and the headless browser may need downloading.
2. The dialog lists every composition with its size, frame rate, duration and folder. Tick the ones to bring in.
   **Edit Props...** opens the props editor for the selected composition (text, numbers, colours, true/false, and
   JSON for lists and objects). **Render as** picks the codec (see below).
3. **Import** renders each composition in the background; the toolbar pill shows progress and Cancel. All the
   clips land together at the playhead, on the lowest free tracks above the video, with opaque renders below
   transparent ones (a title sits over its background scene). **One Ctrl+Z removes the whole import.** A failed or
   cancelled import adds nothing and deletes its renders.

Each linked clip's media lives in `<project>_assets/links/remotion/` (renders of an unsaved project move there on
the first save).

### Props

A clip stores the composition's full input props, its `defaultProps` merged with what you set, and Zenvi passes
them all to Remotion. Change them with **Linked Source > Edit Props...** or `update_linked_clip_tool`; the
re-render and the media swap are one undo step. Props added in code later keep their default value. Changing a
default in code does **not** change clips that were already imported; edit their props instead.

### Open Code and Open in Studio

- **Linked Source > Open Code** opens the component that renders the composition, at the line that defines it, in
  the editor chosen in Preferences (Cursor, VS Code, the system app, or a command template). Zenvi finds it by
  reading the code: `<Composition id component={X}>`, `lazyComponent`, `<Still>` and `<Folder>`, following named,
  aliased, default and namespace imports and re-exports. When it cannot follow (a dynamic id, an import from a
  package or a path alias), it opens the `<Composition>` line.
- **Linked Source > Open in Studio** starts the project's own Remotion Studio (one per project, on the first free
  port from 3000) and opens `http://localhost:<port>/<compositionId>`. Zenvi stops it when Zenvi quits.
  **Remotion Studio listens on all network interfaces:** Remotion binds `0.0.0.0` / `::` (`getHostToBind` in
  `@remotion/renderer/dist/port-config.js`) and 4.0.532 has no localhost-only option, so other machines on your
  network can reach it while it runs, exactly as with `npx remotion studio`.

### Codecs and transparency

| Render as | What you get | When |
| --- | --- | --- |
| **Automatic** (default) | ProRes 4444 when any pixel of the first, middle or last frame is transparent; otherwise H.264 | almost always |
| ProRes 4444 | `.mov`, `yuva444p10le`, keeps transparency | titles, lower thirds, overlays |
| H.264 | `.mp4`, CRF 18, yuv420p, BT.709; opaque and small | full-frame scenes |
| QuickTime Animation | `.mov`, `argb`, keeps transparency, large files | tools that cannot read ProRes |

Linked clips are never WebM: libopenshot 1.0 drops VP9's alpha. A `<Still>` renders as a 5 s clip of its frame.

### Freshness and re-rendering

Zenvi fingerprints what a render depends on: the project's code and assets (small files by content, big media by
size and date), `package.json` and lockfiles, `remotion.config.*`, `tsconfig.json`, `.env`, the props and the
render settings. When any of these change, the clip reads **stale** in Linked Source and the toolbar pill; **Re-render**
brings it up to date. Bundles are cached in `~/.openshot_qt/cache/remotion/bundles/` (the six most recent are
kept), so re-rendering an unchanged project skips webpack.

## Export a Zenvi project to Remotion

**File > Export Project > Remotion Project...**: choose a folder (Zenvi creates it), whether to copy the media
(works on any computer) or symlink it (this computer only), and whether to run `npm install`. The result is a
Remotion 4.0.532 project:

| Path | What |
| --- | --- |
| `package.json` | Pins remotion, @remotion/cli and @remotion/zod-types 4.0.532, react 19.1.0, and zod 4.5.4 (the version Remotion 4.0.532 itself expects). |
| `src/Root.tsx` | One composition, `ZenviTimeline`, with a zod schema (`background`, `volume`, `showTitles`), so its props are editable in Studio. |
| `src/zenvi/timeline.json` | The readable timeline (tracks; clips with `position` / `start` / `end` in seconds and keyframes `{frame, value, easing}`; transitions; markers) plus, under `zenvi`, the original project and the exported baseline. The renderer reads the same fields the round trip does, so what Remotion shows is what comes back. |
| `src/zenvi/*.tsx`, `*.ts` | The renderer: `<Sequence>`, `<OffthreadVideo>`, `<Img>`, `<Audio>`, `interpolate()` with `Easing.bezier()`, `staticFile()`. `geometry.ts` is Zenvi's clip placement, line for line. |
| `public/zenvi-media/` | The media (copies or symlinks). |
| `README.md` | How to run it, how to bring it back, what is approximated. |

```bash
cd <folder> && npm install
npx remotion studio                                  # preview, edit props
npx remotion render ZenviTimeline out/video.mp4      # render
```

Exporting again into the same folder updates it in place and keeps `node_modules` and any dependencies you added.

### How close is it?

Measured on this Mac (1080p30, 16 frames compared with Zenvi's own render of the same project): **41–50 dB PSNR,
mean difference ≤ 0.7/255** everywhere except title text; whole frames **22–49 dB** while a title is on screen.

- **Exact:** clip timing and trims, track order, scale modes, gravity, location, scale, rotation, origin, shear and
  margin (the same math as Zenvi), keyframe easing, opacity, volume, constant speed, fade transitions, and where
  titles sit.
- **Approximated:**
  - Chrome lays out SVG title text a few percent wider than Zenvi (same font, start and baseline).
  - Wipe transitions play as fades.
  - Brightness/Contrast, Saturation, Hue, Blur and Negate become CSS filters; Crop becomes a CSS clip-path; blend
    modes become `mix-blend-mode`.
  - Holds, reverse playback and speed ramps show the right frame on every frame but play no sound.
- **Not drawn:** other effects (they are listed in the export's README and come back on re-import), image
  sequences and missing media (left out, with a warning).
- **Colour:** libopenshot 1.0 decodes video tagged BT.709 with BT.601 coefficients, while Chrome follows the tag,
  so BT.709 video looks slightly different here than in Zenvi's preview (same test, 22–30 dB).

## Bring it back: the round trip

Import the exported folder (**File > Import Project > Remotion Project...**, or `import_remotion_project_tool`).
Zenvi recognises its own export by `src/zenvi/timeline.json` and offers:

- **Open as an editable Zenvi project** (recommended): it writes a new `.zvn` (with a new project id) and opens it.
- **Add its timeline to this project as native clips**: it adds the clips in one undo step, at the playhead. In an
  empty timeline the tracks and ids keep their numbers; otherwise the clips go on new tracks above. Keyframes are
  rescaled with the editor's frame-rate rule when the frame rates differ.
- **Import compositions as linked clips**: this renders `ZenviTimeline`, or compositions you added, with Remotion.

The native restore reads the original Zenvi objects from the `zenvi` block, so an untouched export comes back
**bit-identical**. Edits made to the readable timeline are applied onto those objects, and every other key, known
or not, is kept. Measured on this Mac, a timeline edited in the Remotion project (a clip moved, its slide's end
changed, a duplicate placed past the old end, the video trimmed, a marker moved) renders the same in Zenvi after
the restore as in Remotion: **40–47 dB PSNR excluding title text, frames 0–219**, eight edits reported, one undo
step.

| You change in `timeline.json` | Zenvi does |
| --- | --- |
| a clip's `position` (seconds) | moves it |
| `start` / `end` (source in / out, seconds) | trims it |
| `track` (index, 0 = bottom) or `layer` (a new number makes a new track) | moves it to another track |
| `title`, `hasAudio`, `hasVideo`, `scaleMode` (0–3), `gravity` (0–8), `blendMode` | sets them |
| `fileId` / `src` to another exported file | swaps its media |
| removes a clip entry | deletes the clip |
| copies a clip entry with a new `id` | duplicates the clip (new ids for its effects too) |
| `keyframes.<property>` (`alpha`, `location_x/y`, `scale_x/y`, `rotation`, `origin_x/y`, `shear_x/y`, `margin`, `corner_radius`, `volume`): values, frames, easing (`linear`, `hold`, `[x1, y1, x2, y2]`), added or removed points or properties | rewrites that property's keyframes; untouched points come back exactly |
| a marker's `time` / `frame`, `name`, `color`; added or removed markers | the same in Zenvi |
| a transition's `from`, `durationInFrames` (curves refit like a drag-trim), track; removed transitions | the same in Zenvi |
| a track's `name` or `locked` | renames or locks it |
| a file in `public/zenvi-media/`, e.g. the text of a title SVG | uses the edited copy when it is newer than the original |

A clip's timing has one representation, `position` / `start` / `end` in seconds; the renderer derives `from` and
`durationInFrames`, so editing frame numbers in a clip entry is reported, not applied. Keyframe frames are clip
frames: 0 is the clip's first frame before any trim. Trimming a clip therefore keeps its keyframes on the source,
in Remotion as in Zenvi. The composition's length follows the clips, so a clip moved past the end lengthens the
render.

Every value is checked before anything is applied. An impossible one (alpha 2, a trim past the end of the media,
a track that does not exist, an unknown easing) is refused with a note naming the clip and field, and that field
keeps its original value. These edits are reported but **not** brought back: speed (`playbackRate`, holds, ramps),
effect edits (`filters`, `crop`), new media files, new transitions, the composition's size, frame rate or length,
and the background. Changes to the renderer code are not read back either; to see them as they render, import
`ZenviTimeline` as a linked clip.

## For agents (editor tools)

| Tool | Arguments | What it does |
| --- | --- | --- |
| `list_remotion_compositions_tool` | `project_dir` | Read-only. Lists compositions (id, kind, size, fps, duration, `default_props`, `file:line`, folder) and the project (entry, Remotion version, installed, `zenvi_generated`). Without `node_modules` it lists from the code only. |
| `import_remotion_project_tool` | `project_dir`, `compositions` (ids; empty = all, or the Zenvi timeline of an export), `props` (`{id: {...}}`), `codec` (`auto` / `prores4444` / `h264` / `qtrle`), `position` (default: the playhead), `track` (one composition only), `restore_native` (default true) | Renders linked clips and/or restores an export natively: one undo step. The receipt lists the clips, codecs, `file:line`, and for a restore `native.edits` (what came back) and `warnings` (what did not). |
| `export_to_remotion_tool` | `output_dir`, `copy_media` (default true), `install` (default false) | Writes the Remotion project; the receipt lists the files, notes and next steps. Changes nothing in the project. |

The shared tools work on Remotion clips too: `get_linked_clip_tool` (state, `editable_props`),
`update_linked_clip_tool`, `rerender_linked_clip_tool`, `open_linked_source_tool` (`code` or `studio`) and
`unlink_clip_tool`.

## Licence

Remotion is not free for every use: individuals and companies of up to 3 people may use it for free; larger
companies need a company licence (https://www.remotion.dev/license). Zenvi does not include Remotion. Importing
uses the copy the project installed, and exported projects install it from npm under its own licence. The import
dialog and the export's README say so.

Importing renders the project's code (its `remotion.config.*`, its components), exactly like `npx remotion
render`: import projects you trust.

## For developers

| Module (`src/classes/handoff/remotion/`) | Role |
| --- | --- |
| `__init__.py` | Registers `RemotionProvider` and the two menu entries (loaded by `handoff.plugins.load_plugins`). |
| `detect.py` | Is this a Remotion project: root, entry, installed (Node-style resolution incl. workspaces, npm-nested and pnpm), version, package manager, whether it is a Zenvi export. |
| `sources.py` | The static scan behind Open Code. |
| `helper.mjs` / `helper.py` | The Node side (`probe`, `compositions`, `render`, `still`; `@@zenvi {json}` events; applies `remotion.config.*` and `.env` like the CLI) and its Python runner (progress, errors, bundle cache, cancel, orphan cleanup). |
| `provider.py` | `RemotionProvider`: fingerprint, render (auto codec, qtrle, stills), open code / studio, editable props. |
| `studio.py`, `install.py` | Remotion Studio processes; dependency installs. |
| `importer.py` | Listing and import; checks placement before rendering; one undo step. |
| `exporter.py`, `template/` | The generated project. |
| `restore.py`, `edits.py` | Native restore and the edit round trip. |
| `dialogs.py` | The Qt dialogs (loaded on use). |

`helper.mjs` and `template/` ship as package data (`setup.py` and `freeze.py` copy every file under `src/`).

Tests (headless, parallel-safe; the helper tests run real Node against a fake Remotion in `tests/remotion_fakes.py`):

```bash
.venv/bin/python -m pytest tests/test_handoff_remotion_*.py tests/test_editor_tools_handoff_remotion.py -q
QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \
  .venv/bin/python -m pytest tests/test_handoff_remotion_ui_qt.py -q
```

The real end-to-end run (Remotion + Chrome + libopenshot; renders, so it takes a few minutes) is
`tests/manual/remotion_handoff_e2e.py`; its docstring has the command. It covers linked import with alpha checks,
prop re-render as one undo step, cancel with no Chrome left behind, and export → npm install → tsc → render →
PSNR against libopenshot. It also checks re-import equality, and that edits made in the Remotion project render
the same in Zenvi.

### Troubleshooting

- **"Remotion needs Node.js"**: install Node 18+ or set `ZENVI_NODE`.
- **"... is not installed (remotion, @remotion/renderer ... missing)"**: run the project's install (`npm install`),
  or use Install Dependencies in the import dialog.
- **"Version mismatch ... zod"** in Remotion's output: pin zod to the version Remotion asks for (4.5.4 for 4.0.532;
  `npx remotion add zod`). Zenvi's exports already do.
- **A render fails**: the message carries Remotion's first error lines and the first browser console error; the
  full output is in Zenvi's log.
