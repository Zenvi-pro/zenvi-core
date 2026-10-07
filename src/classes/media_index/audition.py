"""
 @file
 @brief Listen to several candidate music tracks before choosing one.

 Each candidate is a short preview (Freesound serves an MP3 preview for every sound). The preview is downloaded,
 analysed with the same local audio analysis as any imported file (tempo, beats, energy, sections, phrase points),
 and compared with what the edit needs. Nothing is imported and nothing is charged: previews are free and the
 analysis is local. Downloads are restricted to Freesound hosts over https and capped in size.
"""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import urlparse

from classes.logger import log
from classes.media_index import audio as index_audio, musicfit
from classes.media_index.probe import probe_media

ALLOWED_HOST_SUFFIXES = ("freesound.org",)
MAX_BYTES = 25 * 1024 * 1024
MAX_CANDIDATES = 12
TIMEOUT_SECONDS = 30
WORKERS = 4

Fetch = Callable[[str, str], Tuple[bool, str]]


def url_problem(url: Any) -> Optional[str]:
    """Why a preview URL may not be fetched, or None."""
    try:
        parts = urlparse(str(url or ""))
    except ValueError:
        return "not a valid URL"
    if parts.scheme != "https":
        return "only https preview URLs are fetched"
    host = (parts.hostname or "").lower()
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        return f"only {', '.join(ALLOWED_HOST_SUFFIXES)} previews are fetched"
    return None


def default_fetch(url: str, dest: str) -> Tuple[bool, str]:
    """Download *url* to *dest* (size-capped; redirects must stay on an allowed host)."""
    import requests

    try:
        with requests.get(url, stream=True, timeout=TIMEOUT_SECONDS, headers={"User-Agent": "Zenvi"}) as resp:
            resp.raise_for_status()
            problem = url_problem(resp.url)
            if problem:
                return False, "redirected away: " + problem
            size = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return False, f"larger than {MAX_BYTES // (1024 * 1024)} MB"
                    fh.write(chunk)
        return size > 0, "" if size > 0 else "empty download"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _one(cand: Dict[str, Any], wants: Dict[str, Any], fetch: Fetch, folder: str) -> Dict[str, Any]:
    cid = str(cand.get("id") or cand.get("preview_url") or "?")
    out: Dict[str, Any] = {"id": cid, "name": str(cand.get("name") or cid)[:120]}
    problem = url_problem(cand.get("preview_url"))
    if problem:
        return {**out, "error": problem}
    fd, dest = tempfile.mkstemp(dir=folder, suffix=".mp3")          # a name of its own: two candidates may share an id
    os.close(fd)
    ok, why = fetch(str(cand["preview_url"]), dest)
    if not ok:
        return {**out, "error": "could not fetch the preview: " + why}
    try:
        probe = probe_media(dest)
        audio = index_audio.analyze_audio(dest, probe) if probe.get("ok") else None
    except Exception as exc:  # noqa: BLE001
        return {**out, "error": f"could not analyse the preview: {exc}"[:200]}
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
    if not audio:
        return {**out, "error": "the preview has no audio"}
    seconds = float(cand.get("duration") or probe.get("duration") or 0.0)      # the full sound's length, not the preview's
    profile = musicfit.profile_from_audio(audio, float(probe.get("duration") or 0.0))
    profile["seconds"] = round(seconds, 1)
    fit = musicfit.music_fit(profile, **wants)
    return {**out, **fit, "bpm": profile["bpm"], "seconds": profile["seconds"], "preview_seconds": round(float(probe.get("duration") or 0.0), 1),
            "sections": [s["label"] for s in profile["sections"]], "phrase_points": profile["phrase_points"][:8],
            "preview_url": str(cand["preview_url"]),
            "energy": round(sum(profile["energy_arc"]) / len(profile["energy_arc"]), 2) if profile["energy_arc"] else None,
            "note": "tempo and energy are measured on the preview; the preview is shorter than the sound, so sections may be incomplete"}


def audition(candidates: Sequence[Dict[str, Any]], *, bpm_min: Optional[float] = None, bpm_max: Optional[float] = None,
             seconds: Optional[float] = None, energy: str = "", fetch: Optional[Fetch] = None, workers: int = WORKERS
             ) -> Dict[str, Any]:
    """Analyse each candidate's preview and rank them. Returns {"ranked": [...], "failed": [...]}."""
    wants = {"bpm_min": bpm_min, "bpm_max": bpm_max, "seconds": seconds, "energy": energy}
    items = [dict(c) for c in list(candidates)[:MAX_CANDIDATES] if isinstance(c, dict)]
    fetch = fetch or default_fetch
    with tempfile.TemporaryDirectory(prefix="zenvi_audition_") as folder:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1))) as pool:
            results = list(pool.map(lambda c: _one(c, wants, fetch, folder), items))
    ok_ = [r for r in results if "error" not in r]
    failed = [{"id": r["id"], "name": r["name"], "error": r["error"]} for r in results if "error" in r]
    ok_.sort(key=lambda r: (not r["fits"], -r["score"], r["name"].lower()))
    for rank, r in enumerate(ok_, 1):
        r["rank"] = rank
    if len(list(candidates)) > MAX_CANDIDATES:
        log.info("audition: only the first %d of %d candidates were analysed", MAX_CANDIDATES, len(list(candidates)))
    return {"ranked": ok_, "failed": failed, "analysed": len(items)}
