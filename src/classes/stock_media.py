"""
Client-side stock-media download helper.

Stock providers (Pexels video, Freesound audio) expose direct, publicly
accessible CDN URLs. Historically the desktop app asked the backend to download
these files, but the backend runs remotely (api.zenvi.pro) and returned a path
on *its* filesystem — a path that does not exist on the user's machine, so
``openshot.Clip`` could not open it ("... is not a valid video, audio, or image
file"). Downloading here, on the user's machine, fixes that for every caller:
the Pexels/Freesound docks and the chat "add stock media" tool.
"""

import os
import re
import tempfile

import requests

from classes.logger import log

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Zenvi/1.0"


def _safe_name(name: str) -> str:
    """Sanitise an arbitrary string into a filesystem-safe filename stem."""
    cleaned = re.sub(r"[^\w\-]", "_", name or "")
    return cleaned[:120].strip("_")


def download_stock_file(url: str, subdir: str, filename: str, ext: str):
    """Download a public stock-media URL to the local temp dir.

    Args:
        url: Direct, publicly accessible media URL (Pexels MP4 link or
            Freesound HQ MP3 preview URL).
        subdir: Sub-directory under the system temp dir (e.g. "zenvi_pexels").
        filename: Output filename stem, without extension (e.g. "pexels_123").
        ext: File extension without the dot (e.g. "mp4", "mp3").

    Returns:
        (local_path, error) tuple. On success ``error`` is "" and ``local_path``
        points at a non-empty file on the local machine. On failure
        ``local_path`` is "" and ``error`` holds a human-readable message.
    """
    if not url:
        return "", "No download URL provided"

    stem = _safe_name(filename) or _safe_name(subdir) or "stock_media"
    dest_dir = os.path.join(tempfile.gettempdir(), subdir)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        return "", f"Could not create download directory: {exc}"

    dest_path = os.path.join(dest_dir, f"{stem}.{ext}")

    # Reuse a previous download if it is already present and non-empty.
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path, ""

    tmp_path = dest_path + ".part"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            stream=True,
            timeout=180,
        )
        resp.raise_for_status()
        with open(tmp_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    fh.write(chunk)

        if os.path.getsize(tmp_path) == 0:
            os.remove(tmp_path)
            return "", "Downloaded file is empty"

        os.replace(tmp_path, dest_path)
        log.info("Stock media downloaded locally: %s", dest_path)
        return dest_path, ""
    except Exception as exc:
        # Best-effort cleanup of the partial file.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        log.error("Stock media download failed (%s): %s", url, exc)
        return "", str(exc)
