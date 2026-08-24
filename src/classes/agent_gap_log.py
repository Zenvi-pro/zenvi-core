"""Silent tool-gap log for the Zenvi Assistant.

When a user's request needs a capability with no matching registered tool,
we still do everything we CAN via real tool calls, and the chat response only
describes what actually happened — no "I couldn't do X" in the conversation.
The gap itself is recorded here instead, for later review in a separate
viewer (see the gap-log UI), as JSON Lines under the app's user data dir.

Detection (``classify_gap``) is a small, swappable rule-based classifier
(v1) — each rule checks the *live* tool registry, not a hardcoded snapshot,
so it self-retires the moment a matching tool actually ships. It can be
replaced with a model-backed classifier later without changing call sites.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from classes import info

GAP_LOG_PATH = os.path.join(info.USER_PATH, "agent_tool_gaps.jsonl")

_lock = threading.Lock()


def _tool_names() -> set:
    from classes.tool_handlers import AGENT_TOOL_HANDLERS
    return set(AGENT_TOOL_HANDLERS.keys())


# Each rule fires only while none of its `if_missing` tool names are registered.
_GAP_RULES = [
    {
        "keywords": ("fps", "frame rate", "framerate", "resolution", "sample rate"),
        "if_missing": ("set_project_setting_tool",),
        "capability": "no tool for directly changing project fps/resolution/sample rate "
                      "(only export_overrides via set_export_setting)",
    },
    {
        "keywords": ("audio ducking", "duck the audio", "duck audio"),
        "if_missing": ("add_effect_tool",),
        "capability": "no tool for adding arbitrary clip effects (e.g. per-clip audio ducking)",
    },
    {
        "keywords": ("add a title", "add title", "title card", "text overlay"),
        "if_missing": ("add_title_tool",),
        "capability": "no tool for headless title/text-overlay creation (only via the Title editor dialog)",
    },
]


def classify_gap(request_text: str):
    """Return a precise missing-capability description, or None if no known gap matches."""
    if not request_text:
        return None
    text = request_text.lower()
    try:
        available = _tool_names()
    except Exception:
        return None
    for rule in _GAP_RULES:
        if any(k in text for k in rule["keywords"]) and not any(t in available for t in rule["if_missing"]):
            return rule["capability"]
    return None


def _read_all() -> list:
    """Read all entries in append (oldest-first) order. Caller must hold _lock."""
    if not os.path.exists(GAP_LOG_PATH):
        return []
    entries = []
    with open(GAP_LOG_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return entries


def _rewrite_all(entries: list) -> None:
    """Overwrite the log with *entries*. Caller must hold _lock."""
    os.makedirs(os.path.dirname(GAP_LOG_PATH), exist_ok=True)
    with open(GAP_LOG_PATH, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def append_gap(entry: dict) -> dict:
    """Append one gap entry (JSON Lines). Returns the stored entry."""
    record = {
        "id": entry.get("id") or uuid.uuid4().hex,
        "ts": entry.get("ts") if entry.get("ts") is not None else time.time(),
        "request": entry.get("request", ""),
        "missing_capability": entry.get("missing_capability", ""),
        "session_id": entry.get("session_id", ""),
        "resolved": bool(entry.get("resolved", False)),
    }
    with _lock:
        os.makedirs(os.path.dirname(GAP_LOG_PATH), exist_ok=True)
        with open(GAP_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    return record


def read_gaps() -> list:
    """Return all logged gap entries, newest first."""
    with _lock:
        entries = _read_all()
    entries.reverse()
    return entries


def mark_resolved(entry_id: str) -> bool:
    """Mark a gap entry resolved in place. Returns True if it was found."""
    with _lock:
        entries = _read_all()
        found = False
        for e in entries:
            if e.get("id") == entry_id:
                e["resolved"] = True
                found = True
        if found:
            _rewrite_all(entries)
    return found


def delete_gap(entry_id: str) -> bool:
    """Delete a gap entry. Returns True if it existed."""
    with _lock:
        entries = _read_all()
        remaining = [e for e in entries if e.get("id") != entry_id]
        found = len(remaining) != len(entries)
        if found:
            _rewrite_all(remaining)
    return found
