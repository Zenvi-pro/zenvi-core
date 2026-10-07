"""
 @file
 @brief Run the local analysis layers for one file and keep the results on the shelf.

 Layers: ``structure`` (shots, camera motion) and ``look`` (colour) from one decode pass,
 ``audio`` (loudness, silence, tempo), and ``speech`` (the local transcript, aligned to the
 shots). Only layers that are missing or older than the current algorithm run, so calling this
 again for the same content, in any project, does no work. One layer failing never stops the
 others, and cancelling keeps whatever finished.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from classes.logger import log
from classes.media_index import audio as audio_mod
from classes.media_index import look as look_mod
from classes.media_index import schema as S
from classes.media_index import speech_facts
from classes.media_index import structure as structure_mod
from classes.media_index.probe import probe_media
from classes.media_index.store import Shelf, default_shelf, sha_of

READY = "ready"
CACHED = "cached"        # already on the shelf at the current version
SKIPPED = "skipped"      # nothing to analyse (no video / no audio)
UNAVAILABLE = "unavailable"
FAILED = "failed"
NOT_APPLICABLE = S.NOT_APPLICABLE

# share of the progress bar each stage owns
_WEIGHTS = {"structure": (0.0, 0.5), "audio": (0.5, 0.6), "speech": (0.6, 1.0)}


class Cancelled(Exception):
    """The caller asked to stop; finished layers are kept."""


def _is_current(shelf: Shelf, sha: str, layer: str) -> bool:
    return shelf.layer_ready(sha, layer, version=S.LAYER_VERSIONS[layer])


def _watch_cancel(should_cancel: Optional[Callable[[], bool]], token: Any) -> Callable[[], None]:
    """Cancel the speech *token* when *should_cancel* turns true (ASR runs in a subprocess)."""
    stop = threading.Event()
    if should_cancel is None:
        return stop.set

    def _poll() -> None:
        while not stop.wait(0.25):
            if should_cancel():
                token.cancel()
                return

    threading.Thread(target=_poll, name="media-index-cancel", daemon=True).start()
    return stop.set


def technical_of(probe: Dict[str, Any]) -> Dict[str, Any]:
    """The technical facts of a file worth keeping beside the index (what the media health check reads), no picture data."""
    video, audio = probe.get("video") or {}, probe.get("audio") or {}
    return {"duration": probe.get("duration"), "format": probe.get("format"), "bit_rate": probe.get("bit_rate") or None,
            "video": {k: video.get(k) for k in ("codec", "width", "height", "rotation", "fps", "nominal_fps", "vfr", "interlaced", "bit_depth",
                                                 "hdr", "color_transfer", "bit_rate", "orientation")} if video else None,
            "audio": {k: audio.get(k) for k in ("codec", "sample_rate", "channels", "channel_layout")} if audio else None}


def compute_facts(
    path: str,
    *,
    fingerprint: Any = None,
    shelf: Optional[Shelf] = None,
    media_type: str = "video",
    run_speech: bool = True,
    transcribe: Optional[Callable[..., Any]] = None,
    transcript_cache: Any = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Run the missing local layers for *path*. Returns what happened to each layer.

    Raises ``Cancelled`` if *should_cancel* turns true (layers already finished stay saved).
    """
    shelf = shelf or default_shelf()
    sha = sha_of(fingerprint)
    if not sha:
        try:
            from classes.media_fingerprint import fingerprint as make_fp
            sha = sha_of(make_fp(path))
        except Exception:
            sha = ""
    if not sha:
        return {"ok": False, "error": "could not fingerprint the file", "layers": {}}

    probe = probe_media(path)
    if not probe.get("ok"):
        return {"ok": False, "sha": sha, "error": probe.get("error") or "unreadable media", "layers": {}}

    def report(stage: str, fraction: float) -> None:
        if on_progress is not None:
            lo, hi = _WEIGHTS[stage]
            on_progress(round(lo + (hi - lo) * max(0.0, min(1.0, fraction)), 4))

    layers: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    video = probe.get("video") or {}
    shelf.set_source(sha, duration=probe.get("duration"), width=video.get("width"), height=video.get("height"),
                     fps=video.get("fps"), has_audio=probe.get("has_audio"), has_video=probe.get("has_video"),
                     media_type=media_type, orientation=video.get("orientation"),
                     colour=look_mod.pipeline_from_probe(probe),
                     captured_at=(probe.get("capture") or {}).get("captured_at"),
                     captured_source=(probe.get("capture") or {}).get("captured_source"),
                     gps=(probe.get("capture") or {}).get("gps"),
                     camera=probe.get("camera") or None, technical=technical_of(probe))

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    def fail(layer: str, exc: BaseException) -> None:
        log.warning("Media index: %s layer failed for %s: %s", layer, path, exc, exc_info=True)
        errors[layer] = str(exc)[:300]
        layers[layer] = FAILED
        shelf.set_layer(sha, layer, version=S.LAYER_VERSIONS[layer], status=FAILED, error=errors[layer])

    def not_applicable(*names: str, why: str) -> None:
        """A layer the file cannot have (no audio track, no picture): recorded, so nothing waits on it."""
        for name in names:
            shelf.set_layer(sha, name, version=S.LAYER_VERSIONS[name], status=NOT_APPLICABLE, note=why)

    if not probe.get("has_video"):
        not_applicable(S.LAYER_STRUCTURE, S.LAYER_LOOK, why="no picture")
    if not probe.get("has_audio"):
        not_applicable(S.LAYER_AUDIO, S.LAYER_SPEECH, why="no audio track")

    # ---- structure + look: one decode pass ------------------------------------------------
    shots: list = []
    structure_ran = False
    if not probe.get("has_video"):
        layers[S.LAYER_STRUCTURE] = layers[S.LAYER_LOOK] = SKIPPED
    elif _is_current(shelf, sha, S.LAYER_STRUCTURE) and _is_current(shelf, sha, S.LAYER_LOOK):
        layers[S.LAYER_STRUCTURE] = layers[S.LAYER_LOOK] = CACHED
        saved = shelf.read_json(sha, "structure.json") or {}
        shots = saved.get("shots") or []
    else:
        try:
            acc = look_mod.LookAccumulator()
            result = structure_mod.analyze_structure(
                path, probe, look=acc, should_cancel=should_cancel,
                on_progress=lambda f: report("structure", f))
            if result is None:
                layers[S.LAYER_STRUCTURE] = layers[S.LAYER_LOOK] = SKIPPED
            else:
                result["version"] = S.LAYER_VERSIONS[S.LAYER_STRUCTURE]
                result["kind"] = S.MEASURED
                result["source"] = {"duration": probe.get("duration"), "width": video.get("width"),
                                    "height": video.get("height"), "fps": video.get("fps"),
                                    "orientation": video.get("orientation"), "media_type": media_type}
                look_layer = acc.finalize(result["shots"], look_mod.pipeline_from_probe(probe))
                shelf.write_json(sha, "structure.json", result)
                shelf.write_json(sha, "look.json", look_layer)
                shelf.set_layer(sha, S.LAYER_STRUCTURE, version=S.LAYER_VERSIONS[S.LAYER_STRUCTURE], status=READY,
                                shots=len(result["shots"]))
                shelf.set_layer(sha, S.LAYER_LOOK, version=S.LAYER_VERSIONS[S.LAYER_LOOK], status=READY,
                                samples=look_layer["sampling"]["frames"])
                layers[S.LAYER_STRUCTURE] = layers[S.LAYER_LOOK] = READY
                shots = result["shots"]
                structure_ran = True
        except (structure_mod.Cancelled, audio_mod.Cancelled):
            raise Cancelled()
        except Exception as exc:
            fail(S.LAYER_STRUCTURE, exc)
            layers[S.LAYER_LOOK] = FAILED
    report("structure", 1.0)
    if cancelled():
        raise Cancelled()

    # ---- audio ---------------------------------------------------------------------------------
    if not probe.get("has_audio"):
        layers[S.LAYER_AUDIO] = SKIPPED
    elif _is_current(shelf, sha, S.LAYER_AUDIO):
        layers[S.LAYER_AUDIO] = CACHED
    else:
        try:
            result_a = audio_mod.analyze_audio(path, probe, should_cancel=should_cancel,
                                               on_progress=lambda f: report("audio", f))
            if result_a is None:
                layers[S.LAYER_AUDIO] = SKIPPED
            else:
                shelf.write_json(sha, "audio.json", result_a)
                shelf.set_layer(sha, S.LAYER_AUDIO, version=S.LAYER_VERSIONS[S.LAYER_AUDIO], status=READY,
                                windows=len(result_a.get("windows") or []))
                layers[S.LAYER_AUDIO] = READY
        except audio_mod.Cancelled:
            raise Cancelled()
        except Exception as exc:
            fail(S.LAYER_AUDIO, exc)
    report("audio", 1.0)
    if cancelled():
        raise Cancelled()

    # ---- speech ---------------------------------------------------------------------------------
    if not probe.get("has_audio") or not run_speech:
        layers[S.LAYER_SPEECH] = SKIPPED
    else:
        from classes.speech.runtime import CancelToken
        token = CancelToken()
        stop_watch = _watch_cancel(should_cancel, token)
        try:
            already = _is_current(shelf, sha, S.LAYER_SPEECH)
            layer = speech_facts.load_or_transcribe(path, probe, sha, shelf, transcribe=transcribe,
                                                    cache=transcript_cache, token=token)
            if layer is None:
                layers[S.LAYER_SPEECH] = SKIPPED
            elif layer.get("unavailable"):
                layers[S.LAYER_SPEECH] = UNAVAILABLE
                errors[S.LAYER_SPEECH] = str(layer["unavailable"])
            elif layer.get("skipped"):
                layers[S.LAYER_SPEECH] = SKIPPED
            else:
                if shots and (structure_ran or "per_shot" not in layer):
                    layer["per_shot"] = {str(k): v for k, v in speech_facts.per_shot_speech(layer["words"], shots).items()}
                    shelf.write_json(sha, "speech.json", layer)
                layers[S.LAYER_SPEECH] = CACHED if already else READY
        except InterruptedError:
            raise Cancelled()
        except Exception as exc:
            fail(S.LAYER_SPEECH, exc)
        finally:
            stop_watch()
    report("speech", 1.0)
    return {"ok": not errors or any(v in (READY, CACHED) for v in layers.values()), "sha": sha,
            "layers": layers, "errors": errors, "duration": probe.get("duration")}
