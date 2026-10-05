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
| `public/zenvi-media/` | the media ({{MEDIA_MODE}}) |

Keyframes in `timeline.json` are `{frame, value, easing}`; `frame` counts from the clip's first
visible frame and `easing` (`linear`, `hold` or a cubic-bezier `[x1, y1, x2, y2]`) shapes the
segment that ends at that keyframe.

## Bring it back into Zenvi

In Zenvi choose **File > Import Project > Remotion Project...** and pick this folder.

- **Open as an editable Zenvi project** (recommended) restores the original clips, keyframes,
  effects and transitions exactly as they were exported: native, editable Zenvi clips.
- Compositions you add to this project come back as **linked clips**: Zenvi renders them with
  this project's own Remotion and keeps the link (Edit Props, Open Code, Open in Studio,
  Re-render).

The native restore reads the `zenvi` block of `src/zenvi/timeline.json`. Changes to the readable
part of `timeline.json` or to the renderer code are not read back into native clips (Zenvi says
so when `timeline.json` was edited). To bring such changes back as they render, import
`ZenviTimeline` as a linked clip instead.

Agents can do the same with Zenvi's `import_remotion_project_tool` (`project_dir` = this folder).

## How close is it to Zenvi?

Exact: clip timing and trims, track order, scale modes, gravity, location, scale, rotation,
origin and shear (the same math as Zenvi), keyframe easing, opacity, volume, constant speed,
fade transitions, titles (the title SVGs; fonts come from this computer).

Approximated: wipe transitions play as fades; Brightness/Contrast, Saturation, Hue, Blur and
Negate are CSS filters; Crop is a CSS clip-path; blend modes are CSS `mix-blend-mode`; holds,
reverse and speed ramps show the right frame each frame but play no sound.

{{NOTES}}

## Remotion licence

Remotion is not free for every use: individuals and companies of up to 3 people may use it
for free; larger companies need a company licence — see https://www.remotion.dev/license.
Zenvi does not include Remotion: `npm install` downloads it from npm under its own licence.
