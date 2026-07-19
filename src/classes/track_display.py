"""Map between UI track refs and OpenShot layer numbers (authoritative z-order).

Z-order comes ONLY from layer ``number`` (higher = drawn on top / covers lower).
Track ``label`` / display name is cosmetic and may be anything the user typed
(e.g. bottom track labeled \"5\", top labeled \"1\").
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


def layers_sorted_by_number(layers):
    """Ascending by layer number; bottom of the stack is first (lowest number)."""
    if not layers:
        return []
    return sorted(layers, key=lambda L: int(L.get("number") or 0))


def display_index_to_layer_number(display_1based: int, layers) -> int:
    """1-based stack index from the bottom: 1 = lowest layer number."""
    asc = layers_sorted_by_number(layers)
    if not asc or not (1 <= display_1based <= len(asc)):
        raise ValueError(f"display track index {display_1based} out of range")
    return int(asc[display_1based - 1].get("number") or 0)


def layer_number_to_display_index(layer_num: int, layers) -> int | None:
    """Return 1-based bottom-up stack index for this layer number, or None."""
    asc = layers_sorted_by_number(layers)
    try:
        idx = next(
            i
            for i, L in enumerate(asc)
            if int(L.get("number") or 0) == int(layer_num)
        )
    except StopIteration:
        return None
    return idx + 1


def build_track_stack(layers) -> List[Dict[str, Any]]:
    """
    Live editor track stack, bottom → top.

    Each entry:
      layer_number  — OpenShot storage id (z-order key; higher covers lower)
      ui_track      — 1-based index from bottom (NOT the label)
      z_from_bottom — 0 = bottom, N-1 = top
      label         — user-visible name (cosmetic; may be any string)
      track_id      — project layer id when present
    """
    asc = layers_sorted_by_number(layers or [])
    out: List[Dict[str, Any]] = []
    for i, L in enumerate(asc):
        label = (L.get("label") or L.get("name") or "").strip()
        out.append(
            {
                "layer_number": int(L.get("number") or 0),
                "ui_track": i + 1,
                "z_from_bottom": i,
                "label": label,
                "track_id": str(L.get("id") or ""),
            }
        )
    return out


def track_stack_json(layers) -> str:
    """Compact JSON for agents / plan executor (single line)."""
    return json.dumps(build_track_stack(layers), separators=(",", ":"))


def normalize_track_or_layer_arg(raw: str, layers) -> tuple[int | None, str | None]:
    """
    Resolve user/LLM track input to storage layer number against LIVE layers.

    Accepts (in order):
      1) exact layer_number
      2) exact track_id (e.g. L4 from TRACK_STACK_JSON)
      3) exact label / name (case-insensitive; unique) — labels may be numeric
      4) ui_track index 1..N counted from the BOTTOM of the stack

    Labels are never used for z-order — only to find which layer the user meant.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    layers = layers or []
    if not layers:
        return None, "Error: No tracks in project."

    stack = build_track_stack(layers)
    numbers = {e["layer_number"] for e in stack}

    try:
        n = int(float(raw))
    except ValueError:
        n = None

    # 1) Real layer_number
    if n is not None and n in numbers:
        return n, None

    # 2) track_id from TRACK_STACK_JSON (agents often pass track_id=L4)
    tid_query = raw.strip()
    tid_hits = [
        e for e in stack
        if (e.get("track_id") or "").strip()
        and (e.get("track_id") or "").strip().lower() == tid_query.lower()
    ]
    if len(tid_hits) == 1:
        return int(tid_hits[0]["layer_number"]), None

    # 3) Label / name (including numeric labels like "1" / "5")
    low = raw.lower()
    m = re.match(r"^(?:ui\s*)?track\s*[#:]?\s*(.+)$", low, re.I)
    label_query = (m.group(1).strip() if m else low).strip()

    label_hits = [
        e for e in stack
        if (e.get("label") or "").strip().lower() == label_query
        or (e.get("label") or "").strip().lower() == low
    ]
    if len(label_hits) == 1:
        return int(label_hits[0]["layer_number"]), None
    if len(label_hits) > 1:
        return None, (
            f"Error: Ambiguous track label {raw!r} matches multiple layers. "
            "Pass layer_number from list_layers_tool / TRACK_STACK_JSON."
        )

    contains = [
        e for e in stack
        if label_query and label_query in (e.get("label") or "").strip().lower()
    ]
    if len(contains) == 1:
        return int(contains[0]["layer_number"]), None

    # 4) ui_track from bottom
    if n is not None and layers and 1 <= n <= len(stack):
        return int(stack[n - 1]["layer_number"]), None

    return (
        None,
        f"Error: Unknown track or layer {raw!r}. "
        "Call list_layers_tool and use layer_number (preferred), track_id, "
        "ui_track (1=bottom), or the exact label.",
    )


def resolve_track_arg_to_layer_number(
    raw: str,
    stack: List[Dict[str, Any]],
) -> Optional[int]:
    """Resolve a plan/tool track arg against a previously fetched TRACK_STACK."""
    if not stack:
        return None
    raw = (raw or "").strip()
    if not raw:
        return None

    # Fake layers list for normalize
    fake_layers = [
        {
            "number": e["layer_number"],
            "label": e.get("label") or "",
            "id": e.get("track_id") or "",
        }
        for e in stack
    ]
    resolved, err = normalize_track_or_layer_arg(raw, fake_layers)
    if err or resolved is None:
        return None
    return int(resolved)


def format_track_label_for_llm(layer_num: int, layers) -> str:
    """e.g. '1000000 (stack#1 from bottom, label=Hero)'."""
    stack = build_track_stack(layers)
    for e in stack:
        if e["layer_number"] == int(layer_num):
            lab = e.get("label") or ""
            lab_part = f", label={lab!r}" if lab else ""
            return (
                f"{layer_num} (stack_from_bottom={e['ui_track']}, "
                f"z_from_bottom={e['z_from_bottom']}{lab_part})"
            )
    return str(layer_num)


def parse_track_stack_from_tool_text(text: str) -> List[Dict[str, Any]]:
    """Extract TRACK_STACK_JSON=[...] from list_layers / timeline tool output."""
    if not text:
        return []
    m = re.search(r"TRACK_STACK_JSON=(\[[\s\S]*?\])\s*(?:\n|$)", text)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                {
                    "layer_number": int(item.get("layer_number") or 0),
                    "ui_track": int(item.get("ui_track") or 0),
                    "z_from_bottom": int(item.get("z_from_bottom") or 0),
                    "label": str(item.get("label") or ""),
                    "track_id": str(item.get("track_id") or ""),
                }
            )
        except Exception:
            continue
    return out
