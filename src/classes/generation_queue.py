"""
 @file
 @brief This file contains a lightweight in-memory generation queue for ComfyUI jobs.
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import uuid
from collections import deque
from threading import Event
from time import monotonic

from qt_api import QObject, QThread, pyqtSignal, pyqtSlot

from classes.comfy_client import ComfyClient
from classes.logger import log


class _GenerationWorker(QObject):
    """Background worker that simulates generation progress for queued jobs."""

    progress_changed = pyqtSignal(str, int)
    progress_detail_changed = pyqtSignal(str, str)
    progress_sub_changed = pyqtSignal(str, int)
    job_finished = pyqtSignal(str, bool, bool, str, object)

    def __init__(self):
        super().__init__()
        self._cancel_requested = set()
        self._job_prompts = {}

    def _is_cancel_requested(self, job_id, cancel_event):
        return (job_id in self._cancel_requested) or (cancel_event is not None and cancel_event.is_set())

    @staticmethod
    def _is_unfinished_meta_batch(history_entry):
        outputs = history_entry.get("outputs", {}) if isinstance(history_entry, dict) else {}
        if not isinstance(outputs, dict):
            return False
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            unfinished = node_out.get("unfinished_batch", None)
            if isinstance(unfinished, list):
                if any(bool(v) for v in unfinished):
                    return True
            elif unfinished:
                return True
        return False

    @staticmethod
    def _history_prompt_meta(history_entry):
        prompt_payload = history_entry.get("prompt", []) if isinstance(history_entry, dict) else []
        if not isinstance(prompt_payload, list):
            return "", 0
        client_payload = prompt_payload[3] if len(prompt_payload) >= 4 else {}
        if not isinstance(client_payload, dict):
            return "", 0
        client_id = str(client_payload.get("client_id", "")).strip()
        create_time = int(client_payload.get("create_time", 0) or 0)
        return client_id, create_time

    @staticmethod
    def _allow_unfiltered_output_fallback(template_id):
        template_id = str(template_id or "").strip().lower()
        # Track-object templates intentionally have multiple save nodes
        # (mask/debug + final), so we must not relax save-node filtering.
        if template_id in (
            "video-blur-anything-sam2",
            "video-mask-anything-sam2",
            "video-highlight-anything-sam2",
            "txt2music-ace-step",
        ):
            return False
        return True

    def _find_related_meta_batch_outputs(self, client, history_entry, save_node_ids, template_id=""):
        base_client_id, base_create_time = self._history_prompt_meta(history_entry)
        if not base_client_id:
            return []
        try:
            history_all = client.history_all() or {}
        except Exception:
            return []
        if not isinstance(history_all, dict):
            return []

        best_create_time = 0
        best_outputs = []
        for entry in history_all.values():
            if not isinstance(entry, dict):
                continue
            status_obj = entry.get("status", {}) if isinstance(entry, dict) else {}
            status_str = str(status_obj.get("status_str", "")).lower()
            if status_str not in ("success", "completed", ""):
                continue
            entry_client_id, entry_create_time = self._history_prompt_meta(entry)
            if entry_client_id != base_client_id:
                continue
            if entry_create_time and base_create_time and entry_create_time < base_create_time:
                continue
            if self._is_unfinished_meta_batch(entry):
                continue

            outputs = ComfyClient.extract_file_outputs(entry, save_node_ids=save_node_ids)
            if (not outputs) and save_node_ids and self._allow_unfiltered_output_fallback(template_id):
                outputs = ComfyClient.extract_file_outputs(entry, save_node_ids=None)
            if not outputs:
                continue

            if entry_create_time >= best_create_time:
                best_create_time = entry_create_time
                best_outputs = outputs

        return best_outputs

    @pyqtSlot(str, object)
    def run_job(self, job_id, request):
        request = request or {}
        cancel_event = request.get("cancel_event")
        if request.get("workflow") and request.get("comfy_url"):
            self._run_comfy_job(job_id, request)
            return

        canceled = False
        for step in range(1, 21):
            QThread.msleep(250)
            if self._is_cancel_requested(job_id, cancel_event):
                canceled = True
                break
            self.progress_changed.emit(job_id, min(step * 5, 99))

        self._cancel_requested.discard(job_id)

        if canceled:
            self.job_finished.emit(job_id, False, True, "", [])
        else:
            self.progress_changed.emit(job_id, 100)
            self.job_finished.emit(job_id, True, False, "", [])

    @pyqtSlot(str)
    def cancel_job(self, job_id):
        log.debug("GenerationWorker cancel_job received job=%s", str(job_id))
        self._cancel_requested.add(job_id)

    def _run_comfy_job(self, job_id, request):
        comfy_url = request.get("comfy_url")
        workflow = request.get("workflow")
        client_id = request.get("client_id") or "openshot-qt"
        timeout_s = int(request.get("timeout_s") or 86400)  # default 24 hours safety cap
        save_node_ids = list(request.get("save_node_ids") or [])
        template_id = str(request.get("template_id") or "")
        cancel_event = request.get("cancel_event")
        client = ComfyClient(comfy_url)
        ws_client = None

        try:
            prompt_id = client.queue_prompt(workflow, client_id)
            if not prompt_id:
                self.job_finished.emit(job_id, False, False, "ComfyUI returned an invalid prompt_id", [])
                return
            self._job_prompts[job_id] = prompt_id
            try:
                ws_client = ComfyClient.open_progress_socket(comfy_url, client_id)
                log.debug("Comfy progress websocket connected for prompt=%s", str(prompt_id))
            except Exception:
                log.debug("Comfy progress websocket unavailable; continuing without live progress", exc_info=True)

            start_time = monotonic()
            last_in_queue_time = start_time
            last_contact_time = start_time
            last_progress_log_time = 0.0
            last_network_error_log_time = 0.0
            progress_endpoint_unavailable = False
            accepted_progress_started = False
            ws_retry_delay_s = 2.0
            ws_next_retry_at = start_time
            ws_last_progress_time = start_time
            ws_stale_reconnect_s = 60.0
            ws_stale_reconnect_max_s = 300.0
            prompt_key = str(prompt_id)
            last_progress_signature = None
            last_progress_detail = ""

            while True:
                if self._is_cancel_requested(job_id, cancel_event):
                    log.debug("Comfy cancel requested for job=%s prompt=%s", job_id, str(prompt_id))
                    cancel_ok = False
                    cancel_errors = []

                    # Retry cancellation a few times and verify prompt no longer appears in Comfy queue.
                    for attempt in range(1, 181):
                        try:
                            cancel_ok = client.cancel_prompt(prompt_id) or cancel_ok
                        except Exception as ex:
                            cancel_errors.append("queue: {}".format(ex))

                        try:
                            cancel_ok = client.interrupt(prompt_id=prompt_id) or cancel_ok
                        except Exception as ex:
                            cancel_errors.append("interrupt: {}".format(ex))

                        try:
                            history = client.history(prompt_id) or {}
                            prompt_key = str(prompt_id)
                            history_entry = history.get(prompt_key) or history.get(prompt_id) or None
                            if isinstance(history_entry, dict):
                                status_obj = history_entry.get("status", {}) if isinstance(history_entry, dict) else {}
                                status_str = str(status_obj.get("status_str", "")).lower()
                                # Comfy commonly marks interrupted runs as failed/error in history.
                                if status_str in ("error", "failed"):
                                    cancel_ok = True
                                    log.debug(
                                        "Comfy cancel confirmed by history status for job=%s prompt=%s status=%s",
                                        job_id,
                                        prompt_key,
                                        status_str,
                                    )
                                    break
                        except Exception as ex:
                            cancel_errors.append("history-check: {}".format(ex))

                        try:
                            queue_data = client.queue() or {}
                            if not ComfyClient.prompt_in_queue(prompt_id, queue_data):
                                cancel_ok = True
                                log.debug(
                                    "Comfy cancel confirmed by queue absence for job=%s prompt=%s on attempt=%s",
                                    job_id,
                                    str(prompt_id),
                                    attempt,
                                )
                                break
                        except Exception as ex:
                            cancel_errors.append("queue-check: {}".format(ex))

                        if attempt % 10 == 0:
                            log.debug(
                                "Comfy cancel still pending job=%s prompt=%s attempt=%s",
                                job_id,
                                str(prompt_id),
                                attempt,
                            )
                        QThread.msleep(500)

                    self._cancel_requested.discard(job_id)
                    self._job_prompts.pop(job_id, None)
                    if cancel_ok:
                        self.job_finished.emit(job_id, False, True, "", [])
                    else:
                        self.job_finished.emit(
                            job_id,
                            False,
                            False,
                            "ComfyUI did not accept cancel request ({})".format("; ".join(cancel_errors) or "unknown"),
                            [],
                        )
                    return

                history_entry = None
                try:
                    history = client.history(prompt_id) or {}
                    history_entry = history.get(prompt_key) or history.get(prompt_id) or None
                    last_contact_time = monotonic()
                except Exception:
                    now_log = monotonic()
                    if (now_log - last_network_error_log_time) > 8.0:
                        log.debug(
                            "Comfy history poll temporarily unavailable for job=%s prompt=%s; retrying",
                            job_id,
                            prompt_key,
                            exc_info=True,
                        )
                        last_network_error_log_time = now_log
                if history_entry is not None:
                    status_obj = history_entry.get("status", {}) if isinstance(history_entry, dict) else {}
                    status_str = str(status_obj.get("status_str", "")).lower()
                    if status_str in ("error", "failed"):
                        error_text = "ComfyUI job failed."
                        messages = status_obj.get("messages", [])
                        if isinstance(messages, list) and messages:
                            error_text = ComfyClient.summarize_error_text(messages[-1])
                        self._job_prompts.pop(job_id, None)
                        self.job_finished.emit(job_id, False, False, error_text, [])
                        return
                    if self._is_unfinished_meta_batch(history_entry):
                        image_outputs = self._find_related_meta_batch_outputs(
                            client,
                            history_entry,
                            save_node_ids,
                            template_id=template_id,
                        )
                        if image_outputs:
                            self.progress_changed.emit(job_id, 100)
                            self._job_prompts.pop(job_id, None)
                            self.job_finished.emit(job_id, True, False, "", image_outputs)
                            return
                        # Meta batch uses follow-up prompts under the same client_id.
                        # Keep polling progress/queue while waiting for follow-up prompt outputs.
                    else:
                        image_outputs = ComfyClient.extract_file_outputs(history_entry, save_node_ids=save_node_ids)
                        if (not image_outputs) and save_node_ids and self._allow_unfiltered_output_fallback(template_id):
                            # Fallback for workflows whose output node ids shift or emit non-standard keys.
                            image_outputs = ComfyClient.extract_file_outputs(history_entry, save_node_ids=None)
                        self.progress_changed.emit(job_id, 100)
                        self._job_prompts.pop(job_id, None)
                        self.job_finished.emit(job_id, True, False, "", image_outputs)
                        return

                # Query ComfyUI's live progress values when available.
                try:
                    ws_progress_emitted = False
                    now = monotonic()
                    if ws_client is None and now >= ws_next_retry_at:
                        try:
                            ws_client = ComfyClient.open_progress_socket(comfy_url, client_id)
                            ws_retry_delay_s = 2.0
                            log.debug("Comfy progress websocket reconnected for prompt=%s", prompt_key)
                        except Exception:
                            ws_next_retry_at = now + ws_retry_delay_s
                            ws_retry_delay_s = min(60.0, ws_retry_delay_s * 1.5)
                            now_log = monotonic()
                            if (now_log - last_network_error_log_time) > 8.0:
                                log.debug(
                                    "Comfy websocket reconnect failed for job=%s prompt=%s; retrying in %.1fs",
                                    job_id,
                                    prompt_key,
                                    ws_retry_delay_s,
                                    exc_info=True,
                                )
                                last_network_error_log_time = now_log

                    if ws_client is not None:
                        try:
                            # Accept progress from follow-up prompts as well (meta-batch).
                            progress_event = ws_client.poll_progress(prompt_id=None)
                        except Exception:
                            progress_event = None
                            try:
                                ws_client.close()
                            except Exception:
                                pass
                            ws_client = None
                            ws_next_retry_at = monotonic() + ws_retry_delay_s
                            ws_retry_delay_s = min(60.0, ws_retry_delay_s * 1.5)
                            now_log = monotonic()
                            if (now_log - last_network_error_log_time) > 8.0:
                                log.debug(
                                    "Comfy websocket progress read failed for job=%s prompt=%s; switching to retry mode",
                                    job_id,
                                    prompt_key,
                                    exc_info=True,
                                )
                                last_network_error_log_time = now_log

                        if progress_event is not None:
                            elapsed = monotonic() - start_time
                            progress = int(progress_event.get("percent", 0))
                            raw_value = float(progress_event.get("value", 0.0))
                            raw_max = float(progress_event.get("max", 0.0))
                            progress_type = str(progress_event.get("type", ""))
                            progress_node = str(progress_event.get("node", ""))
                            # Some workflows emit near-complete progress bursts at startup
                            # (e.g. tiny setup nodes), then reset to sampler progress.
                            # Ignore those bootstrap spikes for a short window.
                            if (
                                (not accepted_progress_started)
                                and progress >= 95
                                and elapsed < 20.0
                                and raw_max <= 1.0
                            ):
                                log.debug(
                                    "Comfy WS progress setup-node spike ignored job=%s prompt=%s node=%s type=%s value=%s max=%s percent=%s elapsed=%.2fs",
                                    job_id,
                                    prompt_key,
                                    progress_node,
                                    progress_type,
                                    raw_value,
                                    raw_max,
                                    progress,
                                    elapsed,
                                )
                            elif (not accepted_progress_started) and progress >= 95 and elapsed < 20.0:
                                log.debug(
                                    "Comfy WS progress bootstrap spike ignored job=%s prompt=%s percent=%s elapsed=%.2fs",
                                    job_id,
                                    prompt_key,
                                    progress,
                                    elapsed,
                                )
                            else:
                                accepted_progress_started = True
                                progress_signature = (
                                    progress_type,
                                    progress_node,
                                    int(progress),
                                    round(raw_value, 3),
                                    round(raw_max, 3),
                                )
                                if progress_signature != last_progress_signature:
                                    inferred_progress = int(max(0, min(99, progress)))
                                    detail_text = ""
                                    if progress_node:
                                        detail_text = "node {} {}%".format(progress_node, int(progress))

                                    log.debug(
                                        "Comfy WS progress emit job=%s prompt=%s node=%s type=%s value=%s max=%s percent=%s",
                                        job_id,
                                        prompt_key,
                                        progress_node,
                                        progress_type,
                                        raw_value,
                                        raw_max,
                                        inferred_progress,
                                    )
                                    self.progress_changed.emit(job_id, inferred_progress)
                                    self.progress_sub_changed.emit(job_id, int(max(0, min(99, progress))))
                                    if detail_text != last_progress_detail:
                                        self.progress_detail_changed.emit(job_id, detail_text)
                                        last_progress_detail = detail_text
                                    last_progress_signature = progress_signature
                                ws_progress_emitted = True
                                ws_last_progress_time = monotonic()
                                ws_stale_reconnect_s = 60.0
                                last_contact_time = monotonic()
                    if ws_client is not None and not ws_progress_emitted:
                        stale_for = now - ws_last_progress_time
                        if stale_for >= ws_stale_reconnect_s:
                            try:
                                ws_client.close()
                            except Exception:
                                pass
                            ws_client = None
                            ws_next_retry_at = now + ws_retry_delay_s
                            ws_retry_delay_s = min(60.0, ws_retry_delay_s * 1.5)
                            next_stale_reconnect_s = min(
                                ws_stale_reconnect_max_s,
                                max(60.0, ws_stale_reconnect_s * 1.5),
                            )
                            log.debug(
                                "Comfy websocket stalled for job=%s prompt=%s (%.1fs >= %.1fs); forcing reconnect, next stall timeout %.1fs",
                                job_id,
                                prompt_key,
                                stale_for,
                                ws_stale_reconnect_s,
                                next_stale_reconnect_s,
                            )
                            ws_stale_reconnect_s = next_stale_reconnect_s
                    # Use HTTP /progress only when websocket progress is unavailable.
                    # If websocket is connected but temporarily quiet, keep waiting for WS
                    # instead of spamming a misleading 404 fallback warning.
                    if ws_client is None:
                        progress_data = client.progress()
                        if progress_data is None:
                            if not progress_endpoint_unavailable:
                                log.debug(
                                    "Comfy progress endpoint unavailable (404); waiting for websocket progress for job=%s",
                                    job_id,
                                )
                            progress_endpoint_unavailable = True
                            progress_data = {}

                        progress_block = progress_data.get("progress", progress_data)
                        if not isinstance(progress_block, dict):
                            progress_block = {}

                        value = float(progress_block.get("value", progress_block.get("current", 0.0)))
                        maximum = float(progress_block.get("max", progress_block.get("total", 0.0)))
                        progress_prompt = str(
                            progress_data.get("prompt_id", progress_block.get("prompt_id", ""))
                        )
                        prompt_matches = (not progress_prompt) or (progress_prompt == prompt_key)

                        now_log = monotonic()
                        if (now_log - last_progress_log_time) > 8.0:
                            log.debug(
                                "Comfy progress poll job=%s prompt=%s payload_keys=%s value=%s max=%s progress_prompt=%s prompt_match=%s",
                                job_id,
                                prompt_key,
                                list(progress_data.keys()) if isinstance(progress_data, dict) else type(progress_data),
                                value,
                                maximum,
                                progress_prompt,
                                prompt_matches,
                            )
                            last_progress_log_time = now_log

                        if maximum > 0 and prompt_matches:
                            progress = int(max(0, min(99, round((value / maximum) * 100.0))))
                            progress_signature = ("poll", "", int(progress), round(value, 3), round(maximum, 3))
                            if progress_signature != last_progress_signature:
                                log.debug(
                                    "Comfy progress emit job=%s prompt=%s value=%s max=%s percent=%s",
                                    job_id,
                                    prompt_key,
                                    value,
                                    maximum,
                                    progress,
                                )
                                self.progress_changed.emit(job_id, progress)
                                self.progress_sub_changed.emit(job_id, int(max(0, min(99, progress))))
                                if last_progress_detail:
                                    self.progress_detail_changed.emit(job_id, "")
                                    last_progress_detail = ""
                                last_progress_signature = progress_signature
                            last_contact_time = monotonic()
                except Exception:
                    # Keep polling history and queue even if /progress is unavailable.
                    now_log = monotonic()
                    if (now_log - last_network_error_log_time) > 8.0:
                        log.debug("Comfy progress poll failed for job=%s", job_id, exc_info=True)
                        last_network_error_log_time = now_log

                # Check queue to avoid timing out long-running but active jobs.
                in_queue = False
                try:
                    queue_data = client.queue() or {}
                    in_queue = ComfyClient.prompt_in_queue(prompt_id, queue_data)
                    last_contact_time = monotonic()
                except Exception:
                    # If queue check fails, do not penalize the job immediately.
                    in_queue = True
                    now_log = monotonic()
                    if (now_log - last_network_error_log_time) > 8.0:
                        log.debug("Comfy queue check temporarily unavailable for job=%s", job_id, exc_info=True)
                        last_network_error_log_time = now_log
                if in_queue:
                    last_in_queue_time = monotonic()
                else:
                    now_log = monotonic()
                    if (now_log - last_progress_log_time) > 8.0:
                        log.debug(
                            "Comfy queue check: prompt=%s not found in queue_running/queue_pending yet",
                            prompt_key,
                        )
                        last_progress_log_time = now_log

                now = monotonic()
                if (now - start_time) > timeout_s:
                    self._job_prompts.pop(job_id, None)
                    self.job_finished.emit(job_id, False, False, "Timed out waiting for ComfyUI history result", [])
                    return

                if (now - last_contact_time) > 60.0:
                    now_log = monotonic()
                    if (now_log - last_network_error_log_time) > 8.0:
                        log.debug(
                            "Comfy connection degraded for job=%s prompt=%s (no successful API contact for %.1fs); continuing retries",
                            job_id,
                            prompt_key,
                            now - last_contact_time,
                        )
                        last_network_error_log_time = now_log

                # If prompt vanished from queue for an extended period and still no history, treat as failure.
                if (now - last_in_queue_time) > 600:
                    self._job_prompts.pop(job_id, None)
                    self.job_finished.emit(
                        job_id,
                        False,
                        False,
                        "ComfyUI prompt is no longer in queue and has no history result.",
                        [],
                    )
                    return
                QThread.msleep(500)
        except Exception as ex:
            self._job_prompts.pop(job_id, None)
            self.job_finished.emit(job_id, False, False, ComfyClient.summarize_error_text(ex), [])
        finally:
            if ws_client is not None:
                ws_client.close()


class GenerationQueueManager(QObject):
    """Single-worker, in-memory generation queue with per-file active-job limits."""

    ACTIVE_STATES = {"queued", "running", "canceling"}

    job_added = pyqtSignal(str, object)
    job_updated = pyqtSignal(str, str, int)
    job_finished = pyqtSignal(str, str)
    job_removed = pyqtSignal(str)
    file_job_changed = pyqtSignal(str)
    queue_changed = pyqtSignal()

    _run_job = pyqtSignal(str, object)
    _cancel_job = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs = {}
        self._queued = deque()
        self._running_job_id = None
        self._active_file_jobs = {}

        self._thread = QThread(self)
        self._thread.setObjectName("generation_queue_worker")
        self._worker = _GenerationWorker()
        self._worker.moveToThread(self._thread)
        self._run_job.connect(self._worker.run_job)
        self._cancel_job.connect(self._worker.cancel_job)
        self._worker.progress_changed.connect(self._on_progress_changed)
        self._worker.progress_detail_changed.connect(self._on_progress_detail_changed)
        self._worker.progress_sub_changed.connect(self._on_progress_sub_changed)
        self._worker.job_finished.connect(self._on_job_finished)
        self._thread.start()

    def enqueue(self, name, template_id, prompt, source_file_id=None, request=None):
        source_file_id = str(source_file_id or "")
        if source_file_id and self.get_active_job_for_file(source_file_id):
            return None

        job_id = str(uuid.uuid4())
        cancel_event = Event()
        job_request = dict(request or {})
        job_request["cancel_event"] = cancel_event
        job = {
            "id": job_id,
            "name": str(name or "").strip(),
            "template_id": str(template_id or "").strip(),
            "prompt": str(prompt or "").strip(),
            "source_file_id": source_file_id,
            "status": "queued",
            "progress": 0,
            "sub_progress": 0,
            "progress_detail": "",
            "error": "",
            "request": job_request,
            "cancel_event": cancel_event,
        }
        self.jobs[job_id] = job
        self._queued.append(job_id)
        if source_file_id:
            self._active_file_jobs[source_file_id] = job_id

        self.job_added.emit(job_id, source_file_id)
        self.job_updated.emit(job_id, "queued", 0)
        self._emit_file_changed(source_file_id)
        self.queue_changed.emit()
        self._start_next_if_idle()
        return job_id

    def cancel_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            log.debug("GenerationQueue cancel_job ignored; unknown job=%s", str(job_id))
            return False

        log.debug(
            "GenerationQueue cancel_job request job=%s status=%s source_file_id=%s",
            str(job_id),
            str(job.get("status", "")),
            str(job.get("source_file_id", "")),
        )
        if job["status"] == "queued":
            cancel_event = job.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
                log.debug("GenerationQueue cancel_event set for queued job=%s", str(job_id))
            job["status"] = "canceled"
            self._queued = deque([queued_id for queued_id in self._queued if queued_id != job_id])
            self._release_file_slot(job.get("source_file_id", ""))
            self.job_updated.emit(job_id, "canceled", int(job.get("progress", 0)))
            self.job_finished.emit(job_id, "canceled")
            self._emit_file_changed(job.get("source_file_id", ""))
            self.queue_changed.emit()
            log.debug("GenerationQueue cancel_job completed for queued job=%s", str(job_id))
            return True

        if job["status"] == "running":
            cancel_event = job.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
                log.debug("GenerationQueue cancel_event set for running job=%s", str(job_id))
            job["status"] = "canceling"
            self.job_updated.emit(job_id, "canceling", int(job.get("progress", 0)))
            self._cancel_job.emit(job_id)
            self._emit_file_changed(job.get("source_file_id", ""))
            self.queue_changed.emit()
            log.debug("GenerationQueue cancel_job emitted worker cancel for running job=%s", str(job_id))
            return True

        log.debug("GenerationQueue cancel_job ignored for job=%s with status=%s", str(job_id), str(job.get("status", "")))
        return False

    def cancel_jobs_for_file(self, source_file_id):
        source_file_id = str(source_file_id or "")
        if not source_file_id:
            return
        for job in list(self.jobs.values()):
            if job.get("source_file_id") == source_file_id and job.get("status") in self.ACTIVE_STATES:
                self.cancel_job(job["id"])

    def remove_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.get("status") in self.ACTIVE_STATES:
            return False

        source_file_id = job.get("source_file_id", "")
        self.jobs.pop(job_id, None)
        self.job_removed.emit(job_id)
        self._emit_file_changed(source_file_id)
        self.queue_changed.emit()
        return True

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_active_job_for_file(self, source_file_id):
        source_file_id = str(source_file_id or "")
        if not source_file_id:
            return None

        job_id = self._active_file_jobs.get(source_file_id)
        if not job_id:
            return None

        job = self.jobs.get(job_id)
        if not job or job.get("status") not in self.ACTIVE_STATES:
            self._active_file_jobs.pop(source_file_id, None)
            return None
        return job

    def get_file_badge(self, source_file_id):
        job = self.get_active_job_for_file(source_file_id)
        if not job:
            return None

        status = job.get("status")
        progress = int(job.get("progress", 0))
        sub_progress = int(job.get("sub_progress", 0))
        detail = str(job.get("progress_detail", "") or "").strip()
        if status == "queued":
            label = "Queued"
        elif status == "running":
            label = "Generating {}%".format(progress)
            if detail:
                label = "{} ({})".format(label, detail)
        elif status == "canceling":
            label = "Canceling..."
        else:
            label = status.capitalize()

        return {
            "status": status,
            "progress": progress,
            "sub_progress": sub_progress,
            "label": label,
            "job_id": job.get("id"),
        }

    def shutdown(self):
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)

    def _start_next_if_idle(self):
        if self._running_job_id is not None:
            return
        if not self._queued:
            return

        next_job_id = self._queued.popleft()
        job = self.jobs.get(next_job_id)
        if not job:
            self._start_next_if_idle()
            return

        self._running_job_id = next_job_id
        job["status"] = "running"
        job["progress"] = int(job.get("progress", 0))
        job["sub_progress"] = int(job.get("sub_progress", 0))
        self.job_updated.emit(next_job_id, "running", int(job["progress"]))
        self._emit_file_changed(job.get("source_file_id", ""))
        self.queue_changed.emit()
        self._run_job.emit(next_job_id, job.get("request", {}))

    def _release_file_slot(self, source_file_id):
        source_file_id = str(source_file_id or "")
        if source_file_id:
            self._active_file_jobs.pop(source_file_id, None)

    def _emit_file_changed(self, source_file_id):
        source_file_id = str(source_file_id or "")
        if source_file_id:
            self.file_job_changed.emit(source_file_id)

    @pyqtSlot(str, int)
    def _on_progress_changed(self, job_id, progress):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.get("status") not in ("running", "canceling"):
            return
        job["progress"] = int(progress)
        self.job_updated.emit(job_id, job.get("status"), int(progress))
        self._emit_file_changed(job.get("source_file_id", ""))
        self.queue_changed.emit()

    @pyqtSlot(str, str)
    def _on_progress_detail_changed(self, job_id, detail):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.get("status") not in ("running", "canceling"):
            return
        detail_text = str(detail or "").strip()
        if str(job.get("progress_detail", "") or "") == detail_text:
            return
        job["progress_detail"] = detail_text
        self.job_updated.emit(job_id, job.get("status"), int(job.get("progress", 0)))
        self._emit_file_changed(job.get("source_file_id", ""))
        self.queue_changed.emit()

    @pyqtSlot(str, int)
    def _on_progress_sub_changed(self, job_id, progress):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.get("status") not in ("running", "canceling"):
            return
        p = int(max(0, min(99, progress)))
        if int(job.get("sub_progress", 0)) == p:
            return
        job["sub_progress"] = p
        self.job_updated.emit(job_id, job.get("status"), int(job.get("progress", 0)))
        self._emit_file_changed(job.get("source_file_id", ""))
        self.queue_changed.emit()

    @pyqtSlot(str, bool, bool, str, object)
    def _on_job_finished(self, job_id, success, canceled, error, outputs):
        job = self.jobs.get(job_id)
        if not job:
            return

        if canceled:
            job["status"] = "canceled"
        elif success:
            job["status"] = "completed"
            job["progress"] = 100
            job["outputs"] = list(outputs or [])
        else:
            job["status"] = "failed"
            job["error"] = str(error or "")

        source_file_id = job.get("source_file_id", "")
        self._release_file_slot(source_file_id)

        self.job_updated.emit(job_id, job["status"], int(job.get("progress", 0)))
        self.job_finished.emit(job_id, job["status"])
        self._emit_file_changed(source_file_id)
        self.queue_changed.emit()

        if self._running_job_id == job_id:
            self._running_job_id = None
        self._start_next_if_idle()
