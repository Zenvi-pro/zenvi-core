"""Append-only speech/tool audit log for local debugging sessions.

Enable with ``ZENVI_SPEECH_AUDIT=1``. Writes JSON lines to
``~/.openshot_qt/logs/speech_audit.jsonl`` (and mirrors a short line to logging).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("speech.audit")
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("ZENVI_SPEECH_AUDIT", "").strip() not in ("", "0", "false", "False")


def _path() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(
            os.path.expanduser("~"), ".openshot_qt",
        )
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "logs", "speech_audit.jsonl")


def audit(event: str, **fields: Any) -> None:
    if not enabled():
        return
    row = {"ts": time.time(), "event": event, **fields}
    line = json.dumps(row, ensure_ascii=False, default=str)
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        log.debug("speech audit write failed: %s", exc)
    # Always mirror to the app log when audit is on
    summary = fields.get("summary") or fields.get("tool") or event
    log.info("SPEECH_AUDIT %s %s", event, summary)


def audit_receipt(tool: str, receipt_json: str) -> str:
    """Parse a receipt JSON string, audit it, return unchanged."""
    if not enabled():
        return receipt_json
    try:
        from classes.agent_tools.receipt import parse_receipt
        data = parse_receipt(receipt_json)
    except Exception:
        audit("tool_raw", tool=tool, raw=str(receipt_json)[:2000])
        return receipt_json
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    # Truncate huge word lists but keep count + sample
    words_sample = None
    clips = payload.get("clips") if isinstance(payload, dict) else None
    if isinstance(clips, list) and clips:
        w0 = (clips[0] or {}).get("words") or []
        words_sample = w0[:12]
    audit(
        "tool_receipt",
        tool=tool,
        status=data.get("status"),
        summary=data.get("summary"),
        transcriptionSource=payload.get("transcriptionSource") if isinstance(payload, dict) else None,
        modelId=payload.get("modelId") if isinstance(payload, dict) else None,
        transcriptGeneration=payload.get("transcriptGeneration") if isinstance(payload, dict) else None,
        wordSample=words_sample,
        warnings=data.get("warnings"),
    )
    return receipt_json
