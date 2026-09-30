"""Timeline-edit tools that change timing and sound: speed, reverse, repeat, freeze, separate audio,
audio/video on-off and waveforms.

Each one validates, then runs the editor's own Clip menu handler (Time_Triggered,
Repeat_Triggered, Split_Audio_Triggered, Show/Hide_Waveform_Triggered), which joins
the tool call's undo step.
"""

from __future__ import annotations

from classes import timeline_ops
from classes.editor_tools._base import (
    CLIP_TARGET,
    CLIPS_TARGET,
    CONSTANT,
    ToolError,
    boolean,
    enum,
    get_app,
    integer,
    keyframe,
    number,
    obj,
    ok,
    project_fps,
    resolve_layer,
    string,
    timeline_ui,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.timeline_edit_common import (
    all_clip_ids,
    check_overlaps,
    clip_summary,
    extend_timeline,
    frame_seconds,
    fresh,
    is_image,
    keyframe_value,
    media_has_audio,
    media_has_video,
    new_overlaps,
    r3,
    reader_of,
    refresh,
    require_unlocked,
    save_clip,
    shifted_receipt,
    snap,
    span,
    target,
    targets,
    time_arg,
    title,
    tolerance,
    track_name,
    track_numbers_bottom_up,
)

MIN_SPEED, MAX_SPEED = 0.0625, 16.0
_OVERLAP_ARG = boolean(
    "Let the longer clip overlap the next clip on its track (the receipt lists it). Off by default: the tool "
    "refuses and says what is in the way.", False)
_RIPPLE_ARG = boolean("Shift the later clips on the track by the change in length, so nothing overlaps and no "
                      "gap opens.", False)


def _require_targets(timeline_clip_ids, clip_query, scope, verb):
    if not (timeline_clip_ids or (clip_query or "").strip() or (scope or "").strip()):
        raise ToolError(f"say which clips to {verb}: timeline_clip_ids, clip_query, or scope")


def _layer(clip) -> int:
    return int(clip.data.get("layer") or 0)


def effective_speed(clip_data) -> tuple:
    """(speed relative to the source, 'forward'|'backward'|'looped') from the clip's time curve."""
    if timeline_ops.repeat_is_active(clip_data):
        return None, "looped"
    pts = ((clip_data.get("time") or {}).get("Points")) or []
    try:
        pts = sorted(pts, key=lambda p: float(p["co"]["X"]))
        x0, y0 = float(pts[0]["co"]["X"]), float(pts[0]["co"]["Y"])
        x1, y1 = float(pts[-1]["co"]["X"]), float(pts[-1]["co"]["Y"])
    except (KeyError, TypeError, ValueError, IndexError):
        return 1.0, "forward"
    if len(pts) < 2 or x1 <= x0:
        return 1.0, "forward"
    # Time curves map timeline frames X to source frames Y (both inclusive).
    return (abs(y1 - y0) + 1.0) / (x1 - x0), ("backward" if y1 < y0 else "forward")


def _reset_duration(clip) -> float:
    """Length after Clip > Speed > Reset: from the in point to the end of the source."""
    if is_image(clip):
        return float(get_app().get_settings().get("default-image-length") or 10.0)
    cache = clip.data.get("repeat_cache") if timeline_ops.repeat_is_active(clip.data) else {}
    start = float(cache.get("start", clip.data.get("start")) or 0.0)
    try:
        source = float(reader_of(clip).get("duration") or 0.0)
    except (TypeError, ValueError):
        source = 0.0
    return max(frame_seconds(), source - start) if source > 0 else float(clip.data.get("end") or 0.0) - start


def _frames(seconds) -> float:
    fps = project_fps()
    return max(1, int(round(float(seconds) * fps))) / fps


def _ripple_after(clip, start, old_duration):
    """Move everything that starts after *clip* by its change in length.

    Items starting inside it (a crossfade partner and its transition) move too,
    so overlaps between neighbours survive.
    """
    now = fresh(clip.id)
    new_duration = span(now.data)[1] - span(now.data)[0]
    return timeline_ops.shift_after(_layer(clip), start + tolerance(), new_duration - old_duration,
                                    exclude_ids={clip.id})


def _timing_receipt(clip, before_duration) -> dict:
    now = fresh(clip.id)
    speed, direction = effective_speed(now.data)
    return {"timeline_clip_id": clip.id, "title": title(clip), "duration_before": r3(before_duration),
            "duration_after": r3(span(now.data)[1] - span(now.data)[0]),
            "effective_speed": None if speed is None else round(speed, 3), "direction": direction,
            "position": r3(span(now.data)[0]), "end": r3(span(now.data)[1])}


# ---------------------------------------------------------------------------
# Speed / reverse / reset
# ---------------------------------------------------------------------------

@editor_tool(
    "set_clip_speed_tool",
    label="Change clip speed",
    schema=obj({
        **CLIPS_TARGET,
        "speed": number("Speed factor relative to how the clip plays now: 2 = twice as fast (half as long), "
                        "0.5 = half speed, 1.5, 4... 0 = leave the speed alone.", 0.0, minimum=0, maximum=MAX_SPEED),
        "absolute": boolean("Read speed relative to the original media instead (1 = normal speed, 2 = double "
                            "normal), keeping the same part of the source.", False),
        "target_duration_seconds": number("Retime so the clip lasts exactly this long on the timeline (speeds up "
                                          "or slows down as needed); 0 = unused.", 0.0, minimum=0),
        "reverse": boolean("Flip the playback direction (a reversed clip plays forward again).", False),
        "reset": boolean("Clip > Speed > Reset first: normal speed, forward, loops removed. Like the menu, the clip "
                         "then runs from its in point to the end of its source.", False),
        "ripple": _RIPPLE_ARG,
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.speed",),
)
def set_clip_speed(timeline_clip_ids=[], clip_query="", track="", scope="", speed=0.0, absolute=False,
                   target_duration_seconds=0.0, reverse=False, reset=False, ripple=False, allow_overlap=False):
    """Speed clips up or slow them down, play them backwards, or retime them to an exact length
    (Clip menu > Speed). Keyframes and the audio follow the new timing.

    Use for "speed up the drone shot 2x" (speed=2), "slow motion" (speed=0.5), "play it in
    reverse" (reverse=true), "make this clip exactly 3 seconds" (target_duration_seconds=3),
    "back to normal speed" (speed=1, absolute=true keeps the same part of the source; reset=true
    is the menu's Reset). speed combines with reverse. Speeds from 1/16x to 16x.

    A slower clip gets longer and may run into the next clip on its track: the tool refuses
    unless ripple=true (push later clips along) or allow_overlap=true. Looped clips
    (repeat_clip_tool) need reset=true before a new speed. For a still frame use
    freeze_frame_tool; to loop use repeat_clip_tool. One undo step for all clips. Returns each
    clip's length before/after and its effective speed.
    """
    from windows.views.timeline_backend.enums import MenuTime

    _require_targets(timeline_clip_ids, clip_query, scope, "retime")
    if speed and target_duration_seconds:
        raise ToolError("give speed or target_duration_seconds, not both")
    if absolute and not speed:
        raise ToolError("absolute=true needs a speed (1 = normal speed)")
    if not (speed or target_duration_seconds or reverse or reset):
        raise ToolError("nothing to change: give speed, target_duration_seconds, reverse or reset")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    require_unlocked({_layer(c) for c in clips})

    tol = tolerance()
    plans = []
    for c in clips:
        name = title(c)
        duration = span(c.data)[1] - span(c.data)[0]
        eff, direction = effective_speed(c.data)
        if (speed or target_duration_seconds) and eff is None and not reset:
            raise ToolError(f"{name!r} is looped (repeat_clip_tool); pass reset=true to clear the loop before "
                            "changing its speed")
        base_duration = _reset_duration(c) if reset else duration
        base_speed = 1.0 if reset else eff
        factor = None
        if speed:
            factor = speed / base_speed if absolute else float(speed)
        elif target_duration_seconds:
            factor = base_duration / float(target_duration_seconds)
        if factor is not None:
            if abs(factor - 1.0) < 1e-6:
                factor = None
            elif not MIN_SPEED - 1e-9 <= factor <= MAX_SPEED + 1e-9:
                raise ToolError(f"{name!r}: that is a {factor:.3g}x change; speed changes go from 1/16x to 16x per "
                                "call")
        if factor is None and not reverse and not reset:
            raise ToolError(f"{name!r} already plays at that speed; nothing to change")
        new_duration = _frames(base_duration / factor) if factor else base_duration
        plans.append((c, duration, new_duration, factor))

    hits = []
    if not ripple:
        changes = {c.id: (_layer(c), span(c.data)[0], span(c.data)[0] + nd) for c, _d, nd, _f in plans
                   if nd > _d + tol}
        hits = check_overlaps(new_overlaps(changes), allow_overlap, "The longer clip")

    tl = timeline_ui()
    results, shifted = [], []
    for c, duration, _new_duration, factor in sorted(plans, key=lambda p: -span(p[0].data)[0]):
        start = span(c.data)[0]
        if reset:
            tl.Time_Triggered(MenuTime.NONE, [c.id], "1X", 0.0)
        if factor:
            tl.Time_Triggered(MenuTime.BACKWARD if reverse else MenuTime.FORWARD, [c.id], f"{factor:.6g}X", 0.0)
        elif reverse:
            tl.Time_Triggered(MenuTime.REVERSE, [c.id], "1X", 0.0)
        if ripple:
            shifted += _ripple_after(c, start, duration)
        results.append(_timing_receipt(c, duration))
    results.reverse()
    extend_timeline()
    refresh()
    one = results[0]
    what = ("reset" if reset else "") + (" reversed" if reverse else "")
    speed_note = f"{one['effective_speed']}x {one['direction']}" if one["effective_speed"] is not None else one["direction"]
    return ok(f"Retimed {len(results)} clip(s){(' (' + what.strip() + ')') if what.strip() else ''}: "
              f"{one['duration_before']:.2f} s -> {one['duration_after']:.2f} s, now {speed_note}"
              + (f"; {len(shifted)} later item(s) rippled" if shifted else "")
              + (f"; overlaps {len(hits)} clip(s)" if hits else "") + ".",
              clips=results, shifted=shifted_receipt(shifted), overlaps=hits)


# ---------------------------------------------------------------------------
# Repeat
# ---------------------------------------------------------------------------

@editor_tool(
    "repeat_clip_tool",
    label="Repeat clip",
    schema=obj({
        **CLIPS_TARGET,
        "times": integer("How many times the clip plays in total (2 = the clip then one repeat).", 2,
                         minimum=2, maximum=500),
        "pattern": enum(["loop", "ping_pong"], "loop = start over each time; ping_pong = forward, backward, "
                        "forward...", "loop"),
        "reverse": boolean("Start the first pass backwards.", False),
        "delay_seconds": number("Pause (hold the last frame) between passes.", 0.0, minimum=0, maximum=60),
        "speed_ramp_percent": number("Each pass plays this much faster (+) or slower (-) than the one before, "
                                     "in percent.", 0.0, minimum=-90, maximum=1000),
        "ripple": _RIPPLE_ARG,
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.repeat",),
)
def repeat_clip(timeline_clip_ids=[], clip_query="", track="", scope="", times=2, pattern="loop", reverse=False,
                delay_seconds=0.0, speed_ramp_percent=0.0, ripple=False, allow_overlap=False):
    """Loop a clip's playback N times within one timeline clip (Clip menu > Speed > Repeat).

    Use for "loop the beat 4 times" (times=4), "boomerang this" (pattern='ping_pong', times=3),
    "repeat it with a half-second pause" (delay_seconds=0.5), "loop it, speeding up each time"
    (speed_ramp_percent=25). The clip becomes N times as long (plus delays); the tool refuses
    to run into the next clip unless ripple=true or allow_overlap=true. A clip that is already
    looped must be reset first (set_clip_speed_tool reset=true) or undone. To place separate
    copies instead, use duplicate_clips_tool. One undo step.
    """
    _require_targets(timeline_clip_ids, clip_query, scope, "repeat")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    require_unlocked({_layer(c) for c in clips})
    fps = project_fps()
    delay_frames = int(round(float(delay_seconds) * fps))
    ramp = float(speed_ramp_percent) / 100.0

    plans = []
    for c in clips:
        if timeline_ops.repeat_is_active(c.data):
            raise ToolError(f"{title(c)!r} is already looped; reset it first (set_clip_speed_tool reset=true) or "
                            "undo the earlier repeat")
        duration = span(c.data)[1] - span(c.data)[0]
        span_frames = max(1, int(round(duration * fps)))
        total = sum(max(1, int(round(span_frames / abs((1.0 + ramp) ** k)))) for k in range(int(times)))
        total += (int(times) - 1) * delay_frames
        new_duration = total / fps
        if new_duration > 3 * 3600:
            raise ToolError(f"{title(c)!r} would become {new_duration / 3600:.1f} hours long; use fewer passes or "
                            "a smaller slow-down")
        plans.append((c, duration, new_duration))

    hits = []
    if not ripple:
        changes = {c.id: (_layer(c), span(c.data)[0], span(c.data)[0] + nd) for c, _d, nd in plans}
        hits = check_overlaps(new_overlaps(changes), allow_overlap, "The repeated clip")

    tl = timeline_ui()
    results, shifted = [], []
    for c, duration, _nd in sorted(plans, key=lambda p: -span(p[0].data)[0]):
        start = span(c.data)[0]
        tl.Repeat_Triggered("pingpong" if pattern == "ping_pong" else "loop", -1 if reverse else 1, int(times),
                            [c.id], delay_frames, ramp)
        if ripple:
            shifted += _ripple_after(c, start, duration)
        results.append(_timing_receipt(c, duration))
    results.reverse()
    refresh()
    one = results[0]
    return ok(f"Repeated {len(results)} clip(s) {int(times)}x ({pattern.replace('_', '-')}): "
              f"{one['duration_before']:.2f} s -> {one['duration_after']:.2f} s"
              + (f"; {len(shifted)} later item(s) rippled" if shifted else "")
              + (f"; overlaps {len(hits)} clip(s)" if hits else "") + ".",
              times=int(times), pattern=pattern, clips=results, shifted=shifted_receipt(shifted), overlaps=hits)


# ---------------------------------------------------------------------------
# Freeze frame
# ---------------------------------------------------------------------------

@editor_tool(
    "freeze_frame_tool",
    label="Freeze frame",
    schema=obj({
        **CLIP_TARGET,
        "at_seconds": number("Timeline time of the frame to hold (with frame='at_time'); -1 = the playhead.", -1.0),
        "frame": enum(["at_time", "first", "last"], "Which frame to hold: the one at at_seconds, the clip's first "
                      "frame, or its last frame.", "at_time"),
        "hold_seconds": number("How long the frame stays frozen.", 2.0, minimum=0.1, maximum=60),
        "zoom": boolean("Freeze & Zoom: push in slightly (to 105%) and back during the hold.", False),
        "ripple": _RIPPLE_ARG,
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.freeze",),
)
def freeze_frame(timeline_clip_id="", clip_query="", track="", at_seconds=-1.0, frame="at_time", hold_seconds=2.0,
                 zoom=False, ripple=False, allow_overlap=False):
    """Hold one frame of a clip still for a few seconds, then carry on (Clip menu > Speed > Freeze).

    Use for "freeze on the last frame for 2 seconds" (frame='last'), "freeze here" (the
    playhead), "hold the frame at 12.5 s for 3 seconds with a slight zoom" (zoom=true). The
    clip gets hold_seconds longer and its sound is muted during the hold. The tool refuses to
    run into the next clip unless ripple=true or allow_overlap=true. For slow motion use
    set_clip_speed_tool. One undo step.
    """
    from windows.views.timeline_backend.enums import MenuTime

    c = target(timeline_clip_id, clip_query, track)
    require_unlocked({_layer(c)})
    tol = tolerance()
    fr = frame_seconds()
    start, end = span(c.data)
    if end - start < fr - tol:
        raise ToolError(f"{title(c)!r} is shorter than a frame")
    if frame == "first":
        t = start
    elif frame == "last":
        t = end - fr
    else:
        t = snap(time_arg(at_seconds))
        if not start - tol <= t <= end - fr + tol:
            raise ToolError(f"{t:.2f} s is outside {title(c)!r} ({start:.2f}-{end:.2f} s); give a time inside it or "
                            "frame='first'/'last'")
    t = min(max(snap(t), start), snap(end - fr))
    hold = _frames(hold_seconds)
    duration = end - start

    hits = []
    if not ripple:
        hits = check_overlaps(new_overlaps({c.id: (_layer(c), start, end + hold)}), allow_overlap, "The freeze")
    timeline_ui().Time_Triggered(MenuTime.FREEZE_ZOOM if zoom else MenuTime.FREEZE, [c.id], f"{hold:.6g}", t)
    shifted = _ripple_after(c, start, duration) if ripple else []
    extend_timeline()
    refresh()
    receipt = _timing_receipt(c, duration)
    return ok(f"Froze {title(c)!r} at {t:.2f} s for {hold:.2f} s{' with zoom' if zoom else ''}; it now ends at "
              f"{receipt['end']:.2f} s" + (f"; {len(shifted)} later item(s) rippled" if shifted else "")
              + (f"; overlaps {len(hits)} clip(s)" if hits else "") + ".",
              frozen_at=r3(t), hold_seconds=r3(hold), clip=receipt, shifted=shifted_receipt(shifted), overlaps=hits)


# ---------------------------------------------------------------------------
# Separate audio
# ---------------------------------------------------------------------------

@editor_tool(
    "separate_clip_audio_tool",
    label="Separate audio",
    schema=obj({
        **CLIPS_TARGET,
        "per_channel": boolean("One audio clip per channel (left/right...) on successive tracks below, instead of "
                               "one clip with all channels.", False),
        "to_track": string("Track for the new audio clip: UI track number (1 = bottom), name, or layer number. "
                           "Empty = the track directly below (created when there is none), like the menu.", ""),
        "allow_overlap": boolean("Put the audio clip there even if it overlaps clips already on that track.",
                                 False),
    }),
    covers=("clip.separate_audio",),
)
def separate_clip_audio(timeline_clip_ids=[], clip_query="", track="", scope="", per_channel=False, to_track="",
                        allow_overlap=False):
    """Split a clip's sound onto its own audio-only clip and mute the original (Clip menu > Audio >
    Separate Audio).

    Use for "separate the audio from the talking-head clip", "detach the sound so I can move it",
    "put the left and right channels on their own tracks" (per_channel=true). The new clip covers
    the same time, plays only sound (video off) and goes on the track below (or to_track); the
    original keeps its picture with its audio turned off. Refused for clips without audio, clips
    whose audio is already off, and when the destination is locked or already has clips there
    (unless allow_overlap=true). One undo step. Returns the new audio clip ids.
    """
    from windows.views.timeline_backend.enums import MenuSplitAudio

    _require_targets(timeline_clip_ids, clip_query, scope, "separate")
    if per_channel and (to_track or "").strip():
        raise ToolError("to_track works with per_channel=false; per-channel clips go on the tracks below")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    require_unlocked({_layer(c) for c in clips})
    for c in clips:
        if not media_has_audio(c):
            raise ToolError(f"{title(c)!r} has no audio to separate")
        if keyframe_value(c.data.get("has_audio")) == 0.0:
            raise ToolError(f"{title(c)!r} already has its audio off (separated before?); turn it back on with "
                            "set_clip_audio_video_tool(audio='auto') first")
        if not media_has_video(c) and not per_channel:
            raise ToolError(f"{title(c)!r} is already audio-only; use per_channel=true to split its channels")

    order = track_numbers_bottom_up()
    dest = resolve_layer(to_track) if (to_track or "").strip() else None
    planned = []
    for c in clips:
        s, e = span(c.data)
        try:
            channels = int(reader_of(c).get("channels") or 1)
        except (TypeError, ValueError):
            channels = 1
        if dest is not None:
            lanes = [dest]
        else:
            # The menu uses the existing tracks below and creates new ones past the bottom;
            # per channel, an audio-only clip keeps channel 1 itself.
            idx = order.index(_layer(c)) if _layer(c) in order else 0
            below = (channels if media_has_video(c) else channels - 1) if per_channel else 1
            lanes = [order[idx - k] for k in range(1, below + 1) if idx - k >= 0]
        require_unlocked(lanes, "where the audio would go")
        planned += [(f"audio of {title(c)}", lane, s, e) for lane in lanes]
    check_overlaps(new_overlaps({}, planned), allow_overlap, "The separated audio", ripple_hint=False)

    before = all_clip_ids()
    timeline_ui().Split_Audio_Triggered(MenuSplitAudio.MULTIPLE if per_channel else MenuSplitAudio.SINGLE,
                                        [c.id for c in clips])
    new_ids = sorted(all_clip_ids() - before)
    if dest is not None:
        for nid in new_ids:
            save_clip(fresh(nid), layer=dest)
    if not new_ids and not per_channel:
        raise ToolError("the editor did not create an audio clip")
    refresh()
    created = []
    for nid in new_ids:
        now = fresh(nid)
        info = clip_summary(now)
        info["video"] = "off" if keyframe_value(now.data.get("has_video")) == 0.0 else "auto"
        created.append(info)
    originals = [{"timeline_clip_id": c.id, "title": title(c),
                  "audio": "off" if keyframe_value(fresh(c.id).data.get("has_audio")) == 0.0 else "on"}
                 for c in clips]
    tracks = sorted({str(x["track"]) for x in created})
    return ok(f"Separated the audio of {len(clips)} clip(s) into {len(created)} audio clip(s) on track(s) "
              f"{', '.join(tracks) or '-'}; the originals are muted.", audio_clips=created, originals=originals)


# ---------------------------------------------------------------------------
# Audio / video on-off and waveforms
# ---------------------------------------------------------------------------

_AV = {"on": 1.0, "off": 0.0, "auto": -1.0}


@editor_tool(
    "set_clip_audio_video_tool",
    label="Clip audio/video on-off",
    schema=obj({
        **CLIPS_TARGET,
        "audio": enum(["", "on", "off", "auto"], "Clip sound: off = mute the clip entirely, on = force on, "
                      "auto = as the media has it (Properties > Enable Audio). '' = leave it.", ""),
        "video": enum(["", "on", "off", "auto"], "Clip picture: off = sound only (audio-only clip), on/auto = show "
                      "the picture (Properties > Enable Video). '' = leave it.", ""),
        "waveform": enum(["", "show", "hide"], "Draw the clip's audio waveform on the timeline, or hide it.", ""),
    }),
    covers=("clip.enable_av", "clip.waveform"),
)
def set_clip_audio_video(timeline_clip_ids=[], clip_query="", track="", scope="", audio="", video="", waveform=""):
    """Turn a clip's sound or picture on or off, or show/hide its waveform on the timeline.

    Use for "mute this clip" (audio='off'), "unmute it" (audio='auto'), "make it audio only" /
    "hide the video but keep the sound" (video='off'), "show the waveform" (waveform='show').
    This switches the track of sound or picture entirely; to change how loud a clip is use
    set_clip_volume_tool, and to get the sound onto its own clip use separate_clip_audio_tool.
    Refused on locked tracks and for 'on'/'show' when the media has no audio/video. Already in
    that state is not an error (no undo step). One undo step. Waveforms are drawn a moment later.
    """
    _require_targets(timeline_clip_ids, clip_query, scope, "change")
    if not (audio or video or waveform):
        raise ToolError("nothing to change: give audio, video or waveform")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    require_unlocked({_layer(c) for c in clips})
    for c in clips:
        if audio == "on" and not media_has_audio(c):
            raise ToolError(f"{title(c)!r} has no audio in its media")
        if waveform == "show" and not media_has_audio(c):
            raise ToolError(f"{title(c)!r} has no audio, so it has no waveform")
        if video == "on" and not media_has_video(c):
            raise ToolError(f"{title(c)!r} has no picture in its media")

    changed = []
    for c in clips:
        fields = {}
        if audio and keyframe_value(c.data.get("has_audio")) != _AV[audio]:
            fields["has_audio"] = keyframe([(1, _AV[audio], CONSTANT)])
        if video and keyframe_value(c.data.get("has_video")) != _AV[video]:
            fields["has_video"] = keyframe([(1, _AV[video], CONSTANT)])
        if fields:
            save_clip(c, **fields)
            changed.append(c.id)

    def _has_wave(c):
        data = (c.data.get("ui") or {}).get("audio_data")
        return isinstance(data, list) and len(data) > 0

    wave_ids = []
    if waveform == "show":
        wave_ids = [c.id for c in clips if not _has_wave(c)]
        if wave_ids:
            timeline_ui().Show_Waveform_Triggered(wave_ids, transaction_id=get_app().updates.transaction_id)
    elif waveform == "hide":
        wave_ids = [c.id for c in clips if _has_wave(c)]
        if wave_ids:
            timeline_ui().Hide_Waveform_Triggered(wave_ids)
    if not changed and not wave_ids:
        return ok("Nothing changed: the clip(s) were already set that way.", changed=False, clips=[])
    refresh()
    states = []
    for c in clips:
        now = fresh(c.id)
        states.append({"timeline_clip_id": c.id, "title": title(c), "track": ui_track_number(_layer(c)),
                       "audio": {1.0: "on", 0.0: "off"}.get(keyframe_value(now.data.get("has_audio")), "auto"),
                       "video": {1.0: "on", 0.0: "off"}.get(keyframe_value(now.data.get("has_video")), "auto")})
    parts = []
    if audio:
        parts.append(f"audio {audio}")
    if video:
        parts.append(f"video {video}")
    if waveform:
        parts.append(f"waveform {waveform}" + (" (drawing)" if waveform == "show" and wave_ids else ""))
    return ok(f"Set {', '.join(parts)} on {len(clips)} clip(s) on {track_name(_layer(clips[0]))}.",
              clips=states, waveform_requested=wave_ids if waveform == "show" else [],
              waveform_hidden=wave_ids if waveform == "hide" else [])
