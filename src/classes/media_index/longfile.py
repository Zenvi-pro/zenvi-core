"""
 @file
 @brief Long recordings: how to cut one into pieces for the cloud pass, and what asking first looks like.

 A file over half an hour is described in chunks (each its own proxy, upload and cloud job), cut only between shots,
 and only after the user said yes: it can take a long time and use a lot of the cloud. Pure logic, no Qt, no network.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

LONG_SECONDS = 30 * 60           # longer than this needs a yes before the cloud pass
CHUNK_SECONDS = 20 * 60          # a chunk is at most this long (unless one shot is longer, which cannot be: shots are capped)
CREDITS_PER_MINUTE_CEILING = 18  # the backend's flat per-minute estimate. The real charge is from Gemini's tokens and lower.


def is_long(duration_seconds: float) -> bool:
    return float(duration_seconds or 0.0) > LONG_SECONDS


def plan_chunks(shots: Sequence[Dict[str, Any]], chunk_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
    """Chunks of whole shots, in order: ``{"index", "start", "end", "shot_ids"}``. Never cut inside a shot.

    A chunk closes when the next shot would take it past *chunk_seconds*; a single shot longer than that gets a chunk
    of its own.
    """
    chunk_seconds = CHUNK_SECONDS if chunk_seconds is None else chunk_seconds
    ordered = sorted((s for s in shots if float(s["end"]) > float(s["start"])), key=lambda s: float(s["start"]))
    chunks: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    for shot in ordered:
        if current and float(shot["end"]) - float(current[0]["start"]) > chunk_seconds:
            chunks.append(_close(len(chunks), current))
            current = []
        current.append(shot)
    if current:
        chunks.append(_close(len(chunks), current))
    return chunks


def _close(index: int, shots: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"index": index, "start": float(shots[0]["start"]), "end": float(shots[-1]["end"]), "shot_ids": [int(s["id"]) for s in shots]}


def estimate(duration_seconds: float) -> Dict[str, Any]:
    """What describing a long file involves: whole minutes, how many cloud jobs, and a credit ceiling."""
    minutes = max(1, int(math.ceil(float(duration_seconds or 0.0) / 60.0)))
    return {"minutes": minutes, "chunks": max(1, int(math.ceil(float(duration_seconds or 0.0) / CHUNK_SECONDS))),
            "max_credits": minutes * CREDITS_PER_MINUTE_CEILING}


def approval_text(name: str, duration_seconds: float) -> str:
    """The sentence shown on a long file that is waiting for a yes."""
    est = estimate(duration_seconds)
    return (f"{name} is {duration_seconds / 60:.0f} min long. Describing it for search is done in {est['chunks']} cloud passes and "
            f"costs at most about {est['max_credits']} credits (the real charge follows what the AI actually used and is usually lower). "
            "Its shots, colour and sound are already analysed on this computer. Approve it with index_long_file_tool to describe it.")
