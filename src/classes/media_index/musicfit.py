"""
 @file
 @brief What a piece of music is, as an editor needs it, and whether it fits what an edit asks for.

 Pure functions over the audio layer's facts (tempo, beats, energy arc, sections, phrase points). Everything
 is measured; every line of a fit verdict names the comparison it made, and the score is simply the share of the
 requested criteria that were met, so a ranking can be explained.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from classes.media_index import spectro

ENERGY_RANGES = {"low": (0.0, 0.4), "medium": (0.3, 0.7), "high": (0.6, 1.0)}
BUILD_RISE = 0.15


def profile_from_audio(audio: Dict[str, Any], seconds: float) -> Dict[str, Any]:
    """The editor's view of a music file from its saved audio analysis."""
    tempo, music, loud = audio.get("tempo") or {}, audio.get("music") or {}, audio.get("loudness") or {}
    return {
        "seconds": round(float(seconds or 0.0), 1), "bpm": tempo.get("bpm"), "tempo_confidence": audio.get("tempo_confidence"),
        "beats": len(tempo.get("beats") or []), "integrated_lufs": loud.get("integrated_lufs"), "loudness_range": loud.get("lra"),
        "dynamic_range_db": audio.get("dynamic_range_db"), "brightness_hz": music.get("brightness_hz"),
        "energy_arc": music.get("arc", []), "arc_seconds": music.get("arc_seconds"),
        "sections": music.get("sections", []), "downbeats": (music.get("downbeats") or [])[:16],
        "phrase_points": (music.get("phrase_points") or [])[:16], "silence_ranges": (audio.get("silence_ranges") or [])[:8],
        "spectrogramSuggested": spectro.suggest(music.get("sections", []), seconds),
    }


def _mean(values: List[float]) -> float:
    return sum(values) / len(values)


def is_building(arc: List[float]) -> bool:
    """Energy rises and ends high: the last third clearly above the first and not below the middle (a mid-track peak is not a build)."""
    if len(arc) < 3:
        return False
    third = max(1, len(arc) // 3)
    first, middle, last = arc[:third], arc[third:2 * third] or arc[:third], arc[-third:]
    return _mean(last) > _mean(first) + BUILD_RISE and _mean(last) >= _mean(middle) - 0.05


def music_fit(profile: Dict[str, Any], *, bpm_min: Optional[float] = None, bpm_max: Optional[float] = None,
              seconds: Optional[float] = None, energy: str = "") -> Dict[str, Any]:
    """How well a track fits what the edit needs: {"fits", "score", "met", "asked", "notes"}.

    ``score`` is met / asked (1.0 when nothing was asked). A track too short for the edit still fits when it has
    phrase points to end or loop on.
    """
    notes: List[str] = []
    asked = met = 0
    bpm = profile.get("bpm")
    if bpm_min or bpm_max:
        asked += 1
        if bpm is None:
            notes.append("no steady tempo was found, so cuts cannot be placed on beats")
        elif (bpm_min and bpm < bpm_min) or (bpm_max and bpm > bpm_max):
            notes.append(f"{bpm:.0f} BPM is outside {bpm_min or 0:.0f}-{bpm_max or 999:.0f}")
        else:
            met += 1
            notes.append(f"{bpm:.0f} BPM is inside the wanted range")
    if seconds:
        asked += 1
        have = float(profile.get("seconds") or 0.0)
        if have >= seconds:
            met += 1
            notes.append(f"long enough ({have:.0f} s for {seconds:.0f} s needed)")
        else:
            usable = bool(profile.get("phrase_points"))
            met += 1 if usable else 0
            notes.append(f"{have:.0f} s is shorter than the {seconds:.0f} s needed: it must be looped or ended early at a phrase point"
                         + ("" if usable else " (and it has no clear phrase points)"))
    arc = profile.get("energy_arc") or []
    if energy and arc and (energy == "building" or energy in ENERGY_RANGES):
        asked += 1
        if energy == "building":
            rising = is_building(arc)
            met += 1 if rising else 0
            notes.append("energy builds over the track" if rising else "energy does not build")
        elif energy in ENERGY_RANGES:
            lo, hi = ENERGY_RANGES[energy]
            mean = _mean(arc)
            inside = lo <= mean <= hi
            met += 1 if inside else 0
            notes.append(f"average energy {mean:.2f} is {'within' if inside else 'outside'} the {energy} range")
    return {"fits": met == asked, "score": round(met / asked, 3) if asked else 1.0, "met": met, "asked": asked, "notes": notes}
