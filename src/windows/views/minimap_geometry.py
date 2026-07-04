"""
 @file
 @brief Pure geometry helpers for the timeline overview/zoom slider (minimap).

 This module is deliberately free of any Qt or libopenshot imports so the
 minimap rect computation can be unit tested headlessly and recomputed off the
 main thread. Rects are returned as plain (x, y, width, height) float tuples;
 the ZoomSlider widget converts them to QRectF at paint time.
"""


def _resolve_field(item, overrides, field, default=0.0):
    """Return an item field, preferring a live drag override when present."""
    item_id = item.get("id")
    override = overrides.get(item_id) if overrides else None
    if override is not None and field in override and override[field] is not None:
        return override[field]
    return item.get(field, default)


def _item_rect(item, overrides, layer_index, pixels_per_second, vertical_factor):
    """Compute a single (x, y, width, height) rect for a clip/transition."""
    position = float(_resolve_field(item, overrides, "position", 0.0) or 0.0)
    start = float(_resolve_field(item, overrides, "start", 0.0) or 0.0)
    end = float(_resolve_field(item, overrides, "end", 0.0) or 0.0)
    layer = _resolve_field(item, overrides, "layer", 0)

    x = position * pixels_per_second
    width = max(0.0, (end - start)) * pixels_per_second
    row = layer_index.get(layer, 0)
    y = row * vertical_factor
    return (x, y, width, vertical_factor)


def compute_minimap_rects(
    clips,
    transitions,
    markers,
    selected_ids,
    layer_index,
    width,
    height,
    duration,
    overrides=None,
):
    """Compute minimap geometry from plain project data.

    Args:
        clips/transitions: iterables of dicts with id/position/layer/start/end.
        markers: iterable of dicts with position.
        selected_ids: set/collection of selected clip & transition ids.
        layer_index: dict mapping layer number -> row index (0 at top).
        width/height: pixel size of the minimap widget.
        duration: project duration in seconds.
        overrides: optional dict {item_id: {position/layer/start/end}} applied
            live during a drag so the minimap mirrors the timeline track without
            waiting for the change to be persisted.

    Returns a dict with keys: clip_rects, clip_rects_selected, marker_rects.
    Each rect is an (x, y, width, height) tuple of floats.
    """
    overrides = overrides or {}
    selected = set(selected_ids or ())

    result = {"clip_rects": [], "clip_rects_selected": [], "marker_rects": []}

    duration = float(duration or 0.0)
    width = float(width or 0.0)
    height = float(height or 0.0)
    if duration <= 0.0 or width <= 0.0:
        return result

    pixels_per_second = width / duration
    layer_count = len(layer_index) if layer_index else 1
    vertical_factor = height / max(1, layer_count)

    for item in clips or ():
        rect = _item_rect(item, overrides, layer_index, pixels_per_second, vertical_factor)
        if item.get("id") in selected:
            result["clip_rects_selected"].append(rect)
        else:
            result["clip_rects"].append(rect)

    for item in transitions or ():
        rect = _item_rect(item, overrides, layer_index, pixels_per_second, vertical_factor)
        if item.get("id") in selected:
            result["clip_rects_selected"].append(rect)
        else:
            result["clip_rects"].append(rect)

    marker_height = max(1, layer_count) * vertical_factor
    for marker in markers or ():
        position = float(_resolve_field(marker, overrides, "position", 0.0) or 0.0)
        marker_x = position * pixels_per_second
        result["marker_rects"].append((marker_x, 0.0, 0.5, marker_height))

    return result
