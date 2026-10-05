# Zenvi → After Effects

**File > Export Project > After Effects Script (.jsx)...** writes one
ExtendScript file. Running it in After Effects rebuilds the open Zenvi
timeline as a composition: footage, layer order, trims, speed changes,
transforms with their eases, audio levels, titles, effects, fades and wipes,
and markers. **File > Send To > After Effects** does the same in a running
After Effects through the Zenvi Link panel. Two agent tools do the same:
`export_to_after_effects_tool` and `send_to_after_effects_tool`.

Neither changes the Zenvi project, so neither adds a Zenvi undo step. In
After Effects the whole import is one undo step. For the shared handoff core
(snapshots, keyframes, jobs, Zenvi Link), see [handoff.md](handoff.md).

**Platforms.** Exporting works on macOS, Windows and Linux. Send To needs
After Effects with the Zenvi Link panel (macOS and Windows). The AppleScript
fallback is macOS only. There are no preferences.

## Using it

### Export

The dialog has three settings:

- **Folder.** The default is `<project folder>/<name>_AfterEffects`, or
  `~/Downloads/<name>_AfterEffects` for an unsaved project.
- **Copy media into a media/ folder next to the script.** On by default; use
  it to move the folder to another computer.
- **Include audio.** On by default.

The export runs off the GUI thread, with a progress dialog you can cancel. It
is staged in a hidden folder and moved into place when complete. A file that
already exists in the target folder with other content is never overwritten:
the new file gets the next free name.

The result dialog shows:

- the layer and footage counts;
- the script path;
- for each title, whether it became editable text or an image, and why;
- missing media and warnings;
- **Reveal**;
- **Run in After Effects now**, when Zenvi can reach After Effects (Zenvi Link,
  or AppleScript on a Mac where After Effects is installed). Otherwise it says
  how to install Zenvi Link.

### Running the script

| How | Edit > Undo in After Effects shows |
| --- | --- |
| In After Effects: File > Scripts > Run Script File... and pick the `.jsx`. A closing alert lists layers, placeholders and warnings. | "Import Zenvi project" |
| From Zenvi through Zenvi Link: Send To, or "Run in After Effects now". | "Zenvi: Run JSX file" (Zenvi Link's group; the script's own group is nested inside it) |
| From Zenvi through AppleScript on macOS, when After Effects is installed but Zenvi Link is not connected. | "Import Zenvi project" |

Runs that Zenvi starts never show the closing alert:

- A Send To export is written as non-interactive.
- "Run in After Effects now" runs the interactive export through a small
  runner script (`.zenvi-run-*.jsx`, removed afterwards). The runner sets
  `$.global.ZENVI_AE_QUIET` only for that run.

Notes on the AppleScript route:

- **Which After Effects.** It asks the After Effects that is running, found
  with `ps` (no extra permission). Only when none is running does it ask the
  newest installed release.
- **Permission.** macOS asks once whether Zenvi may control After Effects. A
  refusal names System Settings > Privacy & Security > Automation.
- **Waiting.** Zenvi waits at most 31 minutes, and you can cancel. Cancelling
  only stops the wait; After Effects may still finish the comp. AE 2024's
  DoScriptFile is known to hang, and the timeout message says to run the
  script from File > Scripts instead.

### Send To

File > Send To > After Effects is enabled while Zenvi Link reports a
connected After Effects. It exports into
`<project>_assets/after_effects/<name>`, or `~/.openshot_qt/after_effects/<name>`
for an unsaved project. This is not a temp folder, because the comp keeps
referencing those files. It then runs the script through Zenvi Link's
`ae_run_jsx_file`, waiting up to 31 minutes; Zenvi Link allows 30. Zenvi shows
the summary the script returns.

### Agent tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `export_to_after_effects_tool` | `output_dir`? `collect_media`? (true) `open_folder`? (false) | Script, README and folder paths; layer, footage and media counts; per-title choice; missing media; warnings; stats. 0 undo steps. |
| `send_to_after_effects_tool` | `collect_media`? (false) | The comp name and id, layers, placeholders and warnings from After Effects, and the undo step to use there. If After Effects is not connected, it refuses and says how to connect. |

Both are background-safe. The project is snapshotted on the GUI thread, and
everything else runs on the calling worker as a handoff job.

## The export folder

```
Trip_AfterEffects/
  Trip.jsx      the script (ES3, plain ASCII after the first line)
  README.txt    how to run it, the title choices, missing media, notes
  media/        copies of the media (with "Copy media"; image sequences in their own folder)
  titles/       titles rendered as transparent PNG (the ones that cannot be editable text)
  masks/        wipe images rendered from the transition SVGs
```

The script builds:

- a project folder `Zenvi — <project>`, with `Footage` and `Titles`
  subfolders;
- each media file once. A missing file becomes a placeholder you can relink
  with File > Replace Footage, and never aborts the import;
- the comp, with the project's size, pixel aspect, frame rate and duration,
  and a black background;
- the layers and markers. The comp then opens.

The script returns a JSON summary: comp, comp id, layers, footage,
placeholders, warnings, and the name of its undo group.

## What maps to what

Everything follows libopenshot 1.0's semantics, read through C1's
`TimelineSnapshot`, `Curve` and `transform.geometry`. Each animated
property is checked against the exact value at every frame the clip shows:

- If it is an affine function of one Zenvi curve, it keeps that curve's keys
  and eases, converted with the TASTE §2 speed/influence formula and cut
  exactly at the clip's in and out points.
- Anything else is sampled per frame and reduced to linear keys within a
  tolerance: 0.1 px, 0.01 % scale, 0.005°, 0.05 % opacity, or 0.1 dB.

| Zenvi | After Effects |
| --- | --- |
| Tracks bottom to top; clips by position | Layers: a higher track is a higher layer. Track names and numbers go in the layer comment; locked tracks become locked layers. Each track gets a label colour. |
| Clip position, trim (`start`) | `inPoint`, `outPoint`, `startTime = inPoint − round(start·fps)/fps` |
| Constant speed (Clip > Speed) | Time Stretch `100/k`. Source times sit inside the frame libopenshot rounds to (see Timing below). |
| Reverse, freeze, speed ramps, holding past the curve or the media's end | Time Remap keys from the curve, eased like it or sampled |
| Transform: location, scale, rotation, origin, alpha; scale mode and gravity | Anchor Point = origin × source size; Position = the origin's canvas point; Scale; Rotation; Opacity = alpha × 100 |
| A move along one straight path with one easing | Position as one spatial property, eased along the path |
| Other moves: X and Y eased differently, the Back easings (they run past the path's ends), sampled moves | Separate X and Y Position, each with its own eases or samples |
| `volume` | Audio Levels: 20·log10(volume) dB on both channels, from −96 to +12 dB |
| `has_audio` / `has_video` off | Audio switch / video switch off. Audio-only files become audio layers, and an audio layer never shows a picture (not even a missing file's placeholder). |
| Simple title templates (text and rectangles) | A precomp of editable text and shape layers. 15 of the 50 bundled templates qualify: Box, Footer 1–3, Gray Box 1–4, Header 1–3, Solid Color, Standard 1/3/4. |
| Other titles (paths, gradients, filters, mirrored or rotated text...) | A transparent PNG rendered with QSvgRenderer the way libopenshot renders it. The reason is in the report. |
| Fade transition (a uniform mask) | The top clip's opacity × the Mask kernel's own per-frame result |
| Wipe transitions (image masks) | Gradient Wipe on the top clip. The wipe image is a hidden, disabled guide layer. Transition Completion comes from brightness and Softness from contrast. |
| `fade_audio_hint` transitions | libopenshot's equal-power crossfade in the Audio Levels |
| Markers | Comp markers: the name as the comment, the colour as the label |
| Linked clips (`zenvi_link`) | The rendered media, with the link in the layer comment. An After Effects link whose comp is in the open project reuses that comp. |

**Timing.** libopenshot shows source frame `round(time(n))`. After Effects
shows the frame whose span holds the source time, and keeps Time Remap
values in single precision. So for any time curve, the source times are
shifted to the middle of the range that shows libopenshot's frame at every
frame, and every frame is checked against `ClipView.source_frame_at`:

- **Normal speed through a curve** keeps a frame-aligned start.
- **2×, 3× and 4×** keep their times half a frame inside each frame.
- **0.5×** lands a quarter frame from both edges.
- **Ramps.** Where libopenshot's own approximate bezier rounds across a .5,
  only that frame gets an extra key, and the ramp keeps its ease.

**Effects** (`classes/exporters/after_effects_effects.py` has the full table):

| Zenvi | After Effects | Conversion |
| --- | --- | --- |
| Blur | Gaussian Blur (`ADBE Gaussian Blur 2`) | σ from libopenshot's box-blur passes at decode size; Blurriness = σ / 0.3; Repeat Edge Pixels on |
| Brightness | Brightness & Contrast (`ADBE Brightness & Contrast 2`), Use Legacy | Brightness 255·b, contrast 100·c/128 (approximate) |
| Saturation / Hue | Hue/Saturation (`ADBE HUE SATURATION`) | Master Saturation (s − 1)·100; Master Hue 360·h |
| Negate | Invert (`ADBE Invert`) | RGB |
| Pixelate | Mosaic (`ADBE Mosaic`) | Blocks from libopenshot's decode width |
| Sharpen | Sharpen (`ADBE Sharpen`) | Amount 0–40 → 0–100 (approximate) |
| ChromaKey | Keylight (`Keylight 906`), falling back to Color Key (`ADBE Color Key`) | Key colour; Color Key tolerance from fuzz (approximate) |
| Crop | A layer mask | The visible rectangle |
| ColorGrade | Lumetri Color (`ADBE Lumetri`) Basic Correction | Exposure, contrast, highlights, shadows, saturation, vibrance, temperature, tint (approximate). Curves, wheels, LUT and Mix warn unless neutral. |
| Anything else | — | A warning, and listed in the layer comment |

Each effect is added in a try/catch, so a missing third-party effect warns
instead of failing.

Parameters are found by match name first, then by English display name. A
parameter found by an unverified match name must take the value's kind, and
in an English After Effects it must also carry the expected name. A wrong
guess therefore never sets another parameter.

## Known limits

- **Not verified in a live After Effects** (none was available while this
  was built). The checks used were:
  - an ES3 lint and an `acorn --ecma3` parse;
  - golden scripts;
  - runs against a strict mock of the After Effects DOM
    (`tests/fixtures/after_effects/ae_dom.js`);
  - `tsc --checkJs` against the types-for-adobe typings, once.
- **Unverified parameter ids**, with display-name fallbacks: Mosaic,
  Gradient Wipe, Color Key, Keylight. The frame-rounding scheme assumes that
  After Effects shows the frame containing the source time.
- **Lumetri is set by English parameter names only.** In a non-English After
  Effects its parameters are left at their defaults, with a warning per
  parameter.
- **Approximate conversions:**
  - Brightness/Contrast, Saturation, Hue, Sharpen and ChromaKey tolerance.
  - Lumetri.
  - Gradient Wipe completion and softness. AE's softness curve is not
    documented.
  - Blur strength (it uses Skia Skottie's 0.3 constant).
- **Gradient Wipe versus Zenvi's wipe.** Gradient Wipe stretches the wipe
  image over the layer. For scaled or picture-in-picture clips, compare the
  wipe against Zenvi.
- **The guide layer and the Render Queue.** The wipe image is a guide layer,
  and guide layers are left out of Render Queue output. Check that a render
  still shows the wipe. If it does not, turn the guide layer into a normal
  layer with its video switch off.
- **Not exported, with a warning or note:**
  - shear, parenting, rounded corners
  - Crop x/y offsets and Resize
  - LUTs, curves, colour wheels and Mix in ColorGrade
  - captions
  - effects that are not in the table
- **Different frame rates.** Media whose frame rate differs from the project
  may land one source frame off, because libopenshot's FrameMapper and After
  Effects' resampling differ.
- **Fonts.** Titles use the fonts installed in After Effects. Missing fonts
  are reported once:
  - After Effects 24 and later look them up in the font list.
  - Older versions try the usual PostScript spellings (`Ubuntu-Regular`,
    `ArialMT`, `Arial-BoldMT`...).
- **Text on native titles.** Faint outlines (2 px or less at opacity 0.5 or
  less) are dropped, with a note.
- **Long sampled moves still cost a key per frame.** A move that is not one
  curve's function (non-centre gravity with a scale animation, for example)
  and that wiggles keeps most frames even at 0.1 px. Such a script can take
  minutes to build. Each key is 1-D and set without a search.

## Manual test plan (a person with After Effects 2022 or later)

To get media with a visible counter, run
`ffmpeg -f lavfi -i testsrc=duration=10:size=1280x720:rate=30 -pix_fmt yuv420p clipA.mp4`.
Do the same for `clipB.mp4` and `clipC.mp4`. Use any photo and any 12-second
music file. Start Zenvi with `./run.sh`.

1. **Build the project.** Make a 1280×720, 30 fps project with these tracks:
   - **Track 1:**
     - clipA at 0 s, trimmed to start 1 s into its media and 5 s long;
     - clipB at 4 s, overlapping clipA by 1 s, with a Fade over the overlap;
     - clipC at 10.5 s, overlapping clipB by 0.5 s, with "Wipe Left to Right"
       over the overlap. Set clipC to 2× speed.
   - **Track 2:** the photo at 1 s for 4 s, with Location X −0.5 at 1 s and 0
     at 2 s. Use "Ease Out (Back)" on the second key, then add Fade In.
   - **Track 3:** titles Standard_1 ("Launch day") at 2 s and Gold_1 at 7 s.
   - **Track 4:** the music, with Volume fade-out.

   Add a red marker "Title in" at 2 s and save the project as Trip.zvn.
   **Pass:** the preview plays the fade, the wipe, the fast clip and the
   photo's overshooting move.
2. **Export.** Choose File > Export Project > After Effects Script (.jsx)...
   and click Export with the defaults.
   **Pass:**
   - A progress dialog appears, then "Exported to After Effects" with 7
     layers and 7 footage items.
   - It says "Launch day: editable text" and "Gold: image - an SVG filter
     (glow, shadow or blur)".
   - Reveal shows `Trip_AfterEffects/` with Trip.jsx, README.txt, media/,
     titles/ and masks/.
3. **Run the script.** In After Effects choose File > Scripts > Run Script
   File... and pick Trip.jsx.
   **Pass:**
   - The folder "Zenvi — Trip" appears, with Footage and Titles.
   - The comp "Trip" (1280×720, 30 fps, 15.5 s) opens.
   - An alert summarises 7 layers and no warnings, and ends with `Edit > Undo
     "Import Zenvi project" removes it.`
4. **Check the layers.**
   **Pass:**
   - From top to bottom: music.wav, Gold.svg, Launch day.svg, photo,
     clipC, clipB, clipA, then the disabled guide "Wipe image:
     wipe_left_to_right".
   - clipA spans 0–5 s and clipB 4–10 s.
   - clipC spans 10.5–15.5 s with Stretch about 50.2%.
   - The photo's X Position and Y Position are keyed separately: the Back
     easing overshoots.
5. **Compare frames side by side.** Look at Zenvi and After Effects at
   0:00:04:09 (mid-fade), 0:00:10:22 (mid-wipe), 0:00:12:00 and 0:00:15:10
   (the 2× clip), and 0:00:01:15 (mid-move).
   **Pass:**
   - The counter in the 2× clip matches at every checked frame.
   - The photo is in the same place, with the same overshoot.
   - The fade opacity matches.
   - The wipe edge is in about the same place (its softness may differ
     slightly).
6. **Titles.** Double-click the Launch day layer.
   **Pass:**
   - Its precomp holds the editable text layers "Launch day" and the subtitle.
   - The font is Ubuntu Bold, or a substitute that the alert names.
   - Gold looks like Zenvi's render.
7. **Audio.** Look at the music's Audio Levels, then RAM-preview the fade-out.
   **Pass:** the keys fall from 0 dB where the fade-out starts to below
   −60 dB on its last frame, and the audio fades out like Zenvi.
8. **Markers.**
   **Pass:** there is a comp marker at 0:00:02:00 with the comment "Title
   in" and a red label.
9. **Undo.** Choose Edit > Undo Import Zenvi project.
   **Pass:** the folder, the footage and the comp all disappear in one step.
10. **Missing media.** Rename `media/clipB.mp4` and `media/music.wav`, then
    run Trip.jsx again.
    **Pass:**
    - The import finishes with placeholders clipB.mp4 and music.wav.
    - The alert has "Missing media" warnings.
    - The music placeholder shows no picture: its video switch is off and
      no colour bars cover the comp.
    - The other layers are intact.
11. **Render Queue.** Add the comp to the Render Queue and render the wipe
    section.
    **Pass:** the render shows the wipe from clipB to clipC. If it does not,
    the guide layer is excluded from renders; record this.
12. **Send To through Zenvi Link.** Install the panel (`zenvi adobe install`)
    and open Window > Extensions > Zenvi Link. In Zenvi choose File > Send
    To > After Effects.
    **Pass:**
    - The entry is enabled only while the panel runs.
    - The comp is built without an alert.
    - Zenvi's summary ends with `In After Effects, Edit > Undo "Zenvi: Run JSX
      file" removes it.`, and that is the name Edit > Undo shows.
    - The files are in `Trip_assets/after_effects/Trip/`.
13. **Run now through Zenvi Link.** Export again (step 2) with the panel
    open, then click "Run in After Effects now".
    **Pass:**
    - The comp is built with no alert in After Effects.
    - No `.zenvi-run-*.jsx` file is left next to Trip.jsx.
14. **AppleScript fallback (macOS).** Close the panel, export again and click
    "Run in After Effects now".
    **Pass:**
    - macOS asks once whether Zenvi may control After Effects; after you
      allow it, the comp is built in the After Effects that was already
      open.
    - If two releases are installed, the newer one is not started.
    - Cancel in the progress dialog stops Zenvi's wait, and After Effects
      may still finish.
    - If you deny permission, Zenvi points to System Settings > Privacy &
      Security > Automation.
15. **Lumetri in a non-English After Effects** (optional). Grade a clip with
    Colour grade, export it and run the script in a German or French After
    Effects.
    **Pass:** Lumetri Color is added, and its Basic Correction values are
    either set or reported as "left at its default". Nothing fails.
16. **Agent tools.** In the Zenvi chat, ask "export this timeline to After
    Effects".
    **Pass:**
    - `export_to_after_effects_tool` replies with the script path, the counts
      and the title choices.
    - Zenvi's undo history is unchanged.

## For developers

- `classes/exporters/after_effects.py`: the pure generator.
  `build_ae_script(snapshot, *, media_map, title_assets, mask_assets, options)`
  returns `AeExport(jsx, warnings, stats, titles, data)`.
- `after_effects_keys.py`: libopenshot curves to AE keys (exact bezier cuts,
  per-frame checks, sampling, pins).
- `after_effects_effects.py`: the effect mappings.
- `after_effects_titles.py`: title SVG to native layers, or a reason to
  render a PNG.
- `after_effects_runtime.py`: the fixed ES3 runtime every script carries.
- `after_effects_js.py`: ES3 literals and the lint.
- `classes/handoff/after_effects_export.py`: writes the export folder and
  runs it, through Zenvi Link, AppleScript or the quiet runner.
- `classes/handoff/after_effects.py`: the menus.
- `classes/editor_tools/handoff_after_effects.py`: the tools.

To run the tests from the repo root:

```bash
.venv/bin/python -m pytest $(ls tests/test_handoff_ae_*.py | grep -v _qt.py) -q
ZENVI_REAL_QT=1 QT_QPA_PLATFORM=offscreen PYTHONPATH=~/zenvi-deps-1.0/python \
  .venv/bin/python -m pytest tests/test_handoff_ae_qt.py -q      # QSvgRenderer titles and wipes
python tests/ae_goldens.py                                         # regenerate the golden scripts
node tests/fixtures/after_effects/ae_mock.js Trip.jsx --media media.json [options]   # run one in the mock
```

The mock runs a script in a realm without ES5 library methods, so a script
that relies on them fails there as it would in After Effects. It is strict
where After Effects is strict:

- argument shapes;
- ease array sizes;
- negative spatial speeds;
- references made stale by `addProperty` or by separating dimensions;
- locked layers;
- `%XX` in paths;
- eases recomputed when a side becomes Bezier.

Options model other setups: `--no-fonts-api`, `--fonts`, `--no-keylight`,
`--language`, `--wrong-param-ids`, `--no-audio-group`, `--key-time-grid`
and `--zenvi-link`.
