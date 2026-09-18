"""Versioned agent tool receipts (contract 3).

Every ``execute_tool`` result is a JSON string of a ToolReceipt so Assistant
and Claude Code can patch state without re-reading the whole timeline.
Failures keep ``summary`` starting with ``Error:`` for existing classifiers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

CONTRACT_VERSION = 3

ReceiptStatus = Literal["applied", "unchanged", "refused", "error"]

MUTATION_CLIP_LIMIT = 30
SHIFT_GROUP_MINIMUM = 3


@dataclass
class WatchSuggested:
    clipId: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return {"clipId": self.clipId, "start": self.start, "end": self.end}


@dataclass
class ToolReceipt:
    status: ReceiptStatus
    tool: str
    summary: str
    contract: int = CONTRACT_VERSION
    clips: list[dict] = field(default_factory=list)
    clipsNote: Optional[str] = None
    shifted: list[dict] = field(default_factory=list)
    removedClipIds: list[str] = field(default_factory=list)
    createdTracks: list[dict] = field(default_factory=list)
    markers: list[dict] = field(default_factory=list)
    removedMarkerIds: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    watchSuggested: Optional[dict] = None
    undoSteps: int = 0
    data: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.status in ("error", "refused") and not self.summary.startswith("Error"):
            self.summary = f"Error: {self.summary}"
        if self.status in ("applied", "unchanged") and self.summary.startswith("Error"):
            # Keep caller-supplied Error: only for failure statuses.
            pass

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "contract": self.contract,
            "status": self.status,
            "tool": self.tool,
            "summary": self.summary,
            "clips": self.clips,
            "shifted": self.shifted,
            "removedClipIds": self.removedClipIds,
            "createdTracks": self.createdTracks,
            "markers": self.markers,
            "removedMarkerIds": self.removedMarkerIds,
            "warnings": self.warnings,
            "notes": self.notes,
            "watchSuggested": self.watchSuggested,
            "undoSteps": self.undoSteps,
        }
        if self.clipsNote is not None:
            out["clipsNote"] = self.clipsNote
        if self.data is not None:
            out["data"] = self.data
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def error(cls, tool: str, message: str, **kwargs) -> "ToolReceipt":
        summary = message if message.startswith("Error") else f"Error: {message}"
        return cls(status="error", tool=tool, summary=summary, undoSteps=0, **kwargs)

    @classmethod
    def refused(cls, tool: str, message: str, **kwargs) -> "ToolReceipt":
        summary = message if message.startswith("Error") else f"Error: {message}"
        return cls(status="refused", tool=tool, summary=summary, undoSteps=0, **kwargs)

    @classmethod
    def unchanged(cls, tool: str, summary: str, **kwargs) -> "ToolReceipt":
        return cls(status="unchanged", tool=tool, summary=summary, undoSteps=0, **kwargs)

    @classmethod
    def applied(cls, tool: str, summary: str, undo_steps: int = 1, **kwargs) -> "ToolReceipt":
        return cls(status="applied", tool=tool, summary=summary, undoSteps=undo_steps, **kwargs)


def from_handler_str(tool: str, text: str, *, mutated: bool = False) -> ToolReceipt:
    """Shim: wrap a legacy prose / Error: handler string as a receipt."""
    raw = "" if text is None else str(text)
    if raw.startswith("Error"):
        return ToolReceipt.error(tool, raw)
    if not raw.strip():
        return ToolReceipt.unchanged(tool, "No changes.")
    # Heuristic: many handlers return informational no-mutation text.
    lower = raw.lower()
    if not mutated and any(
        p in lower
        for p in (
            "nothing to",
            "no speech",
            "already",
            "no timeline",
            "no clips",
            "no files",
        )
    ):
        return ToolReceipt.unchanged(tool, raw)
    status: ReceiptStatus = "applied" if mutated else "applied"
    undo = 1 if mutated else 0
    # Without a snapshot we cannot know mutation; callers pass mutated=True
    # when history grew. Default undoSteps 0 for unshimmed read paths.
    if not mutated:
        undo = 0
        # Read-only successes still report applied with data in summary.
        status = "applied"
    return ToolReceipt(
        status=status,
        tool=tool,
        summary=raw,
        undoSteps=undo,
    )


def parse_receipt(text: str) -> Optional[dict]:
    """Return a contract-3 receipt dict, or None if *text* is not one."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("contract") != CONTRACT_VERSION:
        return None
    if "status" not in payload or "summary" not in payload:
        return None
    return payload


def is_error_result(text: str) -> bool:
    """Match the legacy Error: classifier used by UI / backend / nesting."""
    if not text:
        return True
    if text.startswith("Error"):
        return True
    receipt = parse_receipt(text)
    if receipt is None:
        return False
    summary = str(receipt.get("summary") or "")
    return receipt.get("status") in ("error", "refused") or summary.startswith("Error")
