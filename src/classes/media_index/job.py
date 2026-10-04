"""
 @file
 @brief The indexing job: sign-in check, reuse of a saved analysis, upload, charge, keep.

 This is the logic of the background indexing worker, with no Qt in it, so it is tested in
 the ordinary headless suite. ``windows.models.files_model.BackendIndexingWorker`` is a thin
 QThread around it: the three ``emit_*`` callbacks are its signals, and it runs on the
 worker thread, never the GUI thread.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from classes.logger import log


class IndexingJob:
    """Index one file (or reuse a saved analysis of the same content) and report back."""

    # Hard limit: clips longer than 30 minutes are not indexed or summarized.
    _MAX_INDEXING_SECONDS = 30 * 60

    def __init__(
        self,
        file_data: Dict[str, Any],
        project_id: str = "",
        summarize_only: bool = False,
        *,
        client_factory: Callable[[], Any],
        emit_completed: Callable[[dict, Any, Any], None],
        emit_progress: Callable[[str, str, int], None],
        emit_intermediate: Callable[[str, Any], None],
    ) -> None:
        self.file_data = file_data
        self.project_id = project_id or ""
        self.summarize_only = bool(summarize_only)
        self._client_factory = client_factory
        self._completed = emit_completed
        self._progress = emit_progress
        self._intermediate = emit_intermediate
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the job to stop at its next check (called from another thread at app quit)."""
        self._cancelled = True

    def _run_local_facts(self, file_path, media_type, file_id):
        """The local analysis layers (shots, colour, audio, transcript), when the preference is on.

        Runs before any cloud step and needs no sign-in, so it works offline. It never raises:
        whatever happens here, indexing carries on exactly as it did before.
        """
        try:
            from classes.media_index.flags import v2_enabled
            if not v2_enabled():
                return
            from classes.media_index import facts
            self._progress(file_id, "analyzing", 0)
            facts.compute_facts(
                file_path,
                fingerprint=self.file_data.get("fingerprint"),
                media_type=media_type,
                should_cancel=lambda: self._cancelled,
                on_progress=lambda f: self._progress(file_id, "analyzing", int(f * 100)),
            )
        except Exception as exc:  # includes facts.Cancelled
            log.info("Local media analysis stopped for %s: %s", file_path, exc)

    def _v2_index_block(self, index_name, file_id, media_type, sha):
        block = {"status": "ready", "index_id": index_name, "index_name": index_name, "video_id": file_id,
                 "provider": "gemini-v2", "media_type": media_type, "v2": True, "fingerprint": sha}
        return block

    def _try_v2(self, client, file_path, media_type, duration, file_id, index_name, metadata):
        """The media index v2 path (preference on). Returns True when it finished the job.

        False means "carry on with the original indexing": no fingerprint, or a backend that
        has no v2 routes yet. A file whose v2 layers are already on the shelf is restored for
        free, in any project, with no sign-in and no charge.
        """
        from classes.media_index import S_WATCH, S_VECTORS, cloud, default_shelf
        from classes.media_index import schema as S

        sha = self._fingerprint_sha(file_path)
        if not sha:
            return False
        shelf = default_shelf()

        def ready(layer):
            # any saved version counts: an older cloud layer is kept, not paid for again
            return shelf.layer_ready(sha, layer)

        def build():
            structure = shelf.read_json(sha, "structure.json") or {}
            watch = shelf.read_json(sha, "watch.json") or {"shots": []}
            speech = shelf.read_json(sha, "speech.json") if ready(S.LAYER_SPEECH) else None
            meta = cloud.v1_metadata_from_v2(watch, speech, structure, media_type)
            block = self._v2_index_block(index_name, file_id, media_type, sha)
            meta["index"], meta["twelvelabs"] = block, dict(block)
            return meta

        done_watch = ready(S_WATCH) or media_type != "video"
        if done_watch and ready(S_VECTORS):
            self._progress(file_id, "done", 100)
            self._completed(self.file_data, build(), None)
            return True

        if not self._signed_in():
            from classes.indexing_status import SKIP_SIGNIN
            metadata["skip_reason"] = "Sign in to Zenvi to index this file for search."
            metadata["skip_code"] = SKIP_SIGNIN
            self._completed(self.file_data, metadata, None)
            return True

        from classes.credits_client import charge_operation_on_success, check_operation
        credit_duration = duration if media_type != "image" else 60.0
        _, _balance, blocked = check_operation("indexing_per_minute", f"{media_type} indexing", duration_seconds=credit_duration)
        if blocked:
            block = {"status": "skipped", "error": blocked, "index_name": index_name, "provider": "gemini-v2", "media_type": media_type}
            metadata["index"], metadata["twelvelabs"] = block, dict(block)
            self._completed(self.file_data, metadata, None)
            return True

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
            return True
        if result.get("unsupported"):
            log.info("This backend has no media index v2; using the original indexing")
            return False
        if result.get("auth"):
            from classes.indexing_status import SKIP_SIGNIN
            metadata["skip_reason"] = "Sign in to Zenvi to index this file for search."
            metadata["skip_code"] = SKIP_SIGNIN
            self._completed(self.file_data, metadata, None)
            return True
        if result.get("error"):
            block = {"status": "failed", "error": result["error"], "index_name": index_name, "provider": "gemini-v2", "media_type": media_type}
            metadata["index"], metadata["twelvelabs"], metadata["error"] = block, dict(block), result["error"]
            self._completed(self.file_data, metadata, None)
            return True
        charge_operation_on_success(True, "indexing_per_minute", provider="gemini", note=f"import {file_id}",
                                    duration_seconds=credit_duration)
        meta = build()
        self._progress(file_id, "done", 100)
        self._completed(self.file_data, meta, None)
        return True

    def run(self):
        import os as _os

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

                self._run_local_facts(file_path, media_type, file_id)
                if self._cancelled:
                    self._completed(self.file_data, metadata, None)
                    return
                duration = float(self.file_data.get("duration") or 0)
                if media_type != "image" and duration > self._MAX_INDEXING_SECONDS:
                    log.warning(
                        "Skipping indexing+summarize for %s: duration %.0fs > 30-minute limit.",
                        file_path, duration,
                    )
                    metadata["skip_reason"] = (
                        f"Clip duration {duration / 60:.1f} min exceeds the 30-minute limit. "
                        "Indexing and description generation were skipped."
                    )
                    self._completed(self.file_data, metadata, None)
                    return

                filename = _os.path.basename(file_path)
                from classes.project_tl_index import build_project_index_name
                from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

                index_name = build_project_index_name(self.project_id)

                from classes.media_index.flags import v2_enabled
                if not self.summarize_only and v2_enabled() and self._try_v2(
                        client, file_path, media_type, duration, file_id, index_name, metadata):
                    return

                indexing_configured = client.is_indexing_configured()

                existing_ai = self.file_data.get("ai_metadata") or {}
                existing_idx = get_index_block(existing_ai)
                already_indexed = twelvelabs_is_indexed(existing_idx)

                if already_indexed and self.summarize_only:
                    metadata = dict(existing_ai) if isinstance(existing_ai, dict) else metadata
                    metadata["index"] = dict(existing_idx)
                    metadata["twelvelabs"] = dict(existing_idx)
                    metadata["error"] = (
                        "Summarize-only is not supported for Gemini indexing. "
                        "Reindex the clip to refresh descriptions."
                    )
                    self._completed(self.file_data, metadata, None)
                    return

                if already_indexed and not self.summarize_only:
                    metadata = dict(existing_ai) if isinstance(existing_ai, dict) else metadata
                    metadata["index"] = dict(existing_idx)
                    metadata["twelvelabs"] = dict(existing_idx)
                    metadata["analyzed"] = bool(metadata.get("analyzed"))
                    # An analysis made before the shelf existed becomes reusable by other
                    # projects too (this runs on the worker thread, never the GUI thread).
                    self._seed_shelf(metadata, file_path, media_type, duration, file_id)
                    self._completed(self.file_data, metadata, None)
                    return

                if not indexing_configured:
                    metadata["error"] = "Gemini indexing is not configured on the backend (GOOGLE_API_KEY)."
                    self._completed(self.file_data, metadata, None)
                    return

                if not self._signed_in():
                    # Not a failure: the badge says "Sign in to index" and the file is
                    # queued again as soon as a session is saved.
                    from classes.indexing_status import SKIP_SIGNIN
                    metadata["skip_reason"] = "Sign in to Zenvi to index this file for search."
                    metadata["skip_code"] = SKIP_SIGNIN
                    self._completed(self.file_data, metadata, None)
                    return

                fp_sha = self._fingerprint_sha(file_path)
                if not self.summarize_only:
                    restored = self._restore_from_shelf(
                        client, fp_sha, media_type=media_type, duration=duration,
                        file_id=file_id, filename=filename, index_name=index_name,
                    )
                    if restored is not None:
                        # Same content indexed before: only the search rows are rebuilt, so
                        # nothing is uploaded, analysed or charged again.
                        self._progress(file_id, "done", 100)
                        self._completed(self.file_data, restored, None)
                        return

                try:
                    from classes.credits_client import check_operation

                    credit_duration = duration if media_type != "image" else 60.0
                    _, balance, blocked = check_operation(
                        "indexing_per_minute",
                        f"{media_type} indexing",
                        duration_seconds=credit_duration,
                    )
                    if blocked:
                        skip_block = {
                            "status": "skipped",
                            "error": blocked,
                            "index_name": index_name,
                            "provider": "gemini",
                            "media_type": media_type,
                        }
                        metadata["index"] = skip_block
                        metadata["twelvelabs"] = skip_block
                        self._completed(self.file_data, metadata, None)
                        return
                except Exception as cred_exc:
                    log.warning("Indexing credits check failed: %s", cred_exc)
                    fail_block = {
                        "status": "failed",
                        "error": str(cred_exc),
                        "index_name": index_name,
                        "provider": "gemini",
                        "media_type": media_type,
                    }
                    metadata["index"] = fail_block
                    metadata["twelvelabs"] = fail_block
                    self._completed(self.file_data, metadata, None)
                    return

                def _progress_cb(phase, percent):
                    self._progress(file_id, phase, percent)

                self._progress(file_id, "uploading", 0)
                partial = client._empty_ai_metadata()
                partial["media_type"] = media_type
                partial["index"] = {
                    "status": "indexing",
                    "index_name": index_name,
                    "video_id": file_id,
                    "provider": "gemini",
                    "media_type": media_type,
                }
                partial["twelvelabs"] = dict(partial["index"])
                self._intermediate(file_id, partial)

                s = client._new_http_session()
                try:
                    idx_result = client.start_direct_indexing_job(
                        file_path,
                        index_name,
                        file_id=file_id,
                        filename=filename,
                        session=s,
                        progress_callback=_progress_cb,
                        project_id=self.project_id,
                        duration_sec=duration,
                        force=bool(self.summarize_only),
                        media_type=media_type,
                    )
                except Exception as idx_exc:
                    log.warning("Gemini indexing failed: %s", idx_exc)
                    fail_block = {
                        "status": "failed",
                        "error": str(idx_exc),
                        "index_name": index_name,
                        "provider": "gemini",
                        "media_type": media_type,
                    }
                    metadata["index"] = fail_block
                    metadata["twelvelabs"] = fail_block
                    self._completed(self.file_data, metadata, None)
                    return

                if isinstance(idx_result, dict) and idx_result.get("error") and not idx_result.get("ai_metadata"):
                    log.warning("Gemini indexing returned error: %s", idx_result.get("error"))
                    fail_block = {
                        "status": "failed",
                        "error": idx_result.get("error"),
                        "index_name": index_name,
                        "provider": "gemini",
                        "media_type": media_type,
                    }
                    metadata["index"] = fail_block
                    metadata["twelvelabs"] = fail_block
                    metadata["error"] = idx_result.get("error")
                    self._completed(self.file_data, metadata, None)
                    return

                has_payload = isinstance(idx_result, dict) and (
                    idx_result.get("index_id")
                    or idx_result.get("ai_metadata")
                    or (idx_result.get("video_id") and not idx_result.get("error"))
                )
                if has_payload:
                    from classes.credits_client import charge_operation_on_success
                    charge_operation_on_success(
                        True,
                        "indexing_per_minute",
                        provider="gemini",
                        note=f"import {file_id}",
                        duration_seconds=duration if media_type != "image" else 60.0,
                    )
                    ai_meta = idx_result.get("ai_metadata")
                    if isinstance(ai_meta, dict) and ai_meta:
                        metadata = ai_meta
                    index_id = str(idx_result.get("index_id") or index_name)
                    video_id = str(idx_result.get("video_id") or file_id)
                    index_block = {
                        "status": "ready",
                        "index_id": index_id,
                        "video_id": video_id,
                        "index_name": index_name,
                        "provider": "gemini",
                        "media_type": media_type,
                    }
                    if isinstance(metadata.get("index"), dict):
                        index_block.update(metadata["index"])
                        index_block["status"] = "ready"
                        index_block["media_type"] = media_type
                        index_block["index_id"] = index_id or index_block.get("index_id") or index_name
                    metadata["index"] = index_block
                    metadata["twelvelabs"] = dict(index_block)
                    metadata["provider"] = "gemini-flash"
                    metadata["media_type"] = media_type
                    if metadata.get("analyzed"):
                        self._save_to_shelf(fp_sha, metadata, media_type, duration, file_id)
                        self._progress(file_id, "done", 100)
                    log.info(
                        "Gemini indexing complete: index=%s index_id=%s video_id=%s media=%s analyzed=%s",
                        index_name, index_id, video_id, media_type, metadata.get("analyzed"),
                    )
                else:
                    err = ""
                    if isinstance(idx_result, dict):
                        err = str(
                            idx_result.get("error")
                            or idx_result.get("message")
                            or ""
                        ).strip()
                    metadata["error"] = err or "Indexing returned no index_id"
                    fail_block = {
                        "status": "failed",
                        "error": metadata["error"],
                        "index_name": index_name,
                        "provider": "gemini",
                        "media_type": media_type,
                    }
                    metadata["index"] = fail_block
                    metadata["twelvelabs"] = fail_block
                    log.warning(
                        "Gemini indexing missing payload for %s: %s",
                        file_id,
                        idx_result,
                    )
        except Exception as exc:
            error = exc
            log.error(f"Backend indexing/summarize worker failed: {exc}")
        self._completed(self.file_data, metadata, error)

    # -- media index shelf ---------------------------------------------------------
    @staticmethod
    def _signed_in():
        """True when a Zenvi session exists. Unknown counts as signed in: the credit
        check that follows then reports the real problem."""
        try:
            from classes.auth_manager import AuthManager
            return bool(AuthManager.instance().is_authenticated())
        except Exception:
            return True

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

    def _restore_from_shelf(self, client, fp_sha, *, media_type, duration, file_id, filename, index_name):
        """Metadata for this file rebuilt from a saved analysis, or None to index normally."""
        if not fp_sha:
            return None
        try:
            from classes.media_index import default_shelf
            from classes.media_index.reuse import restored_metadata, reusable_analysis
            saved = reusable_analysis(default_shelf(), fp_sha, media_type=media_type, duration=duration)
            if saved is None:
                return None
            self._progress(file_id, "indexing", -1)
            meta = restored_metadata(saved, index_name=index_name, file_id=file_id, media_type=media_type)
            result = client.restore_index(
                meta, file_id=file_id, project_id=self.project_id, index_name=index_name,
                filename=filename, media_type=media_type, duration_sec=duration,
                session=client._new_http_session(),
            )
            if isinstance(result, dict) and result.get("success"):
                return meta
            log.info("Saved analysis for %s not restored (%s); indexing normally",
                     file_id, (result or {}).get("error"))
        except Exception:
            log.warning("Restoring a saved analysis failed; indexing normally", exc_info=True)
        return None

    def _save_to_shelf(self, fp_sha, metadata, media_type, duration, file_id):
        if not fp_sha:
            return
        try:
            from classes.media_index import default_shelf
            default_shelf().save_v1_index(
                fp_sha, metadata, duration=duration, media_type=media_type,
                project_id=self.project_id, file_id=file_id,
            )
        except Exception:
            log.warning("Could not save the analysis to the media index", exc_info=True)

    def _seed_shelf(self, metadata, file_path, media_type, duration, file_id):
        """Save an existing project analysis to the shelf if it is not there yet."""
        try:
            if not metadata.get("analyzed"):
                return
            fp_sha = self._fingerprint_sha(file_path)
            if not fp_sha:
                return
            from classes.media_index import LAYER_V1, default_shelf
            if default_shelf().layer_ready(fp_sha, LAYER_V1, version=1):
                return
            self._save_to_shelf(fp_sha, metadata, media_type, duration, file_id)
        except Exception:
            log.debug("Could not seed the media index", exc_info=True)
