"""
 @file
 @brief The indexing job: sign-in check, reuse of a saved analysis, upload, charge, keep.

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

    # Hard limit: clips longer than 30 minutes are not indexed or summarized.
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

    def _long_file_needs_approval(self, file_path, duration, metadata) -> bool:
        """True (with the reason and the cost estimate written into *metadata*) when a file over 30 minutes must not go on.

        A long file can be described in chunks, but only after the user approved it (the file carries ``index_long_ok``):
        it takes a while and uses the cloud.
        """
        import os
        from classes.media_index import longfile
        name = os.path.basename(file_path) or "This file"
        if self.file_data.get("index_long_ok"):
            return False
        from classes.indexing_status import SKIP_LONG
        metadata["skip_reason"] = longfile.approval_text(name, duration)
        metadata["skip_code"] = SKIP_LONG
        return True

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
        block = {"status": "ready", "index_id": index_name, "index_name": index_name, "video_id": file_id,
                 "provider": "gemini-v2", "media_type": media_type, "v2": True, "fingerprint": sha}
        return block

    def _v2_failed(self, metadata, index_name, media_type, reason):
        """Finish the file as failed with *reason*, which the badge shows. Nothing is charged for it."""
        block = {"status": "failed", "error": reason, "index_name": index_name, "provider": "gemini-v2", "media_type": media_type}
        metadata["index"], metadata["twelvelabs"], metadata["error"] = block, dict(block), reason
        self._completed(self.file_data, metadata, None)

    def _try_v2(self, client, file_path, media_type, duration, file_id, index_name, metadata):
        """The media index v2 path, the only way a file is indexed. It always finishes the job.

        A file whose v2 layers are already on the shelf is restored for free, in any project, with no sign-in and no
        charge. Anything else is described by the cloud, or fails with the reason it cannot be.
        """
        from classes.media_index import S_WATCH, S_VECTORS, cloud, default_shelf
        from classes.media_index import schema as S

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

        def ready(layer):
            # any saved version counts: an older cloud layer is kept, not paid for again
            return shelf.layer_ready(sha, layer)

        def settled(layer):
            return cloud.is_settled(shelf, sha, layer)

        def build():
            structure = shelf.read_json(sha, "structure.json") or {}
            watch = shelf.read_json(sha, "watch.json") or {"shots": []}
            speech = shelf.read_json(sha, "speech.json") if ready(S.LAYER_SPEECH) else None
            meta = cloud.metadata_view(watch, speech, structure, media_type)
            if media_type == "audio" and not meta.get("analyzed") and ready(S.LAYER_AUDIO):
                # A music file has no picture to describe; its local analysis is the whole index, and the audio roles
                # (music bed, sound effect) are only assigned to files marked analysed.
                meta["analyzed"] = True
                meta["short_summary"] = cloud.audio_summary(shelf.read_json(sha, "audio.json") or {}, duration)
            block = self._v2_index_block(index_name, file_id, media_type, sha)
            meta["index"], meta["twelvelabs"] = block, dict(block)
            return meta

        done_watch = settled(S_WATCH) or media_type != "video"
        if done_watch and settled(S_VECTORS):
            self._progress(file_id, "done", 100)
            self._completed(self.file_data, build(), None)
            return

        if not self._signed_in():
            from classes.indexing_status import SKIP_SIGNIN
            metadata["skip_reason"] = "Sign in to Zenvi to index this file for search."
            metadata["skip_code"] = SKIP_SIGNIN
            self._completed(self.file_data, metadata, None)
            return

        from classes.credits_client import check_operation
        credit_duration = duration if media_type != "image" else 60.0
        _, _balance, blocked = check_operation("indexing_per_minute", f"{media_type} indexing", duration_seconds=credit_duration)
        if blocked:
            block = {"status": "skipped", "error": blocked, "index_name": index_name, "provider": "gemini-v2", "media_type": media_type}
            metadata["index"], metadata["twelvelabs"] = block, dict(block)
            self._completed(self.file_data, metadata, None)
            return

        from classes.media_index.probe import probe_media
        probe = probe_media(file_path)

        def on_progress(fraction):
            if fraction < 0.4:
                self._progress(file_id, "uploading", int(fraction / 0.4 * 100))
            else:
                self._progress(file_id, "indexing", -1)

        try:
            result = cloud.compute_cloud(client, file_path, probe, sha, shelf, file_id=file_id, media_type=media_type,
                                         should_cancel=lambda: self._cancelled, on_progress=on_progress)
        except cloud.Cancelled:
            self._completed(self.file_data, metadata, None)
            return
        if result.get("unsupported"):
            self._v2_failed(metadata, index_name, media_type, "The backend has no media index v2, so this file cannot be indexed yet.")
            return
        if result.get("auth"):
            from classes.indexing_status import SKIP_SIGNIN
            metadata["skip_reason"] = "Sign in to Zenvi to index this file for search."
            metadata["skip_code"] = SKIP_SIGNIN
            self._completed(self.file_data, metadata, None)
            return
        if result.get("credits") or result.get("rate_limited"):
            # out of credits, or over the account's limit: nothing was wrong with the file, so it is skipped, not failed
            block = {"status": "skipped", "error": result["error"], "index_name": index_name, "provider": "gemini-v2", "media_type": media_type}
            metadata["index"], metadata["twelvelabs"] = block, dict(block)
            self._completed(self.file_data, metadata, None)
            return
        if result.get("error"):
            self._v2_failed(metadata, index_name, media_type, result["error"])
            return
        # No desktop charge here: the backend meters /index/v2 itself, from what Gemini really billed.
        meta = build()
        self._progress(file_id, "done", 100)
        self._completed(self.file_data, meta, None)

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
                too_long = media_type != "image" and duration > self._MAX_INDEXING_SECONDS
                if too_long and self._long_file_needs_approval(file_path, duration, metadata):
                    self._completed(self.file_data, metadata, None)
                    return

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
