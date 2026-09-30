"""Prepare a recording: open and configure the Recording dock (View > Recording View).

Workstream: ai-generation. ``prepare_recording_tool`` does what a person does in the
Recording dock before pressing Start Recording: pick the sources (mic / screen /
webcam), devices, formats, the screen area, the webcam layout, the target track, the
start time and the timeline preview. It drives the dock's own widgets and methods
(``AudioRecordingDockContent``), so the dock looks exactly as if the person had set it
up. Starting a capture stays a human action (coverage OUT_OF_SCOPE
``recording.capture_start``): the tool never presses Record.

Selecting the mic starts the dock's input level meter and selecting the webcam its live
preview, as clicking those cards does; nothing is recorded until the person presses
Start Recording. Dock settings are UI state, not project data: no undo step.
"""

from __future__ import annotations

from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    enum,
    get_app,
    integer,
    mapping,
    nullable,
    number,
    obj,
    ok,
    on_main,
    resolve_clip,
    resolve_layer,
    seconds_to_frame,
    snap_seconds,
    string,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool

SOURCES = ("mic", "screen", "webcam")
PREVIEWS = {"off": "none", "full": "full", "half": "half", "quarter": "quarter"}
WEBCAM_LAYOUTS = ("bottom-right", "top-right", "bottom-left", "top-left", "left", "right", "center", "full")
WEBCAM_CORNERS = {"rectangle": 0.0, "rounded": 0.15, "oval": 0.5}
WEBCAM_SIZES = (0.2, 0.3, 0.4)
AUDIO_FORMATS = ("wav", "flac", "mp3")
SAMPLE_RATES = (44100, 48000, 96000)
SCREEN_FPS = (15, 24, 30, 60)
NO_TRACK = "__no_recording_track__"      # windows.audio_recording.NO_RECORDING_TRACK
PREPARE_TIMEOUT = 120                    # the Recording View rearranges docks and pumps events
NEXT_STEP = "Press Start Recording in the Recording panel when ready (the assistant cannot start a capture)."


# ---------------------------------------------------------------------------
# Reading the dock
# ---------------------------------------------------------------------------

def _items(combo) -> list:
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def _pick(combo, wanted: str, what: str, *, by_data=False) -> int:
    """Index of the combo item whose text (or data) matches *wanted* (exact, then unique substring)."""
    items = _items(combo)
    want = str(wanted).strip().lower()
    for i, (text, data) in enumerate(items):
        if str(text).strip().lower() == want or (by_data and str(data).strip().lower() == want):
            return i
    hits = [i for i, (text, _d) in enumerate(items) if want and want in str(text).strip().lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(str(t) for t, _d in items) or "none found"
    if len(hits) > 1:
        raise ToolError(f"several {what}s match {wanted!r}: {names}; name one exactly")
    raise ToolError(f"no {what} matches {wanted!r}; available: {names}")


def _card(dock, source: str):
    return {"mic": dock.mic_card, "screen": dock.screen_card, "webcam": dock.camera_card}[source]


def _available(dock, source: str) -> tuple:
    card = _card(dock, source)
    available = bool(getattr(card, "_available", True)) and bool(card.isEnabled())
    reason = str(card.toolTip() or "") if not available else ""
    if not available and not reason:
        reason = f"{source} recording is not available on this computer"
    return available, reason


def _screen_mode(dock) -> str:
    if dock.region_button.isChecked():
        return "region"
    if dock.window_button.isChecked():
        return "window"
    return "full_screen"


def _track_report(dock) -> dict:
    data = dock.track_combo.currentData()
    if data == NO_TRACK:
        return {"track": "none", "name": "No Track (Project Files only)"}
    try:
        layer = int(data)
    except (TypeError, ValueError):
        return {"track": None, "name": dock.track_combo.currentText()}
    return {"track": ui_track_number(layer), "name": dock.track_combo.currentText(), "layer": layer}


def dock_report(dock) -> dict:
    """The dock's configuration as the person sees it."""
    sources = {}
    for source in SOURCES:
        available, reason = _available(dock, source)
        sources[source] = {"selected": bool(_card(dock, source).isChecked()), "available": available}
        if reason:
            sources[source]["reason"] = reason
    report = {"sources": sources}
    if sources["mic"]["selected"]:
        report["mic"] = {
            "device": dock.device_combo.currentText(),
            "devices": [t for t, _d in _items(dock.device_combo)],
            "channels": "stereo" if int(dock._channels) == 2 else "mono",
            "format": str(dock.format_combo.currentData() or dock._preferred_format),
            "sample_rate": int(dock._sample_rate),
        }
    if sources["screen"]["selected"]:
        report["screen"] = {
            "screen": dock.screen_display_edit.currentText(),
            "screens": [t for t, _d in _items(dock.screen_display_edit)],
            "mode": _screen_mode(dock),
            "area": {"x": int(dock.screen_x_spin.value()), "y": int(dock.screen_y_spin.value()),
                     "width": int(dock.screen_width_spin.value()), "height": int(dock.screen_height_spin.value())},
            "fps": int(dock.video_fps_combo.currentData() or 30),
            "system_audio": bool(dock.system_audio_combo.currentData()),
            "system_audio_available": bool(dock._system_audio_available()),
            "show_cursor": bool(dock.capture_cursor_combo.currentData()),
            "hide_zenvi": bool(dock.hide_openshot_combo.currentData()),
        }
    if sources["webcam"]["selected"]:
        size = dock.camera_size_combo.currentData()
        corner = float(dock.webcam_corner_radius_combo.currentData() or 0.0)
        report["webcam"] = {
            "device": dock.camera_combo.currentText(),
            "devices": [t for t, _d in _items(dock.camera_combo)],
            "resolution": "%sx%s" % tuple(size) if size else "",
            "fps": dock.camera_fps_combo.currentData(),
            "layout": dock.webcam_layout_combo.currentData(),
            "size": dock.webcam_layout_size_combo.currentData(),
            "corners": next((k for k, v in WEBCAM_CORNERS.items() if abs(v - corner) < 1e-6), str(corner)),
        }
    report["track"] = _track_report(dock)
    start = dock._context_start
    report["start_seconds"] = None if start is None else round(float(start), 3)
    report["start"] = "playhead when Record is pressed" if start is None else f"{float(start):.2f} s"
    report["preview"] = next((k for k, v in PREVIEWS.items() if v == dock.preview_combo.currentData()), "")
    report["record_button"] = dock.record_button.text()
    selected_ok = [s for s, row in sources.items() if row["selected"] and row["available"]]
    report["ready_to_record"] = bool(selected_ok) and bool(dock.record_button.isEnabled())
    report["next_step"] = NEXT_STEP if report["ready_to_record"] else "Select at least one available source."
    return report


# ---------------------------------------------------------------------------
# Configuring it (GUI thread)
# ---------------------------------------------------------------------------

def _set_data(dock, combo, value) -> None:
    dock._set_combo_data(combo, value)


def _prepare(sources, track, start_seconds, timeline_clip_id, mic_device, channels, audio_format, sample_rate,
             screen, screen_region, screen_fps, system_audio, show_cursor, hide_zenvi, webcam_device,
             webcam_resolution, webcam_fps, webcam_layout, webcam_size, webcam_corners, preview,
             recording_view) -> dict:
    win = get_app().window
    win._ensure_audio_recording_dock_content()
    dock = win.audio_recording_content
    if dock._recording or dock._starting:
        raise ToolError("a recording is in progress; stop it in the Recording panel before changing the setup")

    # ---- validate everything before touching the dock -------------------------------------------
    requested = list(dict.fromkeys(sources or []))
    for source in requested:
        available, reason = _available(dock, source)
        if not available:
            raise ToolError(f"{source}: {reason}")
    final = requested if sources else [s for s in SOURCES if _card(dock, s).isChecked()]
    if preview and preview != "off" and "screen" in final:
        raise ToolError("the timeline preview is always off while the screen is recorded (the dock disables it)")
    if system_audio and not dock._system_audio_available():
        raise ToolError("system audio recording is not available on this computer or libopenshot build")
    mic_opts = mic_device or channels or audio_format or sample_rate
    if mic_opts and "mic" not in final:
        raise ToolError("mic_device, channels, audio_format and sample_rate need the mic source (sources=['mic'])")
    screen_opts = screen or screen_region or screen_fps or system_audio is not None or show_cursor is not None \
        or hide_zenvi is not None
    if screen_opts and "screen" not in final:
        raise ToolError("screen settings need the screen source (sources=['screen'])")
    cam_opts = webcam_device or webcam_resolution or webcam_fps or webcam_layout or webcam_size or webcam_corners
    if cam_opts and "webcam" not in final:
        raise ToolError("webcam settings need the webcam source (sources=['webcam'])")
    if sample_rate and int(sample_rate) not in SAMPLE_RATES:
        raise ToolError(f"sample_rate must be one of {', '.join(map(str, SAMPLE_RATES))}")
    if screen_fps and int(screen_fps) not in SCREEN_FPS:
        raise ToolError(f"screen_fps must be one of {', '.join(map(str, SCREEN_FPS))}")
    if webcam_size and not any(abs(float(webcam_size) - s) < 1e-6 for s in WEBCAM_SIZES):
        raise ToolError("webcam_size must be 0.2, 0.3 or 0.4 (the corner layouts' 20/30/40%)")

    mic_index = None
    if mic_device:
        dock.refresh_devices()
        mic_index = _pick(dock.device_combo, mic_device, "microphone")
    screen_index = None
    if screen:
        screen_index = _pick(dock.screen_display_edit, screen, "screen")
    region = None
    if screen_region:
        try:
            region = {k: int(round(float(screen_region[k]))) for k in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            raise ToolError("screen_region needs x, y, width and height (screen pixels)") from None
        if region["width"] < 16 or region["height"] < 16:
            raise ToolError("screen_region must be at least 16x16 pixels")
    cam_index = size_index = None
    if webcam_device or webcam_resolution or webcam_fps:
        if not dock._camera_devices_refreshed:
            dock.refresh_cameras()
        if webcam_device:
            cam_index = _pick(dock.camera_combo, webcam_device, "webcam", by_data=True)

    # track / start (the clip menu's Audio > Record rule for timeline_clip_id)
    track_number = None
    start = None if start_seconds is None else snap_seconds(float(start_seconds))
    clip_track = None
    if timeline_clip_id:
        from classes.query import Clip, Track
        from classes.recording_placement import recording_track_for_clip
        clip = resolve_clip(timeline_clip_id)
        if start is None:
            start = snap_seconds(max(0.0, float(clip.data.get("position") or 0.0)))
        clip_track = recording_track_for_clip(clip.data, [t.data for t in Track.filter()],
                                              [c.data for c in Clip.filter()])
    if track:
        if str(track).strip().lower() in ("none", "no track", "no_track", "files", "project files"):
            track_number = NO_TRACK
        else:
            track_number = resolve_layer(track)
            locked = [t for t in (get_app().project.get("layers") or []) if int(t.get("number") or 0) == track_number
                      and t.get("lock")]
            if locked:
                raise ToolError(f"track {ui_track_number(track_number)} is locked; unlock it or pick another track")
    elif clip_track is not None:
        track_number = clip_track

    # ---- apply ---------------------------------------------------------------------------------------
    if recording_view:
        win.actionAudio_Recording_View_trigger()
    win.show_audio_recording_dock(start_time=start, track_number=track_number)
    if start is not None:
        win.SeekSignal.emit(seconds_to_frame(start))      # with Preview on, the dock records at the playhead

    if sources:
        for source in SOURCES:
            _card(dock, source).setChecked(source in requested)
    if mic_index is not None:
        dock.device_combo.setCurrentIndex(mic_index)
    if channels:
        wanted = 2 if channels == "stereo" else 1
        button = dock.stereo_button if wanted == 2 else dock.mono_button
        if not button.isEnabled():
            raise ToolError(f"this microphone does not record in {channels}")
        dock._set_channels(wanted)
    if audio_format:
        _set_data(dock, dock.format_combo, audio_format)
        dock._set_format(audio_format)
    if sample_rate:
        dock.sample_rate_combo.blockSignals(True)
        _set_data(dock, dock.sample_rate_combo, int(sample_rate))
        dock.sample_rate_combo.blockSignals(False)
        dock._set_sample_rate(int(sample_rate))
    if screen_index is not None:
        dock.screen_display_edit.setCurrentIndex(screen_index)
        if not region:
            dock._select_full_screen()
    if region:
        dock.region_button.setChecked(True)
        dock.full_screen_button.setChecked(False)
        dock.window_button.setChecked(False)
        dock._screen_window_id = ""
        dock._set_screen_target(region["x"], region["y"], region["width"], region["height"],
                                "Region: %(width)sx%(height)s" % region)
    if screen_fps:
        _set_data(dock, dock.video_fps_combo, int(screen_fps))
    if system_audio is not None:
        _set_data(dock, dock.system_audio_combo, bool(system_audio))
    if show_cursor is not None:
        _set_data(dock, dock.capture_cursor_combo, bool(show_cursor))
    if hide_zenvi is not None:
        _set_data(dock, dock.hide_openshot_combo, bool(hide_zenvi))
        dock._hide_openshot_user_set = True
    if cam_index is not None:
        dock.camera_combo.setCurrentIndex(cam_index)
    if webcam_resolution:
        size_index = _pick(dock.camera_size_combo, webcam_resolution.replace("×", "x").replace(" ", "").lower()
                           .replace("x", " x "), "webcam resolution")
        dock.camera_size_combo.setCurrentIndex(size_index)
    if webcam_fps:
        fps_items = [d for _t, d in _items(dock.camera_fps_combo)]
        if int(webcam_fps) not in fps_items:
            raise ToolError(f"the webcam does not offer {webcam_fps} fps at this resolution; it offers "
                            f"{', '.join(map(str, fps_items)) or 'none'}")
        _set_data(dock, dock.camera_fps_combo, int(webcam_fps))
    if webcam_layout:
        _set_data(dock, dock.webcam_layout_combo, webcam_layout)
    if webcam_size:
        _set_data(dock, dock.webcam_layout_size_combo, float(webcam_size))
    if webcam_corners:
        _set_data(dock, dock.webcam_corner_radius_combo, WEBCAM_CORNERS[webcam_corners])
    if preview:
        _set_data(dock, dock.preview_combo, PREVIEWS[preview])
    dock._sync_backend_state()
    report = dock_report(dock)
    report["view"] = "recording" if recording_view else "recording panel"
    if timeline_clip_id:
        report["over_clip"] = str(timeline_clip_id)
    return report


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

_REGION = {"type": "object", "properties": {
    "x": {"type": "number", "description": "Left edge in screen pixels."},
    "y": {"type": "number", "description": "Top edge in screen pixels."},
    "width": {"type": "number", "description": "Width in screen pixels (>= 16)."},
    "height": {"type": "number", "description": "Height in screen pixels (>= 16)."}},
    "required": ["x", "y", "width", "height"], "additionalProperties": False}


@editor_tool(
    "prepare_recording_tool",
    label="Prepare recording",
    schema=obj({
        "sources": array({"type": "string", "enum": list(SOURCES)}, "What to record: mic (voice), screen, webcam; "
                         "any combination. Empty = keep the dock's current choice."),
        "track": string("Track for the recording: UI track number (1 = bottom), name or layer number; extra "
                        "sources go on the tracks below it. 'none' = add to Project Files only. Empty = keep.", ""),
        "start_seconds": nullable(number("Timeline seconds where the recording will be placed (also moves the "
                                         "playhead there). Omit = wherever the playhead is when Record is "
                                         "pressed.", minimum=0)),
        "timeline_clip_id": string("Record over this clip, like its menu's Audio > Record: starts at the clip, "
                                   "on the nearest free unlocked track below it (track/start_seconds override).",
                                   ""),
        "mic_device": string("Microphone by name (the dock's Input list); empty = keep.", ""),
        "channels": enum(["", "mono", "stereo"], "Microphone channels.", ""),
        "audio_format": enum(["", *AUDIO_FORMATS], "Microphone file format (the dock defaults to flac).", ""),
        "sample_rate": integer("Microphone sample rate: 44100, 48000 or 96000 (0 = keep).", 0, minimum=0),
        "screen": string("Which screen to capture, by its name in the dock's Screen list; empty = keep.", ""),
        "screen_region": nullable(mapping("Capture only this area of the screen (Region mode): {x, y, width, "
                                          "height} in screen pixels. Omit for the full screen.",
                                          _REGION["properties"], required=_REGION["required"],
                                          additionalProperties=False)),
        "screen_fps": integer("Screen capture frame rate: 15, 24, 30 or 60 (0 = keep).", 0, minimum=0),
        "system_audio": nullable(boolean("Screen: also record the computer's sound output.")),
        "show_cursor": nullable(boolean("Screen: include the mouse cursor.")),
        "hide_zenvi": nullable(boolean("Screen: hide the Zenvi window while recording a window or region.")),
        "webcam_device": string("Webcam by name; empty = keep.", ""),
        "webcam_resolution": string("Webcam resolution such as '1280x720' (from the dock's list); empty = keep.",
                                    ""),
        "webcam_fps": integer("Webcam recording frame rate (0 = keep).", 0, minimum=0),
        "webcam_layout": enum(["", *WEBCAM_LAYOUTS], "Where the webcam sits over a screen recording.", ""),
        "webcam_size": number("Webcam corner size over a screen recording: 0.2, 0.3 or 0.4 of the frame (0 = keep).",
                              0, minimum=0, maximum=0.4),
        "webcam_corners": enum(["", *WEBCAM_CORNERS], "Webcam frame shape.", ""),
        "preview": enum(["", *PREVIEWS], "Play the timeline while recording: off, full, half or quarter resolution "
                        "(always off when recording the screen).", ""),
        "recording_view": boolean("Also switch the editor to the Recording View layout (View > Recording View).",
                                  False),
    }),
    background_safe=True,
    covers=("recording.prepare",),
)
def prepare_recording(sources=None, track="", start_seconds=None, timeline_clip_id="", mic_device="", channels="",
                      audio_format="", sample_rate=0, screen="", screen_region=None, screen_fps=0,
                      system_audio=None, show_cursor=None, hide_zenvi=None, webcam_device="", webcam_resolution="",
                      webcam_fps=0, webcam_layout="", webcam_size=0, webcam_corners="", preview="",
                      recording_view=False):
    """Open the Recording panel set up for a voiceover, screen recording or webcam recording, ready for the person to press Record.

    Use for "set up a voiceover for this clip", "get ready to record my screen with my webcam
    in the corner", "record narration onto track 3 at 0:30". It selects the sources, devices,
    formats, screen area, webcam layout, target track and start time in the Recording dock and
    returns the resulting setup (ready_to_record, what is selected, the available devices and
    screens). It never starts recording: the person presses Start Recording, and the clip
    lands on the timeline when they stop. Choosing mic or webcam turns on the dock's level
    meter / camera preview as clicking them does. No undo step. Example:
    {"sources": ["mic"], "timeline_clip_id": "C7"} -> voiceover over clip C7 on the free
    track below it.
    """
    report = on_main(lambda: _prepare(
        list(sources or []), track, start_seconds, timeline_clip_id, mic_device, channels, audio_format,
        sample_rate, screen, screen_region, screen_fps, system_audio, show_cursor, hide_zenvi, webcam_device,
        webcam_resolution, webcam_fps, webcam_layout, webcam_size, webcam_corners, preview, recording_view),
        timeout=PREPARE_TIMEOUT)
    chosen = [s for s, row in report["sources"].items() if row["selected"]]
    where = report["track"]
    target = "Project Files only" if where.get("track") == "none" else \
        f"track {where.get('track')}" + (f" ({where.get('name')})" if where.get("name") else "")
    head = (f"Recording panel ready: {', '.join(chosen) or 'no source selected'} -> {target}, start "
            f"{report['start']}. " + report["next_step"])
    return ok(head, **report)
