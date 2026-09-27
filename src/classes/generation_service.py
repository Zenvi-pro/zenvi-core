"""
 @file
 @brief This file contains Comfy generation orchestration logic.
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

import os
import re
import tempfile
import json
import random
from time import time
from urllib.parse import unquote
from fractions import Fraction

import openshot
from qt_api import QObject, QThread, pyqtSignal, pyqtSlot
from qt_api import QMessageBox, QDialog

from classes import info
from classes import time_parts
from classes.app import get_app
from classes.comfy_client import ComfyClient
from classes.comfy_templates import ComfyTemplateRegistry
from classes.logger import log
from classes.query import File
from windows.generate import GenerateMediaDialog


RASTER_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
}


def is_supported_img2img_path(path):
    path_text = str(path or "").strip()
    # Comfy annotated paths can look like: "image.jpg [input]"
    if path_text.endswith("]") and " [" in path_text:
        path_text = path_text.rsplit(" [", 1)[0].strip()
    ext = os.path.splitext(path_text)[1].lower()
    return ext in RASTER_IMAGE_EXTENSIONS


class _ComfyAvailabilityWorker(QObject):
    finished = pyqtSignal(int, str, bool, str)

    @pyqtSlot(int, str, float)
    def check(self, request_id, url, timeout):
        available = False
        error_text = ""
        try:
            available = ComfyClient(url).ping(timeout=float(timeout))
        except Exception as ex:
            error_text = str(ex)
        self.finished.emit(int(request_id), str(url), bool(available), str(error_text or ""))


class GenerationService(QObject):
    """Encapsulates generation-specific UI + workflow behavior."""
    SAM2_DEFAULT_TARGET_BATCH_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
    SAM2_ESTIMATED_BYTES_PER_PIXEL = 24.0
    SAM2_ESTIMATED_BYTES_PER_PIXEL_HIGHLIGHT = 64.0
    SAM2_ESTIMATED_BYTES_PER_PIXEL_BLUR = 40.0
    SAM2_MIN_FRAMES_PER_BATCH = 4
    SAM2_MAX_FRAMES_PER_BATCH = 192
    _request_comfy_check = pyqtSignal(int, str, float)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._generation_temp_files = []
        self._comfy_status_cache = {"checked_at": 0.0, "available": False, "url": ""}
        self._last_logged_comfy_state = None
        self.template_registry = ComfyTemplateRegistry()
        self._comfy_check_request_id = 0
        self._latest_comfy_check_request_id = 0
        self._comfy_check_callbacks = {}
        self._comfy_check_thread = QThread(self)
        self._comfy_check_thread.setObjectName("comfy_availability_worker")
        self._comfy_check_worker = _ComfyAvailabilityWorker()
        self._comfy_check_worker.moveToThread(self._comfy_check_thread)
        self._request_comfy_check.connect(self._comfy_check_worker.check)
        self._comfy_check_worker.finished.connect(self._on_comfy_check_finished)
        self._comfy_check_thread.start()

    def cleanup_temp_files(self):
        for tmp_path in list(self._generation_temp_files):
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
        self._generation_temp_files = []

    def comfy_ui_url(self):
        """Configured ComfyUI base URL, or "" when ComfyUI is not configured.

        Zenvi ships with this blank: the optional local ComfyUI integration stays
        off (no probes, no AI menus) until a URL is entered in Preferences > AI.
        """
        url = get_app().get_settings().get("comfy-ui-url") or ""
        return str(url).strip().rstrip("/")

    def is_comfy_configured(self):
        return bool(self.comfy_ui_url())

    NOT_CONFIGURED_TEXT = "ComfyUI URL is not configured (Preferences > AI)."

    def shutdown(self):
        if getattr(self, "_comfy_check_thread", None):
            self._comfy_check_thread.quit()
            self._comfy_check_thread.wait()

    def refresh_comfy_availability_async(self, timeout=0.5, callback=None):
        self._comfy_check_request_id += 1
        request_id = self._comfy_check_request_id
        url = self.comfy_ui_url()
        self._latest_comfy_check_request_id = request_id
        if callable(callback):
            self._comfy_check_callbacks[request_id] = callback
        if not url:
            # Not configured: settle synchronously without touching the network.
            self._on_comfy_check_finished(request_id, url, False, self.NOT_CONFIGURED_TEXT)
            return request_id
        self._request_comfy_check.emit(request_id, url, float(timeout))
        return request_id

    def is_comfy_available(self, force=False):
        url = self.comfy_ui_url()
        if not force:
            if str(self._comfy_status_cache.get("url", "")) != url:
                return False
            return self._comfy_status_cache["available"]

        now = time()
        available = False
        error_text = ""
        if not url:
            error_text = self.NOT_CONFIGURED_TEXT
        else:
            try:
                available = ComfyClient(url).ping(timeout=0.5)
            except Exception as ex:
                available = False
                error_text = str(ex)

        previous_available = bool(self._comfy_status_cache.get("available"))
        previous_url = str(self._comfy_status_cache.get("url", ""))
        self._comfy_status_cache["checked_at"] = now
        self._comfy_status_cache["available"] = available
        self._comfy_status_cache["url"] = url

        state = (url, bool(available))
        if force or state != self._last_logged_comfy_state or previous_url != url or previous_available != available:
            if available:
                log.info("ComfyUI check passed at %s", url)
            else:
                if error_text:
                    log.info("ComfyUI check failed at %s (%s)", url, error_text)
                else:
                    log.info("ComfyUI check failed at %s", url)
            self._last_logged_comfy_state = state
        return available

    @pyqtSlot(int, str, bool, str)
    def _on_comfy_check_finished(self, request_id, url, available, error_text):
        callback = self._comfy_check_callbacks.pop(int(request_id), None)
        if int(request_id) != int(self._latest_comfy_check_request_id):
            return

        previous_available = bool(self._comfy_status_cache.get("available"))
        previous_url = str(self._comfy_status_cache.get("url", ""))
        self._comfy_status_cache["checked_at"] = time()
        self._comfy_status_cache["available"] = bool(available)
        self._comfy_status_cache["url"] = str(url)

        state = (str(url), bool(available))
        if state != self._last_logged_comfy_state or previous_url != str(url) or previous_available != bool(available):
            if available:
                log.info("ComfyUI check passed at %s", url)
            else:
                if error_text:
                    log.info("ComfyUI check failed at %s (%s)", url, error_text)
                else:
                    log.info("ComfyUI check failed at %s", url)
            self._last_logged_comfy_state = state

        if callback:
            try:
                callback(bool(available), str(error_text or ""), str(url))
            except Exception:
                log.debug("ComfyUI availability callback failed", exc_info=1)

    def can_open_generate_dialog(self):
        return len(self.win.selected_file_ids()) <= 1

    def _prepare_generation_source_path(self, source_file, template_id):
        if not source_file:
            return ""

        source_path = source_file.data.get("path", "")
        media_type = source_file.data.get("media_type")
        if template_id not in ("img2img-basic", "upscale-realesrgan-x4", "img2video-wan") or media_type != "image":
            return source_path

        if is_supported_img2img_path(source_path):
            return source_path

        tmp_fd, tmp_png = tempfile.mkstemp(prefix="openshot-comfy-", suffix=".png")
        os.close(tmp_fd)
        try:
            clip = openshot.Clip(source_path)
            frame = clip.Reader().GetFrame(1)
            frame.Save(tmp_png, 1.0)
            self._generation_temp_files.append(tmp_png)
            return tmp_png
        except Exception:
            try:
                os.remove(tmp_png)
            except OSError:
                pass
            raise

    def _default_generation_name(self, source_file):
        default_name = "generation"
        if source_file:
            path = source_file.data.get("path", "")
            if path:
                default_name = "{}_gen".format(os.path.splitext(os.path.basename(path))[0])
        return default_name

    def _get_source_dimensions(self, source_file):
        if not source_file:
            return (0, 0)
        data = source_file.data if hasattr(source_file, "data") and isinstance(source_file.data, dict) else {}
        try:
            width = int(data.get("width", 0) or 0)
        except Exception:
            width = 0
        try:
            height = int(data.get("height", 0) or 0)
        except Exception:
            height = 0
        return (max(0, width), max(0, height))

    def _sam2_target_batch_bytes(self):
        settings = get_app().get_settings()
        raw_bytes = settings.get("comfy-sam2-target-batch-bytes")
        if raw_bytes is not None:
            try:
                value = int(raw_bytes)
                if value > 0:
                    return value
            except Exception:
                pass
        raw_gb = settings.get("comfy-sam2-target-batch-gb")
        if raw_gb is not None:
            try:
                value = float(raw_gb)
                if value > 0.0:
                    return int(value * 1024 * 1024 * 1024)
            except Exception:
                pass
        return int(self.SAM2_DEFAULT_TARGET_BATCH_BYTES)

    def _estimate_sam2_frames_per_batch(self, width, height, bytes_per_pixel=None):
        width = int(max(0, width))
        height = int(max(0, height))
        if width <= 0 or height <= 0:
            return self.SAM2_MIN_FRAMES_PER_BATCH
        target_bytes = self._sam2_target_batch_bytes()
        if bytes_per_pixel is None:
            bytes_per_pixel = self.SAM2_ESTIMATED_BYTES_PER_PIXEL
        try:
            bytes_per_pixel = float(bytes_per_pixel)
        except Exception:
            bytes_per_pixel = float(self.SAM2_ESTIMATED_BYTES_PER_PIXEL)
        bytes_per_frame = max(
            1.0,
            float(width) * float(height) * bytes_per_pixel,
        )
        frames = int(target_bytes / bytes_per_frame)
        frames = max(self.SAM2_MIN_FRAMES_PER_BATCH, min(self.SAM2_MAX_FRAMES_PER_BATCH, frames))
        # Keep chunk sizes aligned for more stable batching behavior.
        frames = max(self.SAM2_MIN_FRAMES_PER_BATCH, int((frames // 4) * 4))
        return frames

    def _apply_dynamic_sam2_meta_batch(self, workflow, source_file, template_id=None):
        template_id = str(template_id or "").strip().lower()
        # Only adjust SAM2 video tracking template workflows.
        if template_id and template_id not in (
            "video-blur-anything-sam2",
            "video-highlight-anything-sam2",
            "video-mask-anything-sam2",
        ):
            return
        if not isinstance(workflow, dict):
            return

        width, height = self._get_source_dimensions(source_file)
        if width <= 0 or height <= 0:
            return

        has_sam2_chunked = False
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip().lower()
            if class_type == "openshotsam2videosegmentationchunked":
                has_sam2_chunked = True
                break
        if not has_sam2_chunked:
            return

        # Account for downstream per-frame processing memory:
        # - Highlight path is the heaviest (multiple full-frame tensor intermediates)
        # - Blur path is moderately heavy
        # - Mask-only path is closest to baseline SAM2 estimate
        estimated_bpp = float(self.SAM2_ESTIMATED_BYTES_PER_PIXEL)
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip().lower()
            if class_type == "openshotimagehighlightmasked":
                estimated_bpp = max(estimated_bpp, float(self.SAM2_ESTIMATED_BYTES_PER_PIXEL_HIGHLIGHT))
            elif class_type == "openshotimageblurmasked":
                estimated_bpp = max(estimated_bpp, float(self.SAM2_ESTIMATED_BYTES_PER_PIXEL_BLUR))

        dynamic_frames = self._estimate_sam2_frames_per_batch(width, height, bytes_per_pixel=estimated_bpp)
        updated_chunk_nodes = 0
        updated_batch_nodes = 0
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip().lower()
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if class_type == "openshotsam2videosegmentationchunked" and "chunk_size_frames" in inputs:
                inputs["chunk_size_frames"] = int(dynamic_frames)
                updated_chunk_nodes += 1
            if class_type == "vhs_batchmanager" and "frames_per_batch" in inputs:
                inputs["frames_per_batch"] = int(dynamic_frames)
                updated_batch_nodes += 1
        if updated_chunk_nodes or updated_batch_nodes:
            log.info(
                "Dynamic SAM2 batch size: %s frames (source=%sx%s, target_bytes=%s, est_bpp=%s, template=%s, chunk_nodes=%s, batch_nodes=%s)",
                dynamic_frames,
                width,
                height,
                self._sam2_target_batch_bytes(),
                round(estimated_bpp, 2),
                template_id or "unknown",
                updated_chunk_nodes,
                updated_batch_nodes,
            )

    def templates_for_context(self, source_file=None):
        templates = self.template_registry.templates_for_context(source_file=source_file)
        return [
            {"id": t.get("id"), "name": t.get("display_name"), "template": t}
            for t in templates
        ]

    def build_menu_templates(self, source_file=None):
        grouped = {"create": [], "enhance": [], "unknown": []}
        for template in self.template_registry.templates_for_context(source_file=source_file):
            category = str(template.get("category", "unknown"))
            if category not in grouped:
                category = "unknown"
            grouped[category].append(template)
        return grouped

    def icon_for_template(self, template):
        return self.template_registry.output_icon_name(template)

    def _prepare_template_workflow(
        self,
        template,
        payload_name,
        prompt_text,
        source_file,
        source_path,
        reference_image_path="",
        coordinates_positive_text="",
        coordinates_negative_text="",
        rectangles_positive_text="",
        rectangles_negative_text="",
        auto_mode=False,
        tracking_selection=None,
        highlight_color="",
        highlight_opacity=0.0,
        border_color="",
        border_width=0,
        mask_brightness=1.0,
        background_brightness=1.0,
    ):
        workflow = self.template_registry.get_workflow_copy(template.get("id"))
        if not workflow:
            raise ValueError("Template workflow not found.")

        template_dir = ""
        template_path = str((template or {}).get("path") or "").strip()
        if template_path:
            template_dir = os.path.dirname(template_path)

        def _resolve_template_local_file(path_text):
            path_text = str(path_text or "").strip()
            if not path_text:
                return ""
            if os.path.isabs(path_text):
                return path_text if os.path.exists(path_text) else ""
            if not template_dir:
                return ""
            candidate = os.path.abspath(os.path.join(template_dir, path_text))
            if os.path.exists(candidate):
                return candidate
            return ""

        template_id = str((template or {}).get("id") or "").strip().lower()
        prompt_text = str(prompt_text or "").strip()
        music_prompt_text = prompt_text
        music_lyrics_text = ""
        if template_id == "txt2music-ace-step" and prompt_text:
            # Optional inline format:
            #   <style/tags text>
            #   Lyrics:
            #   <lyrics lines...>
            split_match = re.split(r"(?im)^\s*lyrics\s*:\s*", prompt_text, maxsplit=1)
            if len(split_match) == 2:
                music_prompt_text = str(split_match[0] or "").strip()
                music_lyrics_text = str(split_match[1] or "").strip()
            if not music_prompt_text:
                music_prompt_text = prompt_text
        coordinates_positive_text = str(coordinates_positive_text or "").strip()
        coordinates_negative_text = str(coordinates_negative_text or "").strip()
        rectangles_positive_text = str(rectangles_positive_text or "").strip()
        rectangles_negative_text = str(rectangles_negative_text or "").strip()
        auto_mode = bool(auto_mode)
        tracking_selection = tracking_selection if isinstance(tracking_selection, dict) else {}
        highlight_color = str(highlight_color or "").strip()
        border_color = str(border_color or "").strip()
        try:
            highlight_opacity = float(highlight_opacity)
        except (TypeError, ValueError):
            highlight_opacity = 0.0
        try:
            border_width = int(border_width)
        except (TypeError, ValueError):
            border_width = 0
        try:
            mask_brightness = float(mask_brightness)
        except (TypeError, ValueError):
            mask_brightness = 1.0
        try:
            background_brightness = float(background_brightness)
        except (TypeError, ValueError):
            background_brightness = 1.0
        media_type = str(source_file.data.get("media_type", "")).strip().lower() if source_file else ""
        run_seed_base = random.randint(1, 2**31 - 1)
        seed_counter = 0
        applied_prompt = False
        loadimage_node_ids = []
        loadvideo_node_ids = []
        loadaudio_node_ids = []

        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_flat = str(node.get("class_type", "")).strip().lower()
            if class_flat == "loadimage":
                loadimage_node_ids.append(str(node_id))
            elif class_flat in ("loadvideo", "load video", "vhs_loadvideo", "vhs_loadvideopath", "vhs_loadvideoffmpegpath"):
                loadvideo_node_ids.append(str(node_id))
            elif class_flat in ("loadaudio", "load audio", "vhs_loadaudioupload"):
                loadaudio_node_ids.append(str(node_id))

        def _is_placeholder_value(path_text):
            path_text = str(path_text or "").strip().lower()
            return path_text in ("__openshot_input__", "{{openshot_input}}", "$openshot_input")

        def _is_reference_image_placeholder_value(path_text):
            path_text = str(path_text or "").strip().lower()
            return path_text in (
                "__openshot_reference_image__",
                "{{openshot_reference_image}}",
                "$openshot_reference_image",
            )

        def _is_prompt_placeholder_value(text_value):
            text_value = str(text_value or "").strip().lower()
            return text_value in ("__openshot_prompt__", "{{openshot_prompt}}", "$openshot_prompt")

        def _is_lyrics_placeholder_value(text_value):
            text_value = str(text_value or "").strip().lower()
            return text_value in ("__openshot_lyrics__", "{{openshot_lyrics}}", "$openshot_lyrics")

        def _replace_prompt_placeholders(text_value):
            text_value = str(text_value or "")
            if not text_value:
                return text_value
            return (
                text_value
                .replace("__openshot_prompt__", prompt_text)
                .replace("{{openshot_prompt}}", prompt_text)
                .replace("$openshot_prompt", prompt_text)
            )

        def _replace_lyrics_placeholders(text_value):
            text_value = str(text_value or "")
            if not text_value:
                return text_value
            return (
                text_value
                .replace("__openshot_lyrics__", music_lyrics_text)
                .replace("{{openshot_lyrics}}", music_lyrics_text)
                .replace("$openshot_lyrics", music_lyrics_text)
            )

        def _normalize_sam2_coords_input(text_value, fallback_value):
            text_value = str(text_value or "").strip()
            fallback_value = str(fallback_value or "").strip()
            if not text_value:
                return fallback_value
            # Accept raw JSON list format expected by Sam2VideoSegmentationAddPoints.
            if text_value.startswith("[") and "x" in text_value and "y" in text_value:
                return text_value

            # Also accept "x,y; x,y" shorthand and convert to JSON list.
            points = []
            for chunk in text_value.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                parts = [p.strip() for p in chunk.split(",")]
                if len(parts) != 2:
                    return fallback_value
                try:
                    x_val = float(parts[0])
                    y_val = float(parts[1])
                except (TypeError, ValueError):
                    return fallback_value
                points.append({"x": x_val, "y": y_val})
            if not points:
                return fallback_value
            return str(points).replace("'", "\"")

        def _next_random_seed(existing_value):
            nonlocal seed_counter
            seed_counter += 1
            try:
                seed_offset = int(existing_value)
            except Exception:
                seed_offset = 0
            return int((run_seed_base + seed_offset + (seed_counter * 7919)) % (2**31 - 1)) or 1

        def _parse_sam2_points(coords_text):
            coords_text = str(coords_text or "").strip()
            if not coords_text:
                return []
            try:
                parsed = json.loads(coords_text.replace("'", "\""))
            except Exception:
                return []
            if not isinstance(parsed, list):
                return []
            points = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                if "x" not in item or "y" not in item:
                    continue
                try:
                    points.append({"x": float(item["x"]), "y": float(item["y"])})
                except Exception:
                    continue
            return points

        def _select_bind_nodes(node_ids, path_keys, preferred_upload=None):
            if isinstance(path_keys, str):
                path_keys = [path_keys]
            explicit = []
            candidates = []
            for node_id in node_ids:
                node = workflow.get(node_id, {})
                inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
                if not isinstance(inputs, dict):
                    continue
                path_value = ""
                for path_key in path_keys:
                    candidate_value = str(inputs.get(path_key, "")).strip()
                    if candidate_value:
                        path_value = candidate_value
                        break
                upload_value = str(inputs.get("upload", "")).strip().lower()
                if _is_placeholder_value(path_value):
                    explicit.append(node_id)
                    continue
                if preferred_upload and upload_value == preferred_upload:
                    explicit.append(node_id)
                    continue
                if not path_value:
                    candidates.append(node_id)
            if explicit:
                return set(explicit)
            if candidates:
                return {candidates[0]}
            if node_ids:
                return {node_ids[0]}
            return set()

        image_bind_nodes = _select_bind_nodes(loadimage_node_ids, ["image"], preferred_upload="image")
        video_bind_nodes = _select_bind_nodes(loadvideo_node_ids, ["file", "video"])
        audio_bind_nodes = _select_bind_nodes(loadaudio_node_ids, ["audio", "file"])

        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip()
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue

            class_flat = class_type.lower().strip()

            # Resolve generic OpenShot source placeholders in any string input
            # (custom nodes may use keys like `video_path` instead of `video`/`file`).
            if source_path or reference_image_path:
                for input_key, input_value in list(inputs.items()):
                    if isinstance(input_value, str) and source_path and _is_placeholder_value(input_value):
                        inputs[input_key] = source_path
                    elif isinstance(input_value, str) and reference_image_path and _is_reference_image_placeholder_value(input_value):
                        inputs[input_key] = reference_image_path

            if "filename_prefix" in inputs:
                prefix_value = str(inputs.get("filename_prefix", "")).strip()
                if "/" in prefix_value:
                    head, tail = prefix_value.rsplit("/", 1)
                    tail = str(tail or "output").strip()
                    inputs["filename_prefix"] = "{}/{}_{}".format(head, tail, payload_name)
                else:
                    inputs["filename_prefix"] = payload_name

            if "seed" in inputs and not isinstance(inputs.get("seed", None), (list, dict)):
                inputs["seed"] = _next_random_seed(inputs.get("seed"))

            if prompt_text:
                text_value = inputs.get("text", None)
                prompt_value = inputs.get("prompt", None)
                tags_value = inputs.get("tags", None)
                lyrics_value = inputs.get("lyrics", None)

                if isinstance(text_value, str):
                    replaced_text = _replace_prompt_placeholders(text_value)
                    if replaced_text != text_value:
                        inputs["text"] = replaced_text
                        applied_prompt = True
                        text_value = replaced_text
                if isinstance(prompt_value, str):
                    replaced_prompt = _replace_prompt_placeholders(prompt_value)
                    if replaced_prompt != prompt_value:
                        inputs["prompt"] = replaced_prompt
                        applied_prompt = True
                        prompt_value = replaced_prompt
                if isinstance(tags_value, str):
                    replaced_tags = _replace_prompt_placeholders(tags_value)
                    if replaced_tags != tags_value:
                        inputs["tags"] = replaced_tags
                        applied_prompt = True
                        tags_value = replaced_tags

                if isinstance(text_value, str) and _is_prompt_placeholder_value(text_value):
                    inputs["text"] = prompt_text
                    applied_prompt = True
                elif isinstance(prompt_value, str) and _is_prompt_placeholder_value(prompt_value):
                    inputs["prompt"] = prompt_text
                    applied_prompt = True
                elif isinstance(tags_value, str) and _is_prompt_placeholder_value(tags_value):
                    # Support music nodes that use "tags" instead of "text"/"prompt".
                    if template_id == "txt2music-ace-step":
                        inputs["tags"] = music_prompt_text
                    else:
                        inputs["tags"] = prompt_text
                    applied_prompt = True
                elif isinstance(lyrics_value, str) and _is_lyrics_placeholder_value(lyrics_value):
                    if template_id == "txt2music-ace-step":
                        inputs["lyrics"] = music_lyrics_text
                    else:
                        inputs["lyrics"] = ""
                elif class_flat == "cliptextencode" and "text" in inputs and not applied_prompt:
                    inputs["text"] = prompt_text
                    applied_prompt = True
                elif "prompt" in inputs and isinstance(prompt_value, str) and not prompt_value.strip() and not applied_prompt:
                    # Support prompt-driven custom nodes (e.g. GroundingDINO/SAM2) that expose a plain string prompt input.
                    inputs["prompt"] = prompt_text
                    applied_prompt = True

            if isinstance(inputs.get("lyrics", None), str):
                lyrics_value = inputs.get("lyrics", "")
                replaced_lyrics = _replace_lyrics_placeholders(lyrics_value)
                if replaced_lyrics != lyrics_value:
                    inputs["lyrics"] = replaced_lyrics

            if class_flat in (
                "sam2videosegmentationaddpoints",
                "sam2segmentation",
                "openshotsam2videosegmentationaddpoints",
                "openshotsam2segmentation",
            ):
                coords_text = coordinates_positive_text
                points = _parse_sam2_points(coords_text)
                has_positive_rects = bool(rectangles_positive_text)

                seed_frame_idx = 0
                if isinstance(tracking_selection, dict):
                    try:
                        seed_frame_idx = max(0, int(tracking_selection.get("seed_frame", 1)) - 1)
                    except Exception:
                        seed_frame_idx = 0
                if "frame_index" in inputs:
                    try:
                        inputs["frame_index"] = int(seed_frame_idx)
                    except Exception:
                        pass
                if "tracking_selection_json" in inputs:
                    try:
                        inputs["tracking_selection_json"] = json.dumps(tracking_selection or {})
                    except Exception:
                        inputs["tracking_selection_json"] = "{}"
                if "dino_prompt" in inputs:
                    inputs["dino_prompt"] = str(prompt_text or "")

                has_dino_prompt = bool(str(prompt_text or "").strip()) and ("dino_prompt" in inputs)

                auto_enabled = bool(inputs.get("auto_mode", False)) or auto_mode
                if "auto_mode" in inputs:
                    inputs["auto_mode"] = bool(auto_enabled)
                if ("anything-sam2" in template_id) and (not points) and (not has_positive_rects) and (not auto_enabled) and (not has_dino_prompt):
                    raise ValueError("No SAM2 seed was provided. Use Points, Rectangle, or Auto mode.")

                # New OpenShot node contract.
                if "positive_points_json" in inputs and isinstance(inputs.get("positive_points_json", None), str):
                    inputs["positive_points_json"] = _normalize_sam2_coords_input(
                        coords_text,
                        str(inputs.get("positive_points_json", "")),
                    )
                # Backward compatibility for third-party node variants.
                elif "coordinates_positive" in inputs and isinstance(inputs.get("coordinates_positive", None), str):
                    inputs["coordinates_positive"] = _normalize_sam2_coords_input(
                        coords_text,
                        str(inputs.get("coordinates_positive", "")),
                    )

                if class_flat in ("sam2segmentation", "openshotsam2segmentation") and "individual_objects" in inputs:
                    # For Blur Anything, treat points as a single combined prompt.
                    # This is more stable with mixed positive/negative points and avoids
                    # per-object mask selection quirks in the current SAM2 single-image node.
                    if "blur-anything-sam2" in template_id:
                        inputs["individual_objects"] = False
                    else:
                        # Non-Blur-Anything templates keep multi-object behavior.
                        inputs["individual_objects"] = bool(len(points) > 1)

                if coordinates_negative_text:
                    if "negative_points_json" in inputs and isinstance(inputs.get("negative_points_json", None), str):
                        inputs["negative_points_json"] = _normalize_sam2_coords_input(
                            coordinates_negative_text,
                            str(inputs.get("negative_points_json", "")),
                        )
                    elif "coordinates_negative" in inputs:
                        neg_value = inputs.get("coordinates_negative", "")
                        if isinstance(neg_value, str) or "coordinates_negative" not in inputs:
                            inputs["coordinates_negative"] = _normalize_sam2_coords_input(
                                coordinates_negative_text,
                                str(neg_value or ""),
                            )
                if rectangles_positive_text and ("positive_rects_json" in inputs) and isinstance(
                    inputs.get("positive_rects_json", None), str
                ):
                    inputs["positive_rects_json"] = rectangles_positive_text
                if rectangles_negative_text and ("negative_rects_json" in inputs) and isinstance(
                    inputs.get("negative_rects_json", None), str
                ):
                    inputs["negative_rects_json"] = rectangles_negative_text

            if class_flat in ("openshotimagehighlightmasked",) or class_type.strip() == "OpenShotImageHighlightMasked":
                if "highlight_color" in inputs and highlight_color:
                    inputs["highlight_color"] = highlight_color
                if "highlight_opacity" in inputs:
                    inputs["highlight_opacity"] = float(max(0.0, min(1.0, highlight_opacity)))
                if "border_color" in inputs and border_color:
                    inputs["border_color"] = border_color
                if "border_width" in inputs:
                    inputs["border_width"] = int(max(0, border_width))
                if "mask_brightness" in inputs:
                    inputs["mask_brightness"] = float(max(0.0, min(3.0, mask_brightness)))
                if "background_brightness" in inputs:
                    inputs["background_brightness"] = float(max(0.0, min(3.0, background_brightness)))

            if not source_path:
                continue

            node_id = str(node_id)

            if class_flat == "loadimage" and media_type == "image" and node_id in image_bind_nodes:
                if "image" in inputs:
                    inputs["image"] = source_path
                if "upload" in inputs:
                    inputs["upload"] = "image"
            elif class_flat == "loadimage":
                # Resolve template-local reference images (relative filenames) to absolute paths,
                # so ComfyClient can upload and rewrite them to [input] automatically.
                configured_image = str(inputs.get("image", "")).strip()
                local_image = _resolve_template_local_file(configured_image)
                if local_image:
                    inputs["image"] = local_image
                    if "upload" in inputs:
                        inputs["upload"] = "image"
                elif media_type == "image" and source_path:
                    # If additional reference images are missing, gracefully fallback to the selected source image.
                    # This keeps common exported workflows usable without hand-editing filenames.
                    missing_relative = configured_image and (not os.path.isabs(configured_image))
                    missing_absolute = os.path.isabs(configured_image) and (not os.path.exists(configured_image))
                    if missing_relative or missing_absolute:
                        inputs["image"] = source_path
                        if "upload" in inputs:
                            inputs["upload"] = "image"
                        log.warning(
                            "Comfy template missing LoadImage asset (%s). Falling back to selected source image.",
                            configured_image,
                        )
            elif class_flat in ("loadvideo", "load video", "vhs_loadvideopath", "vhs_loadvideoffmpegpath") and media_type == "video" and node_id in video_bind_nodes:
                if "file" in inputs:
                    inputs["file"] = source_path
                elif "video" in inputs:
                    inputs["video"] = source_path
            elif class_flat in ("loadvideo", "load video", "vhs_loadvideopath", "vhs_loadvideoffmpegpath"):
                local_video = _resolve_template_local_file(inputs.get("file", ""))
                if local_video and "file" in inputs:
                    inputs["file"] = local_video
            elif class_flat == "vhs_loadvideo" and media_type == "video" and node_id in video_bind_nodes:
                if "video" in inputs:
                    inputs["video"] = source_path
            elif class_flat in ("loadaudio", "load audio", "vhs_loadaudioupload") and media_type == "audio" and node_id in audio_bind_nodes:
                if "audio" in inputs:
                    inputs["audio"] = source_path
                elif "file" in inputs:
                    inputs["file"] = source_path
            elif class_flat in ("loadaudio", "load audio", "vhs_loadaudioupload"):
                local_audio = _resolve_template_local_file(inputs.get("audio", "") or inputs.get("file", ""))
                if local_audio:
                    if "audio" in inputs:
                        inputs["audio"] = local_audio
                    elif "file" in inputs:
                        inputs["file"] = local_audio

        if media_type == "image" and image_bind_nodes:
            log.debug("Comfy template image input binding nodes=%s source=%s", sorted(image_bind_nodes), source_path)
        if media_type == "video" and video_bind_nodes:
            log.debug("Comfy template video input binding nodes=%s source=%s", sorted(video_bind_nodes), source_path)
        if media_type == "audio" and audio_bind_nodes:
            log.debug("Comfy template audio input binding nodes=%s source=%s", sorted(audio_bind_nodes), source_path)

        self._apply_dynamic_sam2_meta_batch(workflow, source_file=source_file, template_id=template_id)

        return workflow

    def _save_nodes_for_workflow(self, workflow, template_id=None):
        template_id = str(template_id or "").strip().lower()
        save_nodes = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip().lower()
            if not class_type:
                continue
            if class_type.startswith("save") or class_type in (
                "previewany",
                "transnetv2_run",
                "openshottransnetscenedetect",
                "vhs_videocombine",
            ):
                if class_type == "vhs_videocombine":
                    inputs = node.get("inputs", {}) if isinstance(node.get("inputs", {}), dict) else {}
                    prefix = str(inputs.get("filename_prefix", "")).strip().lower()
                    is_mask_output = ("openshot_mask" in prefix)
                    is_track_template = template_id in (
                        "video-blur-anything-sam2",
                        "video-highlight-anything-sam2",
                        "video-mask-anything-sam2",
                    )
                    if is_track_template:
                        if template_id == "video-mask-anything-sam2":
                            if not is_mask_output:
                                continue
                        else:
                            if is_mask_output:
                                continue
                save_nodes.append(str(node_id))
        return save_nodes

    def action_generate_trigger(self, checked=True, source_file=None, template_id=None, open_dialog=True):
        selected_files = [source_file] if source_file else self.win.selected_files()
        if len(selected_files) > 1:
            return

        if not self.is_comfy_configured():
            msg = QMessageBox(self.win)
            msg.setWindowTitle("ComfyUI Not Configured")
            msg.setText(
                "ComfyUI is optional and currently not configured.\n\n"
                "To use the AI Tools, run a local ComfyUI server and enter its URL "
                "(for example http://127.0.0.1:8188) under Preferences > AI > ComfyUI URL."
            )
            msg.exec_()
            return

        if not self.is_comfy_available(force=True):
            msg = QMessageBox(self.win)
            msg.setWindowTitle("ComfyUI Unavailable")
            msg.setText(
                "{} could not connect to ComfyUI at:\n{}\n\n"
                "Start ComfyUI or update the URL in Preferences > AI.".format(info.PRODUCT_NAME, self.comfy_ui_url())
            )
            msg.exec_()
            return

        source_file = selected_files[0] if selected_files else None
        templates = self.templates_for_context(source_file=source_file)
        available_template_ids = {str(t.get("id", "")).strip() for t in templates}
        if open_dialog:
            dialog_title = "Enhance with AI" if source_file else "Create with AI"
            win = GenerateMediaDialog(
                source_file=source_file,
                templates=templates,
                preselected_template_id=template_id,
                dialog_title=dialog_title,
                parent=self.win,
            )
            if win.exec_() != QDialog.Accepted:
                return
            payload = win.get_payload()
        else:
            selected_template_id = str(template_id or "").strip()
            if not selected_template_id:
                return
            if selected_template_id not in available_template_ids:
                QMessageBox.information(
                    self.win,
                    "Invalid Input",
                    "The selected AI action is not available for this source type.",
                )
                return
            payload = {
                "name": self._default_generation_name(source_file),
                "template_id": selected_template_id,
                "prompt": "",
            }

        payload_name = self._next_generation_name(payload.get("name"))
        source_file_id = source_file.id if source_file else None
        template_meta = self.template_registry.get_template(payload.get("template_id"))
        if not template_meta:
            QMessageBox.information(self.win, "Invalid Input", "The selected AI template was not found.")
            return
        try:
            source_path = self._prepare_generation_source_path(source_file, payload.get("template_id"))
        except Exception as ex:
            QMessageBox.warning(
                self.win,
                "Source Conversion Failed",
                "{} could not convert this image into PNG for ComfyUI.\n\n{}".format(info.PRODUCT_NAME, ex),
            )
            return
        reference_image_path = ""
        reference_image_file_id = str(payload.get("reference_image_file_id") or "").strip()
        if reference_image_file_id:
            reference_image_file = File.get(id=reference_image_file_id)
            if reference_image_file and isinstance(reference_image_file.data, dict):
                reference_image_path = str(reference_image_file.data.get("path", "") or "").strip()
        try:
            workflow = self._prepare_template_workflow(
                template_meta,
                payload_name=payload_name,
                prompt_text=payload.get("prompt"),
                source_file=source_file,
                source_path=source_path,
                reference_image_path=reference_image_path,
                coordinates_positive_text=payload.get("coordinates_positive"),
                coordinates_negative_text=payload.get("coordinates_negative"),
                rectangles_positive_text=payload.get("rectangles_positive"),
                rectangles_negative_text=payload.get("rectangles_negative"),
                auto_mode=payload.get("auto_mode"),
                tracking_selection=payload.get("tracking_selection"),
                highlight_color=payload.get("highlight_color"),
                highlight_opacity=payload.get("highlight_opacity"),
                border_color=payload.get("border_color"),
                border_width=payload.get("border_width"),
                mask_brightness=payload.get("mask_brightness"),
                background_brightness=payload.get("background_brightness"),
            )
        except Exception as ex:
            QMessageBox.information(self.win, "Invalid Input", str(ex))
            return
        request = {
            "comfy_url": self.comfy_ui_url(),
            "workflow": workflow,
            "client_id": "openshot-qt",
            "timeout_s": 21600,
            "save_node_ids": self._save_nodes_for_workflow(workflow, template_id=payload.get("template_id")),
            "template_id": str(payload.get("template_id") or ""),
        }
        job_id = self.win.generation_queue.enqueue(
            payload_name,
            payload.get("template_id"),
            payload.get("prompt"),
            source_file_id=source_file_id,
            request=request,
        )
        if not job_id:
            QMessageBox.information(
                self.win,
                "Generation Already Active",
                "Only one active generation is allowed per source file.",
            )
            return

        self.win.statusBar.showMessage("Queued generation job", 3000)

    def on_generation_job_finished(self, job_id, status):
        job = self.win.generation_queue.get_job(job_id) if getattr(self.win, "generation_queue", None) else None
        if not job:
            return

        if status == "completed":
            result = self._import_generation_outputs(job)
            imported = int(result.get("imported", 0))
            caption_saved = bool(result.get("caption_saved", False))
            scenes_labeled = int(result.get("scenes_labeled", 0))
            scene_splits_created = int(result.get("scene_splits_created", 0))
            if imported > 0 and caption_saved:
                self.win.statusBar.showMessage(
                    "Generation completed, imported {} file(s), and saved file caption data".format(imported),
                    5000,
                )
            elif scene_splits_created > 0:
                self.win.statusBar.showMessage(
                    "Generation completed and created {} scene split file(s)".format(scene_splits_created),
                    5000,
                )
            elif imported > 0 and scenes_labeled > 0:
                self.win.statusBar.showMessage(
                    "Generation completed, imported {} file(s), and labeled {} scene segment(s)".format(
                        imported, scenes_labeled
                    ),
                    5000,
                )
            elif imported > 0:
                self.win.statusBar.showMessage("Generation completed and imported {} file(s)".format(imported), 5000)
            elif caption_saved:
                self.win.statusBar.showMessage("Generation completed and saved file caption data", 5000)
            else:
                self.win.statusBar.showMessage("Generation completed (no output files found)", 5000)
            return

        if status == "canceled":
            self.win.statusBar.showMessage("Generation canceled", 3000)
            return

        if status == "failed":
            error_text = ComfyClient.summarize_error_text(job.get("error") or "ComfyUI generation failed.")
            self.win.statusBar.showMessage("Generation failed", 5000)
            QMessageBox.warning(self.win, "Generation Failed", error_text)

    def _import_generation_outputs(self, job):
        outputs = list(job.get("outputs", []) or [])
        if not outputs:
            return {"imported": 0, "caption_saved": False, "scene_splits_created": 0}

        request = job.get("request", {}) or {}
        comfy_url = str(request.get("comfy_url") or self.comfy_ui_url())
        client = ComfyClient(comfy_url)
        output_dir = info.COMFYUI_OUTPUT_PATH
        os.makedirs(output_dir, exist_ok=True)
        template_id = str(job.get("template_id") or "").strip().lower()

        name_raw = str(job.get("name") or "generation")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name_raw).strip("._")
        if not safe_name:
            safe_name = "generation"

        saved_paths = []
        text_outputs = []
        scene_splits_created = 0
        video_path_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
        seen_video_payload_paths = set()
        for index, output_ref in enumerate(outputs, start=1):
            text_payload = str(output_ref.get("text", "")).strip()
            if text_payload:
                if template_id == "video-segment-scenes-transnet":
                    scene_ranges = self._extract_scene_ranges_from_text(text_payload)
                    if scene_ranges:
                        created = self._create_scene_split_files(
                            source_file_id=job.get("source_file_id"),
                            scene_ranges=scene_ranges,
                        )
                        scene_splits_created += created
                        if created > 0:
                            continue
                payload_video_paths = self._extract_video_paths_from_text(text_payload)
                if not payload_video_paths:
                    payload_ext = os.path.splitext(text_payload)[1].lower()
                    if payload_ext in video_path_exts:
                        payload_video_paths = [text_payload]
                downloaded_any_video = False
                for raw_video_path in payload_video_paths:
                    norm_video_path = str(raw_video_path).strip().replace("\\", "/")
                    if not norm_video_path or norm_video_path in seen_video_payload_paths:
                        continue
                    seen_video_payload_paths.add(norm_video_path)

                    payload_ext = os.path.splitext(norm_video_path)[1].lower() or ".mp4"
                    video_ref = self._comfy_output_ref_from_path(norm_video_path)
                    if not video_ref:
                        continue
                    local_name = "{}_{}{}".format(safe_name, str(index).zfill(3), payload_ext)
                    local_path = self._next_available_path(os.path.join(output_dir, local_name))
                    try:
                        client.download_output_file(video_ref, local_path)
                        saved_paths.append(local_path)
                        downloaded_any_video = True
                    except Exception as ex:
                        log.warning(
                            "Failed to download segmented video from Comfy path output %s: %s",
                            raw_video_path,
                            ex,
                        )
                if downloaded_any_video:
                    continue

                # Some Save SRT node variants return the output file path as text.
                # Convert that path to a downloadable Comfy output ref when possible.
                if text_payload.lower().endswith(".srt"):
                    srt_ref = self._comfy_output_ref_from_path(text_payload)
                    if srt_ref:
                        local_name = "{}_{}{}".format(safe_name, str(index).zfill(3), ".srt")
                        local_path = self._next_available_path(os.path.join(output_dir, local_name))
                        try:
                            client.download_output_file(srt_ref, local_path)
                            with open(local_path, "r", encoding="utf-8") as handle:
                                srt_text = handle.read().strip()
                            if srt_text:
                                saved_paths.append(local_path)
                                text_outputs.append(srt_text)
                                continue
                        except Exception as ex:
                            log.warning("Failed to download/read SRT from Comfy path output %s: %s", text_payload, ex)
                    if self._looks_like_filesystem_path(text_payload):
                        # Some Whisper/SRT nodes return only a server-side output path.
                        # Don't treat that path string as caption text.
                        continue

                ext = ".srt" if str(output_ref.get("format", "")).lower() == "srt" else ".txt"
                local_name = "{}_{}{}".format(safe_name, str(index).zfill(3), ext)
                local_path = self._next_available_path(os.path.join(output_dir, local_name))
                try:
                    with open(local_path, "w", encoding="utf-8") as handle:
                        handle.write(text_payload)
                    saved_paths.append(local_path)
                    text_outputs.append(text_payload)
                except Exception as ex:
                    log.warning("Failed to write Comfy text output to %s: %s", local_path, ex)
                continue

            original_name = str(output_ref.get("filename", "output.png"))
            ext = os.path.splitext(original_name)[1] or ".png"
            local_name = "{}_{}{}".format(safe_name, str(index).zfill(3), ext)
            local_path = self._next_available_path(os.path.join(output_dir, local_name))
            try:
                client.download_output_file(output_ref, local_path)
                saved_paths.append(local_path)
            except Exception as ex:
                log.warning("Failed to download Comfy output %s: %s", output_ref, ex)

        if saved_paths:
            self.win.files_model.add_files(
                saved_paths,
                quiet=True,
                prevent_image_seq=True,
                prevent_recent_folder=True,
            )

        if not saved_paths and scene_splits_created <= 0:
            return {"imported": 0, "caption_saved": False, "scene_splits_created": 0}

        caption_saved = False
        scenes_labeled = 0
        if template_id == "video-whisper-srt":
            caption_text = self._resolve_caption_text(saved_paths, text_outputs)
            caption_saved = self._store_caption_on_file(
                source_file_id=job.get("source_file_id"),
                caption_text=caption_text,
            )
        if template_id == "video-segment-scenes-transnet" and saved_paths:
            scenes_labeled = self._apply_scene_segment_metadata(
                source_file_id=job.get("source_file_id"),
                saved_paths=saved_paths,
            )
        return {
            "imported": len(saved_paths),
            "caption_saved": caption_saved,
            "scenes_labeled": scenes_labeled,
            "scene_splits_created": scene_splits_created,
        }

    def _extract_video_paths_from_text(self, text_payload):
        """Extract absolute video file paths from log/text payloads."""
        text_payload = str(text_payload or "")
        if not text_payload:
            return []
        pattern = re.compile(
            r"([A-Za-z]:[\\/][^\r\n]+?\.(?:mp4|mov|mkv|webm|avi|m4v)|/[^\r\n]+?\.(?:mp4|mov|mkv|webm|avi|m4v))",
            re.IGNORECASE,
        )
        return [match.strip() for match in pattern.findall(text_payload) if match.strip()]

    def _looks_like_filesystem_path(self, text_payload):
        text_payload = str(text_payload or "").strip()
        if not text_payload:
            return False
        normalized = text_payload.replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", normalized):
            return True
        return normalized.startswith("/")

    def _extract_scene_ranges_from_text(self, text_payload):
        """Parse scene range metadata JSON from text output payloads."""
        text_payload = str(text_payload or "").strip()
        if not text_payload:
            return []
        if not text_payload.startswith("{"):
            first = text_payload.find("{")
            last = text_payload.rfind("}")
            if first >= 0 and last > first:
                text_payload = text_payload[first:last + 1]
            else:
                return []
        try:
            payload = json.loads(text_payload)
        except Exception:
            return []

        segment_entries = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segment_entries, list):
            return []

        scene_ranges = []
        for segment in segment_entries:
            if not isinstance(segment, dict):
                continue
            start_seconds = segment.get("start_seconds", segment.get("start"))
            end_seconds = segment.get("end_seconds", segment.get("end"))
            try:
                start_value = float(start_seconds)
                end_value = float(end_seconds)
            except (TypeError, ValueError):
                continue
            if end_value <= start_value:
                continue
            scene_ranges.append((max(0.0, start_value), max(0.0, end_value)))
        return scene_ranges

    def _create_scene_split_files(self, source_file_id, scene_ranges):
        """Create split-style file entries pointing to the same source media path."""
        source_file = File.get(id=source_file_id) if source_file_id else None
        if source_file is None:
            source_file = File.get(id=str(source_file_id or ""))
        if source_file is None:
            return 0

        source_data = source_file.data if isinstance(source_file.data, dict) else {}
        source_path = str(source_data.get("path", "") or "")
        if not source_path:
            return 0
        if str(source_data.get("media_type", "")).lower() != "video":
            return 0

        fps_data = source_data.get("fps", {})
        fps_fraction = Fraction(30, 1)
        try:
            fps_num = int(fps_data.get("num", 30))
            fps_den = int(fps_data.get("den", 1) or 1)
            if fps_num > 0 and fps_den > 0:
                fps_fraction = Fraction(fps_num, fps_den)
        except (TypeError, ValueError, ZeroDivisionError):
            fps_fraction = Fraction(30, 1)

        source_duration = float(source_data.get("duration") or 0.0)
        base_name = os.path.splitext(os.path.basename(source_path))[0] or "scene"

        created = 0
        for start_seconds, end_seconds in scene_ranges:
            start_seconds = max(0.0, float(start_seconds or 0.0))
            end_seconds = max(start_seconds, float(end_seconds or start_seconds))
            if source_duration > 0.0:
                start_seconds = min(start_seconds, source_duration)
                end_seconds = min(end_seconds, source_duration)
            if end_seconds <= start_seconds:
                continue

            include_hours = int(end_seconds // 3600) > 0
            include_minutes = include_hours or int((end_seconds % 3600) // 60) > 0
            start_tc = self._seconds_to_compact_timecode(
                start_seconds,
                fps_fraction,
                include_hours=include_hours,
                include_minutes=include_minutes,
            )
            end_tc = self._seconds_to_compact_timecode(
                end_seconds,
                fps_fraction,
                include_hours=include_hours,
                include_minutes=include_minutes,
            )

            split_data = json.loads(json.dumps(source_data))
            split_data.pop("id", None)
            split_data["start"] = start_seconds
            split_data["end"] = end_seconds
            split_data["name"] = "{} ({} to {})".format(
                base_name,
                start_tc,
                end_tc,
            )

            split_file = File()
            split_file.id = None
            split_file.key = None
            split_file.type = "insert"
            split_file.data = split_data
            self._append_scene_tag(split_file)
            split_file.save()
            created += 1
        return created

    def _resolve_caption_text(self, saved_paths, text_outputs):
        srt_path = ""
        for path in saved_paths:
            if str(path).lower().endswith(".srt"):
                srt_path = path
                break
        if srt_path:
            try:
                with open(srt_path, "r", encoding="utf-8") as handle:
                    text = handle.read().strip()
                if text:
                    return text
            except Exception as ex:
                log.warning("Failed reading SRT file for file caption metadata: %s", ex)

        for value in text_outputs:
            text = str(value or "").strip()
            if "-->" in text:
                return text

        for value in text_outputs:
            text = str(value or "").strip()
            if text:
                return text

        return ""

    def _store_caption_on_file(self, source_file_id, caption_text):
        caption_text = str(caption_text or "").strip()
        if not caption_text:
            return False

        source_file_value = source_file_id
        file_obj = File.get(id=source_file_value)
        if file_obj is None:
            file_obj = File.get(id=str(source_file_value or ""))
        if file_obj is None:
            log.info("No source file found for caption metadata update (file_id=%s)", source_file_value)
            return False

        if not isinstance(file_obj.data, dict):
            file_obj.data = {}
        file_obj.data["caption"] = caption_text
        file_obj.save()
        self.win.FileUpdated.emit(str(file_obj.id))
        return True

    def _seconds_to_compact_timecode(self, seconds_value, fps_fraction, include_hours=False, include_minutes=False):
        fps_fraction = fps_fraction if isinstance(fps_fraction, Fraction) and fps_fraction > 0 else Fraction(30, 1)
        fps_float = float(fps_fraction)
        frame_number = int(round(max(0.0, float(seconds_value or 0.0)) * fps_float)) + 1
        t = time_parts.secondsToTime((frame_number - 1) / fps_float, fps_fraction.numerator, fps_fraction.denominator)
        hours = int(t.get("hour", 0))
        minutes = int(t.get("min", 0))
        secs = int(t.get("sec", 0))
        frames = int(t.get("frame", 0))
        if include_hours:
            return "{:02d}:{:02d}:{:02d};{:02d}".format(hours, minutes, secs, frames)
        if include_minutes:
            return "{:02d}:{:02d};{:02d}".format(minutes, secs, frames)
        return "{:02d};{:02d}".format(secs, frames)

    def _append_scene_tag(self, file_obj):
        tags_raw = str(file_obj.data.get("tags", "") or "").strip()
        if not tags_raw:
            file_obj.data["tags"] = "scene"
            return
        tags = [part.strip() for part in tags_raw.split(",") if part.strip()]
        if any(part.lower() == "scene" for part in tags):
            return
        tags.append("scene")
        file_obj.data["tags"] = ", ".join(tags)

    def _apply_scene_segment_metadata(self, source_file_id, saved_paths):
        source_file = File.get(id=source_file_id) if source_file_id else None
        base_name = "scene"
        fps_fraction = Fraction(30, 1)
        if source_file:
            source_path = str(source_file.data.get("path", "") or "")
            if source_path:
                base_name = os.path.splitext(os.path.basename(source_path))[0] or base_name
            fps_data = source_file.data.get("fps", {})
            try:
                num = int(fps_data.get("num", 30))
                den = int(fps_data.get("den", 1) or 1)
                if num > 0 and den > 0:
                    fps_fraction = Fraction(num, den)
            except (TypeError, ValueError, ZeroDivisionError):
                fps_fraction = Fraction(30, 1)

        imported_files = []
        for path in saved_paths:
            file_obj = File.get(path=path)
            if file_obj and str(file_obj.data.get("media_type", "")) == "video":
                imported_files.append(file_obj)
        if not imported_files:
            return 0

        running_start = 0.0
        updated = 0
        for file_obj in imported_files:
            duration = float(file_obj.data.get("duration") or 0.0)
            if duration <= 0:
                start_trim = float(file_obj.data.get("start") or 0.0)
                end_trim = float(file_obj.data.get("end") or 0.0)
                duration = max(0.0, end_trim - start_trim)
            running_end = running_start + max(0.0, duration)

            include_hours = int(running_end // 3600) > 0
            include_minutes = include_hours or int((running_end % 3600) // 60) > 0
            start_tc = self._seconds_to_compact_timecode(
                running_start, fps_fraction, include_hours=include_hours, include_minutes=include_minutes
            )
            end_tc = self._seconds_to_compact_timecode(
                running_end, fps_fraction, include_hours=include_hours, include_minutes=include_minutes
            )
            file_obj.data["name"] = "{} ({} to {})".format(base_name, start_tc, end_tc)
            self._append_scene_tag(file_obj)
            file_obj.save()
            self.win.FileUpdated.emit(str(file_obj.id))

            running_start = running_end
            updated += 1

        return updated

    def _comfy_output_ref_from_path(self, path_text):
        """Convert a Comfy output absolute/relative path into a /view-compatible output ref."""
        path_text = unquote(str(path_text or "").strip())
        if not path_text:
            return None
        normalized = path_text.replace("\\", "/")
        filename = os.path.basename(normalized)
        if not filename:
            return None

        subfolder = ""
        marker = "/output/"
        if marker in normalized:
            rel = normalized.split(marker, 1)[1].lstrip("/")
            rel_dir = os.path.dirname(rel).strip("/")
            subfolder = rel_dir
        elif normalized.startswith("output/"):
            rel = normalized[len("output/"):]
            rel_dir = os.path.dirname(rel).strip("/")
            subfolder = rel_dir
        else:
            if os.path.isabs(normalized):
                # Unknown absolute location outside Comfy output tree; fallback to basename only.
                return {
                    "filename": filename,
                    "subfolder": "",
                    "type": "output",
                }
            rel_dir = os.path.dirname(normalized).strip("/")
            if rel_dir and rel_dir != ".":
                subfolder = rel_dir

        return {
            "filename": filename,
            "subfolder": subfolder,
            "type": "output",
        }

    def _next_generation_name(self, requested_name):
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(requested_name or "").strip()).strip("._")
        if not base:
            base = "generation"

        existing_names = set()
        for file_obj in File.filter():
            if not file_obj:
                continue
            display_name = str(file_obj.data.get("name") or os.path.basename(file_obj.data.get("path", "")) or "")
            if display_name:
                stem = os.path.splitext(display_name)[0]
                existing_names.add(stem.lower())

        if base.lower() not in existing_names:
            return base

        name_root = base
        m = re.match(r"^(.*?)(?:_gen(\d+))?$", base, re.IGNORECASE)
        if m:
            name_root = (m.group(1) or base).rstrip("_") or "generation"
        n = 1
        while True:
            candidate = "{}_gen{}".format(name_root, n)
            if candidate.lower() not in existing_names:
                return candidate
            n += 1

    def _next_available_path(self, path):
        if not os.path.exists(path):
            return path
        folder = os.path.dirname(path)
        stem, ext = os.path.splitext(os.path.basename(path))
        n = 2
        while True:
            candidate = os.path.join(folder, "{}_{}{}".format(stem, n, ext))
            if not os.path.exists(candidate):
                return candidate
            n += 1
