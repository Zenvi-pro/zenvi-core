"""Upload a local chunk file to a Gemini Files resumable upload URL."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

import requests


def upload_file_to_gemini_resumable(
    file_path: str,
    upload_url: str,
    *,
    mime_type: str = "video/mp4",
    timeout: int = 600,
) -> Tuple[Dict[str, Any], str]:
    """PUT/finalize bytes to a Gemini resumable upload URL.

    Returns ({name, uri, mime_type, ...}, error).
    """
    if not file_path or not os.path.isfile(file_path):
        return {}, f"File not found: {file_path}"
    url = (upload_url or "").strip()
    if not url:
        return {}, "upload_url is required"

    size = os.path.getsize(file_path)
    try:
        with open(file_path, "rb") as fh:
            data = fh.read()
        resp = requests.post(
            url,
            data=data,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": mime_type or "video/mp4",
            },
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return {}, f"Gemini upload failed ({resp.status_code}): {resp.text[:300]}"

        payload: Dict[str, Any] = {}
        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            try:
                payload = json.loads(resp.text or "{}")
            except Exception:
                payload = {}

        # Response shapes: {"file": {...}} or flat file object
        file_obj = payload.get("file") if isinstance(payload.get("file"), dict) else payload
        if not isinstance(file_obj, dict):
            file_obj = {}
        name = str(file_obj.get("name") or "").strip()
        uri = str(file_obj.get("uri") or "").strip()
        if not name and not uri:
            return {}, f"Gemini upload finalize missing file name/uri: {resp.text[:300]}"
        return {
            "name": name,
            "uri": uri,
            "mime_type": str(file_obj.get("mimeType") or file_obj.get("mime_type") or mime_type),
            "raw": file_obj,
        }, ""
    except Exception as exc:
        return {}, f"Gemini upload error: {exc}"
