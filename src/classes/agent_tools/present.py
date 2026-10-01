"""Present tool receipts as user-facing chat text (not raw contract JSON)."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from classes.agent_tools.receipt import parse_receipt

NO_AUDIO_USER_MSG = (
    "This video has no audio track, so there's no dialogue to transcribe. "
    "Import a clip with spoken audio, or re-record with the microphone / "
    "system audio enabled."
)

_SCRIPT_ARTIFACT_KEYS = frozenset({
    "script",
    "transcript",
    "dialogue",
    "full_script",
    "fullscript",
})


def is_no_audio_message(text: Any) -> bool:
    lower = str(text or "").lower()
    return (
        "no audio track" in lower
        or "video-only" in lower
        or "does not contain any stream" in lower
    )


def word_text(word: Any) -> str:
    if isinstance(word, dict):
        return str(word.get("text") or "").strip()
    if isinstance(word, (list, tuple)) and len(word) >= 2:
        return str(word[1] or "").strip()
    return str(getattr(word, "text", "") or "").strip()


def words_to_script(words: Optional[Iterable[Any]]) -> str:
    """Join ASR words into a readable script (paragraph breaks on sentence ends)."""
    texts = [t for t in (word_text(w) for w in (words or [])) if t]
    if not texts:
        return ""
    parts: list[str] = []
    buf: list[str] = []
    for i, token in enumerate(texts):
        buf.append(token)
        ends = bool(token) and token[-1] in ".!?"
        nxt = texts[i + 1] if i + 1 < len(texts) else ""
        next_cap = bool(nxt) and nxt[0].isupper()
        if ends and next_cap:
            parts.append(" ".join(buf))
            buf = []
    if buf:
        parts.append(" ".join(buf))
    return "\n\n".join(parts)


def clips_to_script(clips: Optional[Iterable[dict]]) -> str:
    rows = [c for c in (clips or []) if isinstance(c, dict)]
    if not rows:
        return ""
    sections: list[str] = []
    multi = len(rows) > 1
    for clip in rows:
        script = words_to_script(clip.get("words") or [])
        if not script:
            script = words_to_script(clip.get("compactWords") or [])
        if not script:
            continue
        if multi:
            label = str(clip.get("clipId") or clip.get("fileId") or "").strip()
            sections.append(f"[{label}]\n{script}" if label else script)
        else:
            sections.append(script)
    return "\n\n".join(sections)


def _parse_json_object(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _script_from_artifacts(artifacts: Any) -> str:
    if not isinstance(artifacts, dict):
        return ""
    for key, val in artifacts.items():
        if str(key or "").strip().lower() not in _SCRIPT_ARTIFACT_KEYS:
            continue
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    return ""


def user_facing_subagent_text(text: str) -> Optional[str]:
    """If *text* is a SubagentResult JSON blob, return the full dialogue script."""
    data = _parse_json_object(text)
    if data is None:
        return None
    # SubagentResult always has status and/or summary; receipts use contract/tool.
    if "contract" in data or "tool" in data:
        return None
    if "status" not in data and "summary" not in data:
        return None
    script = _script_from_artifacts(data.get("artifacts"))
    if script:
        return script
    return None


def user_facing_receipt_text(text: str) -> str:
    """Turn a contract-3 receipt / SubagentResult (or plain text) into chat copy.

    get_transcript dumps become the dialogue script. SubagentResult with
    artifacts.script becomes that full script. Other receipts become their
    summary. Non-structured text is returned unchanged.
    """
    raw = "" if text is None else str(text)
    sub = user_facing_subagent_text(raw)
    if sub:
        return sub

    receipt = parse_receipt(raw)
    if receipt is None:
        if is_no_audio_message(raw):
            return NO_AUDIO_USER_MSG
        return raw

    tool = str(receipt.get("tool") or "")
    status = str(receipt.get("status") or "")
    summary = str(receipt.get("summary") or "").strip()
    data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}

    if tool in ("get_transcript_tool", "transcribe_media_tool"):
        script = data.get("script") if isinstance(data.get("script"), str) else ""
        if not script.strip() and isinstance(data.get("clips"), list):
            script = clips_to_script(data.get("clips"))
        if status in ("applied", "unchanged") and script.strip():
            return script.strip()
        if is_no_audio_message(summary) or is_no_audio_message(
            " ".join(str(w) for w in (receipt.get("warnings") or []))
        ):
            return NO_AUDIO_USER_MSG
        if not script.strip() and status in ("applied", "unchanged"):
            return summary or "No spoken dialogue found."

    if summary.startswith("Error:"):
        summary = summary[6:].strip()
    if is_no_audio_message(summary):
        return NO_AUDIO_USER_MSG
    return summary or raw
