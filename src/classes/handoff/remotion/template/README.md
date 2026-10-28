# {{SOURCE_PROJECT}} — a Remotion project exported from Zenvi

{{GENERATOR}} wrote this [Remotion](https://www.remotion.dev) project from the Zenvi
project **{{SOURCE_PROJECT}}**. One composition, `ZenviTimeline` ({{WIDTH}}×{{HEIGHT}},
{{FPS}} fps, {{DURATION}}), plays the whole timeline: every clip is a `<Sequence>` holding an
`<OffthreadVideo>`, `<Img>` or `<Audio>`, and keyframes are Remotion's `interpolate()` with
`Easing.bezier()`.

## Run it

Node.js 18 or newer:

```bash
npm install                                        # Remotion {{REMOTION_VERSION}} (pinned)
npx remotion studio                                # preview; edit props in the right panel
npx remotion render ZenviTimeline out/video.mp4    # render the timeline
```

## What is where

| Path | What |
| --- | --- |
| `src/Root.tsx` | the `ZenviTimeline` composition and its props schema (`background`, `volume`, `showTitles`) |
| `src/zenvi/timeline.json` | the timeline: tracks, clips, keyframes, transitions, markers; under `zenvi`, the original Zenvi project for a lossless round trip |
| `src/zenvi/ZenviTimeline.tsx`, `ZenviClip.tsx` | the renderer |
| `src/zenvi/geometry.ts` | Zenvi's clip placement (scale mode, gravity, location, scale, rotation, origin, shear) |
| `src/zenvi/curves.ts` | keyframe evaluation, CSS filters, crop |
| `src/zenvi/timing.ts` | clip frames, the composition's length, stacking order (track, then position), sound |
| `public/zenvi-media/` | the media ({{MEDIA_MODE}}) |

A clip's timing in `timeline.json` is `position` (where it starts on the timeline), `start` and `end`
(source in and out), in seconds; the renderer derives the frames. Keyframes are `{frame, value,
easing}`: `frame` is a clip frame (0 = the clip's first frame before any trim, so trimming keeps
keyframes on the source) and `easing` (`linear`, `hold` or a cubic-bezier `[x1, y1, x2, y2]`) shapes
the segment that ends at that keyframe.

## Bring it back into Zenvi

In Zenvi choose **File > Import Project > Remotion Project...** and pick this folder.

- **Open as an editable Zenvi project** (recommended) restores the original clips, keyframes,
  effects and transitions as native, editable Zenvi clips -- with the edits you made here.
- Compositions you add to this project come back as **linked clips**: Zenvi renders them with
  this project's own Remotion and keeps the link (Edit Props, Open Code, Open in Studio,
  Re-render).

Edits to `src/zenvi/timeline.json` that come back onto the original Zenvi clips (everything else
about them stays exactly as exported):

- clips: move (`position`), trim (`start` / `end`: source in / out), another track (`track` index
  or `layer` number; new layer numbers become new tracks), `title`, another exported file (`fileId`
  / `src`), `hasAudio` / `hasVideo`, `scaleMode`, `gravity`, `blendMode`; delete a clip by removing
  it; duplicate one by copying its entry with a new `id`;
- keyframes of alpha, location, scale, rotation, origin, shear, margin, corner radius and volume:
  values, frames and easing (`linear`, `hold` or `[x1, y1, x2, y2]`), added or removed points;
- markers (time, name, colour; add or remove), transitions (`from`, `durationInFrames`, track;
  remove), track names and locks;
- media you change in `public/zenvi-media/` (e.g. the text of a title SVG) when the copy is newer
  than the original.

Zenvi checks every value first: an impossible one (alpha 2, a trim past the end of the media, an
unknown easing) is refused with a note naming the clip and field, and the original value stays.
Speed changes (`playbackRate`, holds, ramps), effect edits (`filters`, `crop`), new media files,
new transitions and the composition's size or frame rate are not brought back -- Zenvi says so.
Changes to the renderer code are not read back either: import `ZenviTimeline` as a linked clip to
see them as they render.

Agents can do the same with Zenvi's `import_remotion_project_tool` (`project_dir` = this folder).

Exporting from Zenvi into this folder again updates it (`node_modules`, added dependencies and your
own files stay). Zenvi records what it wrote (under `zenvi` in `timeline.json`), so it will not
replace changes you made here -- timeline edits, code, media copies -- without asking: import them
first to keep them.

## How close is it to Zenvi?

Exact: clip timing and trims, stacking (by track, then position), scale modes, gravity, location,
scale, rotation, origin and shear (the same math as Zenvi), keyframe easing, opacity, volume,
constant speed, fade transitions, and where titles sit (the title SVGs themselves; fonts come from
this computer).

Approximated: Chrome lays out SVG title text a few percent wider than Zenvi does (same font,
start and baseline); wipe transitions play as fades; Brightness/Contrast, Saturation, Hue, Blur
and Negate are CSS filters; Crop is a CSS clip-path; blend modes are CSS `mix-blend-mode`; holds,
reverse and speed ramps show the right frame each frame but play no sound. Zenvi (libopenshot)
decodes video as BT.601 even when it is tagged BT.709; Chrome follows the tag, so BT.709 video
looks slightly different in colour here than in Zenvi's preview.

{{NOTES}}

## Remotion licence

Remotion is not free for every use: individuals and companies of up to 3 people may use it
for free; larger companies need a company licence — see https://www.remotion.dev/license.
Zenvi does not include Remotion: `npm install` downloads it from npm under its own licence.
