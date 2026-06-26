"""Upload a local video file to TwelveLabs presigned URLs (no backend video hop)."""

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests


def upload_file_via_presigned_urls(
    file_path: str,
    *,
    chunk_size: int,
    presigned_urls: List[Dict[str, Any]],
    fetch_more_urls: Callable[[int, int], List[Dict[str, Any]]],
    upload_headers: Optional[Dict[str, str]] = None,
    batch_size: int = 10,
) -> Tuple[List[Dict[str, Any]], str]:
    """Upload file chunks; return (parts, error). parts match upload-complete schema."""
    if not file_path or not os.path.isfile(file_path):
        return [], f"File not found: {file_path}"
    if chunk_size <= 0:
        return [], "Invalid chunk_size"

    total_size = os.path.getsize(file_path)
    total_chunks = max(1, (total_size + chunk_size - 1) // chunk_size)

    url_map: Dict[int, str] = {}
    for item in presigned_urls or []:
        idx = int(item.get("chunk_index") or 0)
        url = str(item.get("url") or "")
        if idx > 0 and url:
            url_map[idx] = url

    completed: List[Dict[str, Any]] = []
    headers = dict(upload_headers or {})
    headers.setdefault("Content-Type", "application/octet-stream")

    with open(file_path, "rb") as fh:
        for batch_start in range(0, total_chunks, batch_size):
            batch_end = min(batch_start + batch_size, total_chunks)
            batch_indices = list(range(batch_start + 1, batch_end + 1))

            missing = [i for i in batch_indices if i not in url_map]
            if missing:
                start = min(missing)
                count = max(missing) - start + 1
                extra = fetch_more_urls(start, count)
                for item in extra or []:
                    idx = int(item.get("chunk_index") or 0)
                    url = str(item.get("url") or "")
                    if idx > 0 and url:
                        url_map[idx] = url
                missing = [i for i in batch_indices if i not in url_map]
                if missing:
                    return completed, f"Missing presigned URL for chunk {missing[0]}"

            for chunk_index in batch_indices:
                offset = (chunk_index - 1) * chunk_size
                fh.seek(offset)
                data = fh.read(chunk_size)
                if not data:
                    return completed, f"Empty read for chunk {chunk_index}"

                url = url_map[chunk_index]
                try:
                    resp = requests.put(url, data=data, headers=headers, timeout=600)
                    resp.raise_for_status()
                except Exception as exc:
                    return completed, f"Chunk {chunk_index} upload failed: {exc}"

                etag = (resp.headers.get("ETag") or "").strip().strip('"')
                if not etag:
                    return completed, f"Chunk {chunk_index} missing ETag"

                completed.append({
                    "chunk_index": chunk_index,
                    "proof": etag,
                    "chunk_size": len(data),
                })

    return completed, ""
