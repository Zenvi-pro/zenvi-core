"""
 @file
 @brief The indexing job: local analysis, search vectors, optional descriptions, keep.

 This is the logic of the background indexing worker, with no Qt in it, so it is tested in
 the ordinary headless suite. ``windows.models.files_model.BackendIndexingWorker`` is a thin
 QThread around it: the two ``emit_*`` callbacks are its signals, and it runs on the
 worker thread, never the GUI thread.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Tuple

from classes.logger import log

PROVIDER = "media-index-v2"
_FLIGHTS_LOCK = threading.Lock()
_FLIGHTS: Dict[str, Tuple[threading.Lock, int]] = {}


@contextmanager
def single_flight(key: str, cancelled: Callable[[], bool] = lambda: False, poll: float = 0.2) -> Iterator[bool]:
    """Let one thread at a time work on *key* (a file's fingerprint); the others wait their turn.

    Two workers handed the same footage (two copies in one import, or the same file in two projects) would each pay for
    the same cloud analysis. The second waits, then finds the first one's result on the shelf and reuses it. Yields True
    once it holds the turn, or False when this job was cancelled while waiting.
    """
    with _FLIGHTS_LOCK:
        lock, users = _FLIGHTS.get(key) or (threading.Lock(), 0)
        _FLIGHTS[key] = (lock, users + 1)
    held = False
    try:
        while not held:
            held = lock.acquire(timeout=poll)
            if not held and cancelled():
                break
        yield held
    finally:
        if held:
            lock.release()
        with _FLIGHTS_LOCK:
            lock, users = _FLIGHTS[key]
            if users <= 1:
                del _FLIGHTS[key]
            else:
                _FLIGHTS[key] = (lock, users - 1)


class IndexingJob:
    """Index one file (or reuse a saved analysis of the same content) and report back."""

    # Files longer than 30 minutes are indexed, but only described on import after a yes (index_long_file_tool).
    _MAX_INDEXING_SECONDS = 30 * 60

    def __init__(
        self,
        file_data: Dict[str, Any],
        project_id: str = "",
        *,
        client_factory: Callable[[], Any],
        emit_completed: Callable[[dict, Any, Any], None],
        emit_progress: Callable[[str, str, int], None],
    ) -> None:
        self.file_data = file_data
        self.project_id = project_id or ""
        self._client_factory = client_factory
        self._completed = emit_completed
        self._progress = emit_progress
        self._cancelled = False
        self._collision = False

    def cancel(self) -> None:
        """Ask the job to stop at its next check (called from another thread at app quit)."""
        self._cancelled = True

    def _run_local_facts(self, file_path, media_type, file_id):
        """The local analysis layers (shots, colour, audio, transcript).

        Runs before any cloud step and needs no sign-in, so it works offline. It never raises:
        whatever happens here, indexing carries on exactly as it did before.
        """
        try:
            from classes.media_index import facts
            self._progress(file_id, "analyzing", 0)
            result = facts.compute_facts(
                file_path,
                fingerprint=self.file_data.get("fingerprint"),
                media_type=media_type,
                should_cancel=lambda: self._cancelled,
                on_progress=lambda f: self._progress(file_id, "analyzing", int(f * 100)),
            )
        except Exception as exc:  # includes facts.Cancelled
            log.info("Local media analysis stopped for %s: %s", file_path, exc)
            return
        self._collision = bool((result or {}).get("collision"))
        self._run_people_scan(file_path, media_type)

    def _run_people_scan(self, file_path, media_type):
        """Find the faces in a video and tell the voices apart when the people preference is on and the models are already installed (it never downloads).

        Local only. Never raises: whatever happens here, indexing carries on exactly as it did before.
        """
        try:
            from classes.media_index.flags import people_enabled
            if media_type not in ("video", "audio") or not people_enabled():
                return
            from classes.media_index import default_shelf, library, people, people_models
            st = people_models.status()
            sha = self._fingerprint_sha(file_path)
            if not sha or not st["runtime"]:
                return
            shelf = default_shelf()
            fi = library.get_file_index(shelf, sha, file_id=str(self.file_data.get("id") or ""), name=file_path)
            if fi is None:
                return
            if st["faces_ready"] and fi.shots and not people.load_scan(sha):
                people.scan_video(file_path, sha, fi.shots, fi.duration, should_cancel=lambda: self._cancelled)
            if st["voices_ready"] and not people.load_voice_scan(sha):
                people.scan_file_voices(file_path, sha, fi.duration, shelf, should_cancel=lambda: self._cancelled)
        except Exception as exc:  # includes cancellation
            log.info("People scan skipped for %s: %s", file_path, exc)

    def _flight_key(self, file_path):
        """The fingerprint to take a turn on, or None when the file has none (then there is nothing to share)."""
        return self._fingerprint_sha(file_path) or None

    def _run_local_facts_once(self, file_path, media_type, file_id):
        key = self._flight_key(file_path)
        if not key:
            return self._run_local_facts(file_path, media_type, file_id)
        with single_flight(key, lambda: self._cancelled) as turn:
            if turn:
                self._run_local_facts(file_path, media_type, file_id)

    def _try_v2_once(self, client, file_path, media_type, duration, file_id, index_name, metadata):
        """``_try_v2`` with one job at a time per fingerprint. A job cancelled while it waits reports itself and stops."""
        key = self._flight_key(file_path)
        if not key:
            return self._try_v2(client, file_path, media_type, duration, file_id, index_name, metadata)
        with single_flight(key, lambda: self._cancelled) as turn:
            if not turn:
                self._completed(self.file_data, metadata, None)
                return
            return self._try_v2(client, file_path, media_type, duration, file_id, index_name, metadata)

    def _v2_index_block(self, index_name, file_id, media_type, sha):
        return {"status": "ready", "index_id": index_name, "index_name": index_name, "video_id": file_id,
                "provider": PROVIDER, "media_type": media_type, "v2": True, "fingerprint": sha}

    def _v2_failed(self, metadata, index_name, media_type, reason):
        """Finish the file as failed with *reason*, which the badge shows. Nothing is charged for it."""
        block = {"status": "failed", "error": reason, "index_name": index_name, "provider": PROVIDER, "media_type": media_type}
        metadata["index"], metadata["twelvelabs"], metadata["error"] = block, dict(block), reason
        self._completed(self.file_data, metadata, None)

    def _skip(self, metadata, index_name, media_type, reason):
        """Finish the file as skipped (nothing wrong with it: out of credits, over a limit)."""
        block = {"status": "skipped", "error": reason, "index_name": index_name, "provider": PROVIDER, "media_type": media_type}
        metadata["index"], metadata["twelvelabs"] = block, dict(block)
        self._completed(self.file_data, metadata, None)

    def _try_v2(self, client, file_path, media_type, duration, file_id, index_name, metadata):
        """The only way a file is indexed. It always finishes the job.

        The search vectors are made by the model chosen in Preferences: EmbeddingGemma 2 on this computer (the default,
        free, no sign-in), or Gemini Embedding 2 in the cloud when picked and signed in. A file whose vectors from that
        model are already on the shelf is not embedded again, in any project. Gemini descriptions are made here only when
        the user turned on "Describe all footage on import"; otherwise agents ask for them shot by shot.
        """
        from classes.media_index import cloud, default_shelf, embedder as E, vectors
        from classes.media_index import schema as S
        from classes.media_index.probe import probe_media

        sha = self._fingerprint_sha(file_path)
        if not sha:
            self._v2_failed(metadata, index_name, media_type, "The file has no fingerprint, so it cannot be indexed.")
            return
        shelf = default_shelf()
        if self._collision:          # local facts found the layers under this fingerprint belong to other footage
            # Two different files can in theory share a fingerprint; never describe this one with the other's layers.
            log.warning("Saved index for %s was made from different footage; not indexing it", file_path)
            self._v2_failed(metadata, index_name, media_type,
                            "This footage shares a fingerprint with a different file, so it cannot be indexed safely.")
            return

        signed_in = self._signed_in()
        model = E.chosen_model(signed_in)
        probe = probe_media(file_path)
        if not vectors.is_current(shelf, sha, model):
            self._progress(file_id, "indexing", 0)
            try:
                vectors.compute_vectors(file_path, probe, sha, shelf, E.get_embedder(model), should_cancel=lambda: self._cancelled,
                                        on_progress=lambda f: self._progress(file_id, "indexing", int(f * 100)))
            except vectors.Cancelled:
                self._completed(self.file_data, metadata, None)
                return
            except E.EmbedError as exc:
                if exc.flags.get("credits") or exc.flags.get("rate_limited"):
                    self._skip(metadata, index_name, media_type, str(exc))
                else:
                    self._v2_failed(metadata, index_name, media_type, f"Could not index this file for search: {exc}")
                return

        if media_type == "video" and signed_in and (E.describe_on_import() or self.file_data.get("index_long_ok")):
            # A cancel here stops only the descriptions: the file is searchable, so it is still reported indexed.
            self._describe_on_import(client, file_path, probe, sha, shelf, model, duration, file_id, metadata)

        structure = shelf.read_json(sha, "structure.json") or {}
        watch = shelf.read_json(sha, "watch.json") or {"shots": []}
        speech = shelf.read_json(sha, "speech.json") if shelf.layer_ready(sha, S.LAYER_SPEECH) else None
        meta = cloud.metadata_view(watch, speech, structure, media_type)
        meta["analyzed"] = True          # searchable: every file has its vectors (or nothing to embed) from here on
        if media_type == "audio" and shelf.layer_ready(sha, S.LAYER_AUDIO) and not meta.get("short_summary"):
            # A music file has no picture to describe; its local analysis is the summary.
            meta["short_summary"] = cloud.audio_summary(shelf.read_json(sha, "audio.json") or {}, duration)
        block = self._v2_index_block(index_name, file_id, media_type, sha)
        meta["index"], meta["twelvelabs"] = block, dict(block)
        self._progress(file_id, "done", 100)
        self._completed(self.file_data, meta, None)

    def _describe_on_import(self, client, file_path, probe, sha, shelf, model, duration, file_id, metadata):
        """Gemini descriptions of every shot, when the user asked for them up front (the preference, or a yes for a long
        file). Never fails the index: search works without them, and an agent can still ask for any shot later."""
        from classes.credits_client import check_operation
        from classes.media_index import cloud, embedder as E, vectors
        if duration > self._MAX_INDEXING_SECONDS and not self.file_data.get("index_long_ok"):
            log.info("Not describing %s on import: over 30 minutes and not approved", file_path)
            return
        _, _balance, blocked = check_operation("indexing_per_minute", "video descriptions", duration_seconds=duration)
        if blocked:
            log.info("Not describing %s on import: %s", file_path, blocked)
            return
        try:
            result = cloud.compute_cloud(client, file_path, probe, sha, shelf, file_id=file_id, media_type="video",
                                         should_cancel=lambda: self._cancelled,
                                         on_progress=lambda f: self._progress(file_id, "describing", int(f * 100)))
            if result.get("error"):
                log.info("Describing %s on import failed: %s", file_path, result["error"])
            vectors.add_descriptions(shelf, sha, E.get_embedder(model), (result.get("watch") or {}).get("shots") or [])
        except (cloud.Cancelled, E.EmbedError) as exc:
            log.info("Describing %s on import stopped: %s", file_path, exc)

    def run(self):
        client = self._client_factory()
        metadata = client._empty_ai_metadata()
        error = None
        try:
            file_path = self.file_data.get("path", "")
            file_id = self.file_data.get("id", "")
            # Re-resolve type from path: libopenshot often marks MP3 as has_video.
            from classes.image_types import get_media_type, is_audio_path
            media_type = str(self.file_data.get("media_type") or "").strip().lower()
            if is_audio_path(file_path):
                media_type = "audio"
                self.file_data["media_type"] = "audio"
            elif media_type not in ("video", "image", "audio"):
                media_type = get_media_type(self.file_data) if self.file_data else "video"
            if media_type in ("video", "image", "audio"):

                self._run_local_facts_once(file_path, media_type, file_id)
                if self._cancelled:
                    self._completed(self.file_data, metadata, None)
                    return
                duration = float(self.file_data.get("duration") or 0)

                from classes.project_tl_index import build_project_index_name

                index_name = build_project_index_name(self.project_id)
                self._try_v2_once(client, file_path, media_type, duration, file_id, index_name, metadata)
                return
        except Exception as exc:
            error = exc
            log.error(f"Backend indexing/summarize worker failed: {exc}")
        self._completed(self.file_data, metadata, error)

    # -- media index shelf ---------------------------------------------------------
    @staticmethod
    def _signed_in():
        """True when a Zenvi session exists. When that cannot be told, the answer is no: the file is then
        queued again on sign-in, instead of running unauthenticated cloud work that the backend will refuse."""
        try:
            from classes.auth_manager import AuthManager
            return bool(AuthManager.instance().is_authenticated())
        except Exception:
            log.warning("Could not tell whether a Zenvi session exists; treating as signed out", exc_info=True)
            return False

    def _fingerprint_sha(self, file_path):
        """sha256 fingerprint key of the file (stamped on import; computed here if missing)."""
        try:
            from classes.media_index import sha_of
            sha = sha_of(self.file_data.get("fingerprint"))
            if sha:
                return sha
            from classes.media_fingerprint import fingerprint
            return sha_of(fingerprint(file_path))
        except Exception:
            log.debug("Could not fingerprint %s", file_path, exc_info=True)
            return ""
