# Premiere Pro ⇄ Zenvi

Zenvi and Adobe Premiere Pro exchange edits as **Final Cut Pro 7 XML** (xmeml version 4). Premiere imports that
format with File > Import and writes it with File > Export > Final Cut Pro XML. Zenvi's exporter follows the
conventions of Premiere's own exports, so motion, opacity, volume, speed, dissolves, markers and track names land
where they were. The importer reads those same conventions and brings a Premiere sequence back as one undo step.
With the Zenvi Link panel running in Premiere, **File > Send To > Premiere Pro** opens the timeline in Premiere
directly, and the panel's **Send to Zenvi** button brings the active sequence back.

This page covers the desktop (zenvi-core) side. The shared machinery (Zenvi Link discovery, the toolbar pill, the
plug-in registry) is described in [handoff.md](handoff.md). The panel and its `premiere_*` tools live in zenvi-web
(`packages/adobe-link/docs/PREMIERE_PRO.md`).

```
 Zenvi -> Premiere
   File > Export Project > Premiere Pro (.xml)...      ┐
   File > Export Project > Export XML (Final Cut Pro)  ├─► exporters/final_cut_pro.py ─► <name> (Premiere).xml
   export_to_premiere_tool                             ┘     + <name> (Premiere)_media/titles/*.png
   File > Send To > Premiere Pro                       ┐
   send_to_premiere_tool                               ┴─► <project>_assets/premiere/<time> <name>/
                                                           ─► premiere_import_xml (Zenvi Link panel in Premiere)
 Premiere -> Zenvi
   Premiere: File > Export > Final Cut Pro XML, or the panel's Send to Zenvi (premiere_export_xml)
   File > Import Project > Premiere Pro XML...         ┐
   File > Import Project > Import XML (Final Cut Pro)  ├─► importers/final_cut_pro.py ─► new tracks, ONE undo step
   import_timeline_xml_tool                            ┘
```

## Zenvi → Premiere

### Export a Premiere XML

**File > Export Project > Premiere Pro (.xml)...** asks where to write the XML. The default is
`<project> (Premiere).xml` next to the saved project, or `~/Untitled Project (Premiere).xml` for an unsaved one.

- **Copy the media next to the XML** copies every media file into `<xml name>_media/` and points the XML at the
  copies. Use it to move the edit to another computer: copy the XML and that folder together.
- **Open the folder when done** shows the result in Finder or Explorer.
- Titles are always rendered to transparent PNG stills in `<xml name>_media/titles/`, because Premiere cannot
  import SVG. Stills in formats Premiere may not read (WebP, AVIF, HEIC, JPEG XL) are converted to PNG in
  `<xml name>_media/stills/`.

The export runs in the background; the toolbar pill shows it and has a Cancel button. Everything that could not be
carried over exactly is listed in a report when it finishes. The XML and its stills are written to a temporary
name first and moved into place only when complete, so a failed or cancelled export leaves any earlier file as it
was. **File > Export Project > Export XML (Final Cut Pro)** uses the same exporter.

In Premiere, use **File > Import** and pick the XML. Premiere adds a bin with the sequence and its media.

### Send To Premiere Pro

**File > Send To > Premiere Pro** is enabled while Premiere runs the Zenvi Link panel (Window > Extensions >
Zenvi Link). When the entry is disabled, its tooltip says how to connect. Choosing it makes Zenvi:

1. writes the XML and the title stills into `<project>_assets/premiere/<YYYYmmdd-HHMMSS> <timeline name>/`
   (`~/.openshot_qt/premiere/...` for an unsaved project). They stay there because the Premiere project links to
   the PNG stills;
2. asks Premiere to import it with `premiere_import_xml`. The sequence lands in the **Zenvi Imports** bin and
   opens;
3. reports the new sequence's name, any media Premiere could not find, and the conversion warnings.

The import is one step in Premiere's own undo history. Nothing changes in the Zenvi project.

### What carries over

| Zenvi | Premiere (in the XML) |
| --- | --- |
| Clip position and trims | `start`/`end` on the sequence and `in`/`out` in the media, in whole frames at the sequence rate (29.97 = timebase 30 + NTSC) |
| Constant speed, reverse | Time Remap: `speed` in percent and `reverse`. `in`/`out` and keyframe times count *retimed* frames, as Premiere does |
| Variable speed, freeze frames | Time Remap `graphdict` keys through the same frames (with a warning) |
| Location, scale (fit/crop/stretch/none), rotation, origin | Basic Motion: `center` is the anchor's offset from the frame centre in source-media units, `centerOffset` is the anchor point, `scale` is a percentage of the native size, and `rotation` is in degrees clockwise |
| Uneven scale (`scale_x` ≠ `scale_y`) | Scale (height) plus Distort `aspect` (with a warning) |
| Crop effect | Crop (left/right/top/bottom in percent) |
| Alpha keyframes, fade presets | Opacity keyframes (0–100) |
| Volume keyframes | Audio Levels: linear gain, 1 = 0 dB, capped at +12 dB |
| Eased and hold keyframes | Extra linear keys wherever a straight line would leave the curve: ¼ source pixel, 0.05 % scale, 0.05°, 1 % opacity. Premiere's XML keyframes are linear |
| Crossfade (Fade transition across two clips) | Cross Dissolve centred on the cut. The clip edges inside it are `-1`, as in Premiere's exports |
| Fade from or to black at a clip edge | Cross Dissolve at that edge |
| Transition with "fade audio" | Cross Fade (+3dB) on the audio tracks too |
| Other wipes | Cross Dissolve with the same timing (with a warning) |
| Markers | Sequence markers with names and colours (`pproColor`; green is Premiere's default) |
| Track names, locks | `MZ.TrackName`, `locked` |
| Clip video / audio switches | Only the parts that play are written |
| Overlapping clips on one track (no transition) | Extra tracks named `<track> (2)`, so nothing is hidden |
| Stereo audio | Premiere's exploded track pairs (A1/A2), linked to the video item |
| Titles | Transparent PNG stills, `alphatype straight`, named without `.svg` |
| Media paths | `file://localhost/` URLs, percent-encoded (spaces, Unicode), `C%3a` for drive letters, `file://server/share` for UNC paths |

The export warns about anything else: Zenvi effects that FCP7 XML cannot express (blur, colour grading, and so
on), shear, clips that follow a parent clip, scale above 1000 %, gain above +12 dB, image sequences (relink them
in Premiere), and frame rates without a whole timebase.

## Premiere → Zenvi

### Import a Premiere sequence

In Premiere, select the sequence and use **File > Export > Final Cut Pro XML...**, or press **Send to Zenvi** in
the Zenvi Link panel. The panel runs `premiere_export_xml` and then this import with `placement: new_tracks`.

In Zenvi, use **File > Import Project > Premiere Pro XML...** (or *Import XML (Final Cut Pro)*, the same importer).

- Parsing (defusedxml), finding the media and probing it run in the background; the toolbar pill shows the import.
- Each media file is looked for at its own path, then in the XML's folder, in `<xml name>_media/`, in `media/`
  next to the XML, and in the folders you pointed Find Missing File at. When media is still missing, Zenvi asks for
  it once per import, even when *none* of it was found (an XML from another computer). Pointing at one folder
  finds the other files in it.
- Everything lands on **new tracks above the existing ones**. With `placement` `new_tracks` (the menu and the
  default) the sequence keeps its own timing; `at_playhead` starts it at the playhead.
- **One Ctrl+Z removes the whole import.** A refused or cancelled import (not an FCP7 XML, no clip that can be
  placed, Cancel while it is read) adds nothing to the history.
- A report lists what could not be carried over.

### What maps

| Premiere | Zenvi |
| --- | --- |
| Clip items and trims | Clips with the same position, start and end (snapped to the project's frames) |
| Linked video + audio with the same timing | One clip. J/L cuts become a video-only and an audio-only clip |
| A video item without linked audio | The clip's audio is off (Premiere played none) |
| Disabled clips, turned-off or muted tracks | Clips with video off (hidden) or audio off (muted): nothing is lost |
| Cross Dissolve (and other video transitions, with a warning) | A Zenvi fade transition over the same frames with a straight-line ramp, so it looks like Premiere's. The two clips overlap on one track, like Zenvi's own crossfades |
| Audio cross fade under a video dissolve | libopenshot's equal-power audio crossfade on that transition |
| Other audio transitions | Volume ramps (constant power for Cross Fade (+3dB)) |
| Time Remap: constant speed, reverse | The `time` curve Zenvi's Speed menu writes |
| Time Remap: variable speed | A straight-line `time` curve through the same frames (with a warning) |
| Basic Motion, Distort aspect, Crop, Opacity, Audio Levels | Location, scale, rotation, origin, the Crop effect, alpha and volume, static or keyframed, placing the clip where Premiere did |
| Sequence markers | Timeline markers with the nearest colour (marker durations are dropped) |
| Nested sequences | **Flattened**: the nest's clips come in on extra tracks right above the track the nest sat on, cut to the part that was used, up to 8 levels deep. The nest's own motion, opacity or speed is reported, not applied |
| A sequence of another size or frame rate | Scaled to fit and snapped to the project's frames (reported) |
| Generators (Premiere titles, colour mattes, bars), clip markers, effects Zenvi lacks | Left out and listed in the report |

## Editor tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `export_to_premiere_tool` | `output_path` (default `<project> (Premiere).xml`, a free name), `collect_media` (false), `overwrite` (false) | Writes the XML (and title stills); returns `path`, `media_dir`, `titles`, `copied_media`, `warnings`, `counts`. Refuses an existing file unless `overwrite`. No undo step |
| `send_to_premiere_tool` | — | Export + `premiere_import_xml` in the connected Premiere. Returns `sequence_name`, `sequences`, `offline`, `warnings`, `xml`. Refused, with how to connect, when Premiere is not connected |
| `import_timeline_xml_tool` | `path`, `placement` (`new_tracks` / `at_playhead`) | One undo step. Returns `timeline_clip_ids`, `layers`, `transition_ids`, `marker_ids`, `file_ids`, `missing_media`, `warnings`, `offset`. Missing media is skipped and listed (no dialog); refused when nothing can be imported |

Coverage ids: `handoff.premiere_export`, `handoff.premiere_send`, `handoff.timeline_xml_import`.
`import_project_file_tool` (format `fcpxml`) and `export_project_file_tool` use the same importer and exporter.

## Limits

- **Not verified in Premiere yet.** The conventions come from real Premiere exports: OpenTimelineIO's
  `premiere_example.xml` and Premiere XML files published in open-source projects, cross-checked with tools that
  round-trip with Premiere. The exports read cleanly in OpenTimelineIO's FCP 7 adapter, and imported motion renders
  where Premiere would draw it (real libopenshot test). Run the manual test plan below on a real Premiere.
- Eased curves arrive in Premiere as many linear keys that trace the same motion. They come back to Zenvi as
  linear keys too, not as the original bezier.
- Titles come back as PNG stills, not editable Zenvi titles. Titles and graphics made in Premiere are not
  imported.
- Zenvi's default Fade transition does its dissolve in the first half of its length (its brightness ramp
  saturates: a 1 s Fade blends from about 0.15 s to 0.5 s). Premiere's Cross Dissolve of the same length is linear
  across the whole second, centred on the cut. The export keeps the length you chose. A Cross Dissolve comes back
  as a linear fade that matches Premiere's look, not as the original Fade.
- Effects do not carry over in either direction, except the transforms, crop, opacity, volume and speed above.
- Landing a large import holds the editor for a moment, because libopenshot updates its timeline once per clip.
  The import runs as one preview batch (playback caching off, one redraw at the end): on a loaded 8 GB Mac, 14
  clips took 4–9 s, against 11–17 s without it.
- OpenTimelineIO's FCP 7 adapter ignores `MZ.TrackName` and Time Remap, which matters only for OTIO-based tools.

## Manual test plan (Premiere Pro 2024–2026)

Before you start: in Premiere, set *Preferences > Media > Default Media Scaling* to **None** (the XML carries each
clip's scale). For steps 9–11, install the Zenvi Link panel (`zenvi adobe install`) and open it with Window >
Extensions > Zenvi Link.

1. In Zenvi, make a 1080p 30 fps timeline: two clips with a 1 s Fade between them on track 1; a PNG logo on
   track 2 with eased location keyframes, a scale change and a 0.5 s fade in; a title on track 3; a clip at 2x
   speed and a reversed clip; music on track 4 with a 1.5 s volume fade out; a red marker and a green marker;
   tracks named Picture, Logo, Titles and Music, with Music locked. Save the project.
   **Pass:** the project saves.
2. File > Export Project > Premiere Pro (.xml)..., tick *Copy the media next to the XML*, OK.
   **Pass:** the pill says it exported; `<name> (Premiere).xml` and `<name> (Premiere)_media/` (with the media
   and `titles/<title>-<id>.png`) appear next to the project; no report appears (or only expected warnings).
3. In Premiere: File > Import, pick the XML.
   **Pass:** a bin holds the sequence and its media; nothing is offline.
4. Open the sequence and play it next to Zenvi's preview.
   **Pass:** clip positions and trims match frame for frame; the Fade is a Cross Dissolve of the same length
   centred on the cut.
5. Select the logo and open Effect Controls.
   **Pass:** Position keyframes trace the same eased path (many linear keys), Scale and Opacity match, and the
   logo sits where it is in Zenvi at the first, middle and last keyframe.
6. Check the 2x and reversed clips.
   **Pass:** Speed shows 200 % and Reverse is on for the reversed one; they show the same frames as in Zenvi at
   their first and last frame.
7. Check the title, markers and tracks.
   **Pass:** the title is a PNG with transparency over the video, named without `.svg`; markers have their names,
   red and green; tracks are named Picture/Logo/Titles/Music and the music tracks are locked; stereo audio is on
   linked track pairs.
8. Play the end of the music.
   **Pass:** the volume fades out over 1.5 s (Audio Levels keyframes).
9. Close the Premiere sequence. In Zenvi, open File > Send To.
   **Pass:** *Premiere Pro* is enabled while the panel runs; with Premiere closed it is disabled and its tooltip
   says how to connect.
10. With Premiere and the panel open, File > Send To > Premiere Pro.
    **Pass:** Premiere opens a new sequence in the *Zenvi Imports* bin; Zenvi says "Opened “<name>” in Premiere
    Pro"; `<project>_assets/premiere/<time> <name>/` holds the XML and the title PNG; Edit > Undo once in Premiere
    removes the import.
11. In Premiere, edit the sequence (trim a clip, add a Cross Dissolve, move the logo, add a marker) and press
    **Send to Zenvi** in the panel.
    **Pass:** Zenvi adds the sequence on new tracks above the existing ones, the edits match Premiere, and one
    Edit > Undo in Zenvi removes all of it.
12. In Premiere, File > Export > Final Cut Pro XML... for a sequence with a nested sequence, a disabled clip and a
    variable-speed clip. Move the XML to another computer or folder, without its media. In Zenvi, File > Import
    Project > Premiere Pro XML... and pick it.
    **Pass:** Zenvi asks for the first missing file (*Browse...*); pointing at the media folder finds the rest; the
    nest's clips come in on extra tracks; the disabled clip is hidden; the report lists the variable speed and the
    flattened nest; one Undo removes the import.
13. Repeat step 12 and press Cancel when Zenvi asks for the media.
    **Pass:** "Import failed: no clip of '<sequence>' could be imported: missing media ..." and no undo step.
14. File > Export Project > Export XML (Final Cut Pro) and import that file into Premiere.
    **Pass:** it opens like step 3.

## Tests

From the worktree root:

```bash
.venv/bin/python -m pytest tests/test_premiere_export.py tests/test_premiere_import.py \
    tests/test_editor_tools_handoff_premiere.py -q
ZENVI_REAL_QT=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_premiere_qt.py -q
QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \
    .venv/bin/python -m pytest tests/test_premiere_libopenshot.py -q
```

- `tests/premiere_fakes.py` builds project dicts for a real `TimelineSnapshot`, a stand-in for the QSvgRenderer
  step, and a media probe.
- `tests/fixtures/premiere/` holds the golden export (`ZENVI_UPDATE_GOLDEN=1` rewrites it), Premiere-style
  fixtures (promo, speed, nested, a legacy Zenvi export), and OpenTimelineIO's Apache-2.0 `premiere_example.xml`
  with its attribution header.
- `test_premiere_qt.py` renders SVG titles to PNG with real Qt; `test_premiere_libopenshot.py` renders an imported
  sequence with libopenshot 1.0 and checks where the logo lands.
