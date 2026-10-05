# HyperFrames ⇄ Zenvi

[HyperFrames](https://github.com/heygen-com/hyperframes) projects are HTML videos: `index.html` holds a root
composition whose timed elements (`<video>`, `<img>`, `<audio>`, nested compositions, titles and other scripted
graphics) play on a GSAP timeline. Zenvi opens such a project with its media as **native, editable clips** and its
compositions and scripted graphics as **linked clips** (rendered by HyperFrames, re-rendered when the code
changes). In the other direction, any Zenvi timeline exports as a HyperFrames project that `npx hyperframes
preview | lint | render` accept, and that comes back to Zenvi losslessly.

This page covers the desktop (zenvi-core) side. The shared linked-clip machinery (the Linked Source menu, the props
dialog, freshness, the toolbar pill) is described in [handoff.md](handoff.md).

```
 File > Import Project > HyperFrames Project...     File > Export Project > HyperFrames Project...
 import_hyperframes_project_tool                    export_to_hyperframes_tool
        │                                                   │
        ▼                                                   ▼
 classes/handoff/hyperframes/                        exporter.py  ──►  <folder>/index.html (+ assets/, README.md,
   parser · gsap · mapping · importer · restore                         hyperframes.json, meta.json, package.json,
        │                                                               .zenvi-export.json)
        ▼                                                   │
 provider.py + wrappers.py ── the HyperFrames CLI (Node + headless Chrome + ffmpeg)    re-import ◄──┘ (restore.py)
```

## What you need

- **Node.js 22 or newer** for the HyperFrames CLI (Zenvi searches the usual places; `ZENVI_NODE` points at a
  specific `node`). Without Node, Zenvi still reads a project's timing itself, but cannot render its linked parts.
- **The HyperFrames CLI.** Zenvi never ships it and never runs a copy that came with the project (a project's
  `node_modules` is its code). In order:
  1. `ZENVI_HYPERFRAMES_CLI`: a `hyperframes` executable, `bin/hyperframes.mjs`, `dist/cli.js`, or a folder holding
     one. Point it at a project's own install if you want that one.
  2. The local HyperFrames setup's private install (`~/.openshot_qt/hyperframes`, used by motion graphics), when it
     is the version the project pins, or the project pins none and it is at least 0.8.126.
  3. `npx --yes hyperframes@<version>`: the version the project's `package.json` scripts pin
     (`npx hyperframes@0.8.126 render`), else 0.8.126 (the version this handoff was verified with).

  Every command runs in a folder of Zenvi's own (`~/.openshot_qt/cache/hyperframes-run`) with the project folder
  as its argument, so a project's `.npmrc` never applies. The one exception is `hyperframes timeline --json`,
  which in 0.8.126 only reads the project from its working folder (a folder argument is taken for a sub-command
  name): it runs in the project folder, but as plain `node <entry>` — an npx CLI is first downloaded from
  Zenvi's folder and then run from npm's cache — so nothing npm reads from the project applies either.
  HyperFrames' telemetry, update check and self-install are always off (`HYPERFRAMES_NO_TELEMETRY=1`,
  `DO_NOT_TRACK=1`, ...): Zenvi never opts anyone in.
- **ffmpeg / ffprobe** (Zenvi already needs them) to measure media and check renders.
- The first render downloads HyperFrames' headless Chrome into `~/.cache/hyperframes` (HyperFrames does that).

## Import a HyperFrames project

**File > Import Project > HyperFrames Project...** and pick the folder that holds `index.html`.

1. Zenvi reads the project **without running any of it**: its own HTML / CSS / GSAP reader builds the summary
   dialog (composition, size, length, how many media clips, compositions and graphics layers, what cannot be
   rebuilt, and the trust note). Nothing of HyperFrames, Node or the project runs before you click **Import**.
2. **Import** asks HyperFrames for its own resolved timeline (`hyperframes timeline --json`), then renders the
   linked parts. Rendering runs the project's HTML and scripts in a headless browser, like `npx hyperframes
   render`: import projects you trust. The toolbar pill shows progress and Cancel.
3. Everything lands together, in **one undo step**, where the playhead was when you clicked Import (or at
   `position`). A failed or cancelled import adds nothing and deletes its renders; a commit that fails half-way
   takes back what it added. If the editor was too busy to finish adding the clips in time, Zenvi says they are
   still landing (do not import again). If another project was opened meanwhile, nothing is added.

### What becomes what (modes)

- **native** — every `<video>` / `<img>` / `<audio>` of the root composition becomes a Zenvi clip: its CSS box,
  `object-fit` / `object-position` and transform as scale mode, location and scale; its GSAP tweens
  (`to` / `from` / `fromTo` / `set` with literal targets and values, GSAP's own placement and ease rules) as
  keyframes — power1 / power2 eases as exact beziers, other eases sampled on every frame; `data-volume`, fades and
  `data-automation` volume lanes as volume keyframes; the in point (`data-playback-start`, else
  `data-media-start`) and `data-playback-rate` as trims and a time curve. Each nested composition becomes a linked
  clip rendered on its own with transparency. What else the root draws (titles, shapes, scripted DOM) becomes one
  linked *graphics layer* — two when part of it paints under the media and part over it.
- **flatten** — the whole project as one linked clip: HyperFrames' own render.
- **auto** (default) — native, except: a project without local media flattens; and so does one where a media clip
  has something Zenvi cannot rebuild exactly (the dialog and the receipt say what). Use **native** to get the
  editable clips anyway, without those parts.

Not rebuilt natively (auto flattens; native imports without it and lists it): animation of a wrapper around a
media element, CSS animations / transitions / filters / clip-path / masks / blend modes / shadows on media, GSAP
beyond literal tweens (stagger, function values, repeat / yoyo, nested timelines, `timeScale`), tweens added in
loops, functions, callbacks or conditions (they may run several times or never), looping videos that wrap
(`<video loop>` longer than its media), animated GIF / APNG images, and `data-automation` playback-rate lanes.
`object-fit: cover` inside a box smaller than the frame is approximated (warned). A speed change on a clip with
sound warns: HyperFrames keeps the pitch, Zenvi's time curve does not.

### Timing

HyperFrames' CLI resolves the timeline (`data-start` as seconds or `<id>` / `<id> ± n`, durations, tracks), with
one exception: it resolves a reference against **authored** durations only, so `data-start="<id>"` after a video
without `data-duration` comes back at that video's *start*, while the runtime plays it after the video's media.
Zenvi uses the CLI's start only for numeric starts and for references to clips with a `data-duration` (and whose
own start counts); the others it resolves itself from the measured media. Without the CLI (no Node) Zenvi's own
resolver does all of it. Images without `data-duration` last 3 s; media last their length from the in point over
the playback rate.

### Tracks and what is in front

HyperFrames never stacks by `data-track-index` (a lane in its timeline): CSS `z-index` and the document order
decide what is in front. Zenvi keeps each lane's clips on one track (new tracks above the existing ones; an empty
timeline reuses its tracks from the bottom; `track` puts the first one on a given track) and orders the tracks by
what HyperFrames paints in front. When two lanes hold clips that stack both ways, the import says which ones to
move.

The composition's background colour is not imported (Zenvi shows black behind the clips; the import says so).

## Linked clips

| Role | What it renders | How |
| --- | --- | --- |
| `project` | the whole project | `hyperframes render` of `index.html`, MP4 with its sound |
| `composition` | one nested composition, transparent | a one-host wrapper (`data-composition-src`), or a copy of `index.html` where everything but it is hidden (inline compositions; its wrapper elements keep their layout, their own backgrounds cleared) |
| `layer` | the root's graphics, transparent | a copy of `index.html` with the clips Zenvi rebuilt natively **hidden** (`opacity: 0 !important`): the root's scripts still find them and the layout stays |

Wrappers live in a hidden `.render-zenvi-*/` folder inside the project during a render and are deleted afterwards.
Transparent renders are ProRes 4444 (HyperFrames' MOV, video only); a composition that turns out fully opaque on
its first, middle and last frames is re-encoded to H.264 with the same YUV values. Renders are always SDR
(`--sdr`: libopenshot has no HDR path).

**Variables.** A linked clip's `props` hold only what was changed **in Zenvi**. Each render reads the variables
from the HTML as it is now — the declared defaults (`<html data-composition-variables>`), and for a composition its
mount's `data-variable-values` on top — and puts the props over them. So an edit made in HyperFrames comes through
on the next render, while a value changed in Linked Source > Edit Props (or `update_linked_clip_tool`) stays. Edit
Props shows the HTML's values with Zenvi's changes on top; a value set equal to the HTML's is not a change and is
dropped when the clip renders.

**Freshness.** A clip reads stale when the project's HTML changes structurally (HyperFrames Studio's `data-hf-id`
stamps do not count), when its CSS / JS / JSON / assets change (also inside symlinked folders), or when its link
changes (props, fps, what the layer hides). Edits made in Studio's manual-edit mode
(`.hyperframes/studio-manual-edits.json`) are not applied by `hyperframes render`, so they do not reach Zenvi's
renders either, and do not make clips stale.

**Open Code** opens the composition's file at its line. **Open in Studio** starts `hyperframes preview` for the
project on a free loopback port (one per project, reused, stopped when Zenvi quits) and opens it in the browser. A
Studio you started yourself (port 3002) is not reused.

Layers name the elements they hide by id, or, without an id, by their position in `index.html` (`@1/2`) plus what
they look like (tag, class, media); when an edit moves such an element, Zenvi finds it again by its look, and the
layer says when it cannot. Giving elements ids avoids the question.

## A Zenvi export comes back

A folder Zenvi exported holds the Zenvi project in `index.html` (`<script type="application/json"
id="zenvi-timeline">`). Importing it restores the clips natively and losslessly (ids, keyframes, effects,
transitions, markers; files from the originals when they are unchanged, else from `assets/`; keyframes rescaled to
the project's frame rate). What was changed in HyperFrames since comes along:

- an exported element's new `data-start`, `data-duration` or in point (`data-playback-start` /
  `data-media-start`) moves or trims its clip;
- an element split or duplicated in Studio (the clone keeps `data-zenvi-clip-id`) comes back as copies of its clip
  with fresh ids, each with its element's timing (and the clip's effects and keyframes);
- a deleted element drops its clip;
- a new lane (`data-track-index`) is only reported: it does not change what HyperFrames paints, so the clip keeps
  its Zenvi track;
- anything added (elements without `data-zenvi-clip-id`, compositions) imports like any HyperFrames content.

Choose **One linked clip of the whole project** to bring the export in as HyperFrames renders it instead.

## Export the timeline

**File > Export Project > HyperFrames Project...**, a new or empty folder, then **Export** (or
`export_to_hyperframes_tool`). The folder gets `index.html`, `hyperframes.json`, `meta.json` and `package.json`
like `hyperframes init` writes them (scripts pin `npx hyperframes@0.8.126`), the media in `assets/` (copied, or
linked with `copy_media: false`), `README.md` (how to preview, lint and render; what did not translate) and
`.zenvi-export.json` (what Zenvi wrote there). The result is linted when the CLI is available.

- Every clip is a primitive: `<video>`, `<img>` (stills and SVG titles) or `<audio>`, with `data-start`,
  `data-duration`, `data-media-start`, `data-track-index` (painted bottom-up with `z-index`),
  `data-playback-rate` for a constant speed, `data-volume` or a `data-automation` volume lane, and
  `data-zenvi-clip-id`.
- Geometry comes from libopenshot 1.0's own transform (scale modes, gravity, origin, rotation, shear, SCALE_NONE
  sizes). A keyframed channel that follows one curve becomes one GSAP tween per keyframe segment, eased by
  `zenviEase` — the bezier evaluation libopenshot uses — so the browser draws the same pixels frame for frame;
  channels that mix curves are sampled per frame.
- Transitions are applied the way libopenshot applies them: only to the clip on top (the one that starts last on
  that track), only while the transition runs, with the fade mask's brightness / contrast formula (checked against
  libopenshot's frames). Wipes become cross-fades. A transition with *Fade Audio* also writes its equal-power audio
  fades into the volume lanes.
- GSAP loads from the jsDelivr CDN exactly like the `hyperframes init` template. GSAP is under its own no-charge
  licence; it is never part of Zenvi or of the exported folder.

**Exporting again into the same folder** replaces exactly the files listed in `.zenvi-export.json` — all together:
they move aside, the new ones move in, and any failure puts the earlier export back. Your own files there stay; a
new file never takes the name of one of them. If Zenvi's files were edited since (in HyperFrames: `index.html`,
`package.json`, an asset; Studio's `data-hf-id` stamps do not count), the export stops and lists them: import the
folder first to keep those edits, export to a new folder, or replace them (the dialog asks; the tool takes
`overwrite_changes: true`). A folder whose record points outside it, or whose `assets/` is a link to another
folder, is refused.

Not exported (listed in the README and the receipt; they come back on re-import): effects, rounded corners, blend
modes, parent links, animated origin / margin (exported at their first value), speed ramps, freezes and reverse
(normal speed from the in point), image sequences and missing media.

## Colour

Zenvi does **no colour conversion** on HyperFrames renders (SPEC section 5, 2026-10-05; see
[handoff.md](handoff.md#colour-linked-media-is-treated-like-any-other-media)). HyperFrames' MP4 is BT.709 and its
ProRes is BT.601; libopenshot 1.0 decodes every YUV source with BT.601 coefficients, so BT.709 media — imported
videos and flattened renders alike — look slightly shifted in Zenvi's preview, and come out right in Zenvi's
exports. That is a libopenshot issue to fix there, for all media.

## Editor tools

- `import_hyperframes_project_tool(project_dir, mode="auto" | "native" | "flatten", position=null, track="")` —
  background-safe, one undo step; the receipt lists every clip (native / linked / restored), the mode and why, the
  project summary and what could not be rebuilt.
- `export_to_hyperframes_tool(output_dir, copy_media=true, overwrite_changes=false)` — changes nothing in the
  project; the receipt carries the lint result and the changed files it replaced.

The shared tools (`get_linked_clip_tool`, `update_linked_clip_tool`, `rerender_linked_clip_tool`,
`open_linked_source_tool`) work on HyperFrames clips like on any linked clip.

## Code map

| File | What |
| --- | --- |
| `parser.py` | HTML, CSS and timing of a project (stdlib `html.parser`); the CLI timing rule (`cli_start_trusted`) |
| `gsap.py` | the GSAP reader: timelines, placement rules, eases, what it cannot read |
| `mapping.py` | CSS and tweens as Zenvi transform / volume / time keyframes |
| `importer.py` | inspection (modes, problems, tracks by paint order) and the one-undo-step commit |
| `restore.py` | a Zenvi export back as native clips, with HyperFrames-side edits |
| `provider.py`, `wrappers.py` | the `hyperframes` link provider: fingerprint, render wrappers, props, Studio |
| `cli.py` | which CLI, quiet environment, timeline / lint / render, Studio processes |
| `exporter.py` | the timeline as a HyperFrames project, transitions, the export folder record |
| `dialogs.py` | the Qt dialogs and their background jobs |

## Tests

```bash
.venv/bin/python -m pytest tests/test_handoff_hyperframes_*.py tests/test_editor_tools_handoff_hyperframes.py -q
QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \
  .venv/bin/python -m pytest tests/test_handoff_hyperframes_ui_qt.py -q
```

`tests/manual/hyperframes_handoff_e2e.py` is the real end-to-end check (HyperFrames CLI + headless Chrome +
libopenshot 1.0; see its docstring): it scaffolds a project with `hyperframes init`, imports it native and
flattened, compares frames, re-renders, starts the Studio, cancels a render, exports a Zenvi project (with a
cross-fade) and compares HyperFrames' render with libopenshot's frames, then re-imports it with a duplicated clip
and checks the re-export rules.
