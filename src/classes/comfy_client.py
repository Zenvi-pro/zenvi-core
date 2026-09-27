"""
 @file
 @brief This file contains a small ComfyUI HTTP/WebSocket client.
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

import json
import os
import ssl
import base64
import uuid
from datetime import datetime
import re
import socket
import struct
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode
from urllib.parse import urlparse

from classes import info
from classes.logger import log


class ComfyProgressSocket:
    """Minimal WebSocket client for ComfyUI /ws progress events."""

    def __init__(self, base_url, client_id):
        self.base_url = str(base_url or "").rstrip("/")
        self.client_id = str(client_id or "")
        self.sock = None
        self._connect()

    def _connect(self):
        parsed = urlparse(self.base_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if not host:
            raise RuntimeError("Invalid ComfyUI URL for websocket")
        port = parsed.port or (443 if scheme == "https" else 80)
        base_path = (parsed.path or "").rstrip("/")
        ws_path = "{}/ws".format(base_path) if base_path else "/ws"
        path = "{}?clientId={}".format(ws_path, quote(self.client_id))

        raw = socket.create_connection((host, port), timeout=6.0)
        if scheme == "https":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        # Allow slower remote/proxied websocket handshakes.
        raw.settimeout(6.0)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET {} HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Origin: {}://{}:{}\r\n"
            "Pragma: no-cache\r\n"
            "Cache-Control: no-cache\r\n"
            "Sec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).format(path, host, port, scheme, host, port, key)
        self.sock.sendall(req.encode("utf-8"))

        response = self._recv_http_headers()
        if " 101 " not in response.split("\r\n", 1)[0]:
            raise RuntimeError("WebSocket upgrade failed: {}".format(response.split("\r\n", 1)[0]))
        # Use short timeout for regular frame polling after successful handshake.
        self.sock.settimeout(0.25)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def poll_progress(self, prompt_id=None, max_messages=8):
        """Read available frames and return latest progress payload.

        If prompt_id is provided, events are filtered to that prompt id.
        If prompt_id is None/empty, events from any prompt on this websocket
        are accepted (useful for meta-batch follow-up prompts).
        """
        if not self.sock:
            return None
        latest = None
        latest_rank = None
        prompt_key = str(prompt_id or "").strip()
        for _ in range(max_messages):
            frame = self._recv_frame_nonblocking()
            if frame is None:
                break
            opcode, payload = frame

            # Ping -> pong
            if opcode == 0x9:
                self._send_control_frame(0xA, payload)
                continue
            if opcode == 0x8:
                self.close()
                break
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue

            event_type = msg.get("type")
            event_data = msg.get("data", {})
            if event_type == "progress":
                if not isinstance(event_data, dict):
                    continue
                event_prompt = str(event_data.get("prompt_id", ""))
                if prompt_key and (not event_prompt or event_prompt != prompt_key):
                    continue
                value = float(event_data.get("value", 0.0))
                maximum = float(event_data.get("max", 0.0))
                if maximum > 0:
                    candidate = {
                        "percent": int(max(0, min(99, round((value / maximum) * 100.0)))),
                        "value": value,
                        "max": maximum,
                        "node": str(event_data.get("node", "")),
                        "type": "progress",
                        "prompt_id": event_prompt,
                    }
                    # Prefer unfinished progress, and prefer explicit "progress" events.
                    unfinished = (value + 1e-6) < maximum
                    rank = (1 if unfinished else 0, 2, maximum)
                    if latest is None or rank > latest_rank:
                        latest = candidate
                        latest_rank = rank
            elif event_type == "progress_state":
                # Newer Comfy events: data={prompt_id, nodes={node_id:{value,max}}}
                if not isinstance(event_data, dict):
                    continue
                event_prompt = str(event_data.get("prompt_id", ""))
                if prompt_key and (not event_prompt or event_prompt != prompt_key):
                    continue
                nodes = event_data.get("nodes", {})
                if not isinstance(nodes, dict):
                    continue
                # Prefer unfinished node progress; only fall back to completed states.
                best = None
                best_rank = None
                for node_id, node_state in nodes.items():
                    if not isinstance(node_state, dict):
                        continue
                    value = float(node_state.get("value", 0.0))
                    maximum = float(node_state.get("max", 0.0))
                    if maximum > 0:
                        candidate = {
                            "percent": int(max(0, min(99, round((value / maximum) * 100.0)))),
                            "value": value,
                            "max": maximum,
                            "node": str(node_id),
                            "type": "progress_state",
                            "prompt_id": event_prompt,
                        }
                        unfinished = (value + 1e-6) < maximum
                        rank = (1 if unfinished else 0, maximum)
                        if best is None or rank > best_rank:
                            best = candidate
                            best_rank = rank
                if best is not None:
                    rank = (best_rank[0], 1, float(best.get("max", 0.0)))
                    if latest is None or rank > latest_rank:
                        latest = best
                        latest_rank = rank
        return latest

    def _recv_http_headers(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break
        return data.decode("utf-8", errors="replace")

    def _recv_exact(self, size):
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RuntimeError("WebSocket connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_frame_nonblocking(self):
        try:
            header = self.sock.recv(2)
            if not header:
                return None
        except socket.timeout:
            return None
        except OSError:
            return None

        if len(header) < 2:
            return None
        b1, b2 = header[0], header[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask_key = b""
        if masked:
            mask_key = self._recv_exact(4)

        payload = self._recv_exact(length) if length > 0 else b""
        if masked and payload:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))

        return opcode, payload

    def _send_control_frame(self, opcode, payload=b""):
        if self.sock is None:
            return
        payload = payload or b""
        first = 0x80 | (opcode & 0x0F)
        # Client frames must be masked.
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length < (1 << 16):
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        masked_payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(header + mask + masked_payload)


class ComfyClient:
    """Minimal ComfyUI client using stdlib HTTP."""
    ERROR_MAX_CHARS = 1800

    def __init__(self, base_url):
        self.base_url = str(base_url or "").rstrip("/")

    @staticmethod
    def _write_debug_error(payload):
        debug_dir = info.COMFYUI_PATH
        try:
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, "debug_error.json")
            with open(debug_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except Exception:
            log.warning("Failed writing Comfy debug error payload", exc_info=True)

    def _write_debug_prompt_payload(self, prompt_graph, client_id):
        debug_dir = info.COMFYUI_PATH
        try:
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, "debug.json")
            payload = {
                "generated_at_utc": datetime.utcnow().isoformat() + "Z",
                "comfy_url": self.base_url,
                "client_id": str(client_id or ""),
                "prompt": prompt_graph,
            }
            with open(debug_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except Exception:
            log.warning("Failed writing Comfy sent prompt payload", exc_info=True)

    @staticmethod
    def open_progress_socket(base_url, client_id):
        return ComfyProgressSocket(base_url, client_id)

    def ping(self, timeout=0.5):
        with urlopen("{}/system_stats".format(self.base_url), timeout=timeout) as response:
            return int(response.status) >= 200 and int(response.status) < 300

    def queue_prompt(self, prompt_graph, client_id):
        prompt_graph = self._rewrite_prompt_local_file_inputs(prompt_graph)
        self._write_debug_prompt_payload(prompt_graph, client_id)
        payload = json.dumps({"prompt": prompt_graph, "client_id": client_id}).encode("utf-8")
        req = Request(
            "{}/prompt".format(self.base_url),
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=10.0) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as ex:
            details = ""
            try:
                error_data = json.loads(ex.read().decode("utf-8"))
                ComfyClient._write_debug_error(error_data)
                error_obj = error_data.get("error", {})
                if isinstance(error_obj, dict):
                    details = error_obj.get("type") or error_obj.get("message") or ""
                else:
                    details = str(error_obj or "")

                node_errors = error_data.get("node_errors", {})
                node_error_text = self._format_node_errors(node_errors)
                if node_error_text:
                    details = "{}\n{}".format(details or "prompt validation failed", node_error_text)
                elif not details:
                    details = ComfyClient.summarize_error_text(error_data)
                else:
                    details = "{}\n{}".format(details, ComfyClient.summarize_error_text(error_data))
            except Exception:
                details = str(ex)
            raise RuntimeError("ComfyUI prompt rejected: {}".format(ComfyClient.summarize_error_text(details)))
        return data.get("prompt_id")

    def _rewrite_prompt_local_file_inputs(self, prompt_graph):
        """Rewrite local absolute paths for image/video loader nodes to uploaded Comfy input refs."""
        if not isinstance(prompt_graph, dict):
            return prompt_graph
        rewritten = dict(prompt_graph)

        def _annotated(path_text):
            path_text = str(path_text or "").strip()
            return path_text.endswith("[input]") or path_text.endswith("[output]") or path_text.endswith("[temp]")

        for node_id, node in rewritten.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", ""))
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue

            if class_type == "LoadImage":
                image_path = str(inputs.get("image", "")).strip()
                if image_path and os.path.isabs(image_path) and os.path.exists(image_path) and not _annotated(image_path):
                    uploaded = self.upload_input_file(image_path)
                    inputs["image"] = uploaded
                    node["inputs"] = inputs
                    rewritten[node_id] = node
                    log.debug("ComfyClient rewrote LoadImage input node=%s path=%s -> %s", str(node_id), image_path, uploaded)
            elif class_type == "LoadVideo":
                video_path = str(inputs.get("file", "")).strip()
                if video_path and os.path.isabs(video_path) and os.path.exists(video_path) and not _annotated(video_path):
                    uploaded = self.upload_input_file(video_path)
                    inputs["file"] = uploaded
                    node["inputs"] = inputs
                    rewritten[node_id] = node
                    log.debug("ComfyClient rewrote LoadVideo input node=%s path=%s -> %s", str(node_id), video_path, uploaded)
            elif class_type in ("VHS_LoadVideo", "VHS_LoadVideoPath", "VHS_LoadVideoFFmpegPath"):
                video_path = str(inputs.get("video", "")).strip()
                if video_path and os.path.isabs(video_path) and os.path.exists(video_path) and not _annotated(video_path):
                    uploaded = self.upload_input_file(video_path)
                    # VHS_LoadVideo expects a plain filename from Comfy input options.
                    # Path-based VHS loaders accept a plain relative path as well.
                    if uploaded.endswith(" [input]"):
                        uploaded = uploaded[:-8].strip()
                    inputs["video"] = uploaded
                    node["inputs"] = inputs
                    rewritten[node_id] = node
                    log.debug(
                        "ComfyClient rewrote %s input node=%s path=%s -> %s",
                        class_type,
                        str(node_id),
                        video_path,
                        uploaded,
                    )

        return rewritten

    @staticmethod
    def _format_node_errors(node_errors):
        if not isinstance(node_errors, dict) or not node_errors:
            return ""
        lines = []
        max_lines = 8
        for node_id, err in node_errors.items():
            if len(lines) >= max_lines:
                break
            if not isinstance(err, dict):
                lines.append("node {}: {}".format(node_id, str(err)))
                continue
            err_type = str(err.get("type", "")).strip()
            message = str(err.get("message", "")).strip()
            if not message:
                details = err.get("details")
                if details:
                    message = str(details)
            if err_type and message:
                lines.append("node {} [{}]: {}".format(node_id, err_type, message))
            elif message:
                lines.append("node {}: {}".format(node_id, message))
            elif err_type:
                lines.append("node {} [{}]".format(node_id, err_type))
        if not lines:
            return ""
        return "Node validation errors: {}".format(" | ".join(lines))

    @staticmethod
    def summarize_error_text(value, max_chars=None):
        """Return a compact Comfy error text safe for UI display."""
        if max_chars is None:
            max_chars = ComfyClient.ERROR_MAX_CHARS

        if isinstance(value, (dict, list, tuple)):
            value = ComfyClient._limit_error_structure(value)
            try:
                text = json.dumps(value, ensure_ascii=True)
            except Exception:
                text = str(value)
        else:
            text = str(value or "")

        # Remove huge numeric/tensor dumps that make dialogs unreadable.
        text = re.sub(r"tensor\(\[[\s\S]{250,}?\]\)", "tensor([<omitted>])", text)
        text = re.sub(r"array\(\[[\s\S]{250,}?\]\)", "array([<omitted>])", text)
        text = re.sub(r"\[[\d\.\-eE,\s]{350,}\]", "[<numeric array omitted>]", text)
        text = re.sub(r"\s+", " ", text).strip()

        max_chars = max(300, int(max_chars))
        if len(text) > max_chars:
            truncated = len(text) - max_chars
            text = "{} ... [truncated {} chars]".format(text[:max_chars], truncated)
        return text

    @staticmethod
    def _limit_error_structure(value, depth=0, max_depth=4, max_items=10, max_str=260):
        if depth >= max_depth:
            return "<...>"
        if isinstance(value, dict):
            out = {}
            for index, key in enumerate(value.keys()):
                if index >= max_items:
                    out["<truncated_keys>"] = len(value) - max_items
                    break
                out[str(key)] = ComfyClient._limit_error_structure(
                    value.get(key),
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_str=max_str,
                )
            return out
        if isinstance(value, (list, tuple)):
            out = []
            for index, item in enumerate(value):
                if index >= max_items:
                    out.append("<truncated_items:{}>".format(len(value) - max_items))
                    break
                out.append(
                    ComfyClient._limit_error_structure(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_str=max_str,
                    )
                )
            return out
        text = str(value)
        if len(text) > max_str:
            return text[:max_str] + "...<truncated>"
        return text

    def list_checkpoints(self):
        """Return available checkpoint names from ComfyUI object info."""
        with urlopen("{}/object_info/CheckpointLoaderSimple".format(self.base_url), timeout=8.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Expected path:
        # CheckpointLoaderSimple -> input -> required -> ckpt_name
        # Supports multiple schema variants:
        # 1) [ [..names..], {...meta...} ]
        # 2) ["COMBO", {"options":[..names..], ...}]
        node_info = data.get("CheckpointLoaderSimple", {})
        required = node_info.get("input", {}).get("required", {})
        ckpt_input = required.get("ckpt_name", None)
        values = self._extract_combo_options(ckpt_input)
        return [str(v) for v in values if str(v).strip()]

    def list_upscale_models(self):
        """Return available upscaler model names from ComfyUI object info."""
        models = []
        # Primary source: object_info schema for UpscaleModelLoader.
        try:
            with urlopen("{}/object_info/UpscaleModelLoader".format(self.base_url), timeout=8.0) as response:
                data = json.loads(response.read().decode("utf-8"))

            node_info = data.get("UpscaleModelLoader", {})
            required = node_info.get("input", {}).get("required", {})
            model_input = required.get("model_name", None)
            values = self._extract_combo_options(model_input)
            if values:
                models = [str(v) for v in values if str(v).strip()]
        except Exception as ex:
            log.debug("ComfyClient list_upscale_models object_info parse failed: %s", ex)

        # Fallback: direct model listing endpoint.
        if not models:
            try:
                with urlopen("{}/models/upscale_models".format(self.base_url), timeout=8.0) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if isinstance(data, list):
                    models = [str(v) for v in data if str(v).strip()]
            except Exception as ex:
                log.debug("ComfyClient list_upscale_models /models fallback failed: %s", ex)

        # Dedupe while preserving order.
        seen = set()
        ordered = []
        for name in models:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def list_clip_models(self):
        """Return available CLIP/text-encoder model names from ComfyUI object info."""
        with urlopen("{}/object_info/CLIPLoader".format(self.base_url), timeout=8.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        node_info = data.get("CLIPLoader", {})
        required = node_info.get("input", {}).get("required", {})
        clip_input = required.get("clip_name", None)
        values = self._extract_combo_options(clip_input)
        return [str(v) for v in values if str(v).strip()]

    def list_clip_vision_models(self):
        """Return available CLIP vision model names from ComfyUI object info."""
        with urlopen("{}/object_info/CLIPVisionLoader".format(self.base_url), timeout=8.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        node_info = data.get("CLIPVisionLoader", {})
        required = node_info.get("input", {}).get("required", {})
        clip_input = required.get("clip_name", None)
        values = self._extract_combo_options(clip_input)
        return [str(v) for v in values if str(v).strip()]

    def list_rife_vfi_models(self):
        """Return available RIFE checkpoint names from ComfyUI object info."""
        node_type = "RIFE VFI"
        with urlopen(
            "{}/object_info/{}".format(self.base_url, quote(node_type, safe="")),
            timeout=8.0,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))

        node_info = data.get(node_type, {})
        required = node_info.get("input", {}).get("required", {})
        ckpt_input = required.get("ckpt_name", None)
        values = self._extract_combo_options(ckpt_input)
        return [str(v) for v in values if str(v).strip()]

    @staticmethod
    def _extract_combo_options(input_config):
        """Extract valid options from Comfy object_info input config variants."""
        if input_config is None:
            return []

        # Variant: [ [options...], {meta...} ]
        if isinstance(input_config, list) and input_config and isinstance(input_config[0], list):
            return [str(v) for v in input_config[0]]

        # Variant: ["COMBO", {"options":[...], ...}]
        if (
            isinstance(input_config, list)
            and len(input_config) >= 2
            and str(input_config[0]).upper() == "COMBO"
            and isinstance(input_config[1], dict)
        ):
            options = input_config[1].get("options", [])
            if isinstance(options, list):
                return [str(v) for v in options]

        # Variant: direct list of values
        if isinstance(input_config, list):
            scalar_values = []
            for item in input_config:
                if isinstance(item, (str, int, float)):
                    scalar_values.append(str(item))
            return scalar_values

        return []

    def history(self, prompt_id):
        with urlopen("{}/history/{}".format(self.base_url, quote(str(prompt_id))), timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def history_all(self):
        with urlopen("{}/history".format(self.base_url), timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def progress(self):
        """Return ComfyUI /progress payload."""
        try:
            with urlopen("{}/progress".format(self.base_url), timeout=8.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as ex:
            if int(getattr(ex, "code", 0)) == 404:
                # Some ComfyUI versions don't expose /progress.
                return None
            raise

    def interrupt(self, prompt_id=None):
        payload = {}
        if prompt_id:
            payload["prompt_id"] = str(prompt_id)
        log.debug("ComfyClient interrupt request base_url=%s prompt_id=%s", self.base_url, payload.get("prompt_id", ""))
        req = Request(
            "{}/interrupt".format(self.base_url),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=8.0) as response:
            log.debug("ComfyClient interrupt response status=%s", int(response.status))
            return int(response.status) >= 200 and int(response.status) < 300

    def cancel_prompt(self, prompt_id):
        """Request ComfyUI to delete/cancel a prompt from the queue."""
        log.debug("ComfyClient cancel_prompt request base_url=%s prompt_id=%s", self.base_url, str(prompt_id))
        payload = json.dumps({"delete": [str(prompt_id)]}).encode("utf-8")
        req = Request(
            "{}/queue".format(self.base_url),
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=8.0) as response:
            log.debug("ComfyClient cancel_prompt response status=%s", int(response.status))
            return int(response.status) >= 200 and int(response.status) < 300

    def queue(self):
        """Return ComfyUI queue state."""
        with urlopen("{}/queue".format(self.base_url), timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def upload_input_file(self, local_path):
        """Upload a local file into ComfyUI input dir via /upload/image."""
        local_path = str(local_path or "").strip()
        if not local_path or not os.path.exists(local_path):
            raise RuntimeError("Local file does not exist: {}".format(local_path))

        boundary = "----OpenShotComfy{}".format(uuid.uuid4().hex)
        filename = os.path.basename(local_path)
        parts = []

        def _add_field(name, value):
            parts.append("--{}\r\n".format(boundary).encode("utf-8"))
            parts.append('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name).encode("utf-8"))
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        _add_field("type", "input")
        parts.append("--{}\r\n".format(boundary).encode("utf-8"))
        parts.append(
            (
                'Content-Disposition: form-data; name="image"; filename="{}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).format(filename).encode("utf-8")
        )
        with open(local_path, "rb") as handle:
            parts.append(handle.read())
        parts.append(b"\r\n")
        parts.append("--{}--\r\n".format(boundary).encode("utf-8"))
        body = b"".join(parts)

        req = Request(
            "{}/upload/image".format(self.base_url),
            data=body,
            method="POST",
            headers={"Content-Type": "multipart/form-data; boundary={}".format(boundary)},
        )
        with urlopen(req, timeout=30.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        name = str(data.get("name", "")).strip()
        subfolder = str(data.get("subfolder", "")).strip()
        if not name:
            raise RuntimeError("ComfyUI upload failed: invalid response")
        rel = "{}/{}".format(subfolder, name) if subfolder else name
        return "{} [input]".format(rel)

    @staticmethod
    def prompt_in_queue(prompt_id, queue_data):
        """Check if prompt_id appears in queue_running/queue_pending payload."""
        pid = str(prompt_id)
        if not isinstance(queue_data, dict):
            return False

        for key in ("queue_running", "queue_pending"):
            entries = queue_data.get(key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                # Common format: [number, prompt_id, ...]
                if isinstance(entry, list) and len(entry) >= 2 and str(entry[1]) == pid:
                    return True
                # Defensive fallback for dict-like entries
                if isinstance(entry, dict):
                    if str(entry.get("prompt_id", "")) == pid:
                        return True
        return False

    @staticmethod
    def extract_file_outputs(history_entry, save_node_ids=None):
        """Return a flat list of file refs from image/video/audio history outputs."""
        outputs = []
        if not isinstance(history_entry, dict):
            return outputs
        node_outputs = history_entry.get("outputs", {})
        if not isinstance(node_outputs, dict):
            return outputs
        save_node_ids = set(str(node_id) for node_id in (save_node_ids or []))

        for node_id, node_out in node_outputs.items():
            if save_node_ids and str(node_id) not in save_node_ids:
                continue
            if isinstance(node_out, dict):
                for key in ("images", "videos", "video", "gifs", "audios", "audio", "files", "filenames"):
                    refs = node_out.get(key, [])
                    if not isinstance(refs, list):
                        continue
                    for ref in refs:
                        if not isinstance(ref, dict):
                            continue
                        if ref.get("filename"):
                            outputs.append({
                                "filename": str(ref.get("filename")),
                                "subfolder": str(ref.get("subfolder", "")),
                                "type": str(ref.get("type", "output")),
                            })
                # Also extract text-like outputs (for custom nodes such as Whisper/SRT pipelines).
                for value in node_out.values():
                    text_values = ComfyClient._extract_text_outputs(value)
                    for text_value in text_values:
                        output_format = "srt" if ComfyClient._looks_like_srt(text_value) else "txt"
                        outputs.append({
                            "text": text_value,
                            "format": output_format,
                            "type": "text",
                        })
            else:
                # Some custom nodes emit list/string outputs directly instead of dicts.
                text_values = ComfyClient._extract_text_outputs(node_out)
                for text_value in text_values:
                    output_format = "srt" if ComfyClient._looks_like_srt(text_value) else "txt"
                    outputs.append({
                        "text": text_value,
                        "format": output_format,
                        "type": "text",
                    })
        return outputs

    @staticmethod
    def extract_image_outputs(history_entry, save_node_ids=None):
        return ComfyClient.extract_file_outputs(history_entry, save_node_ids=save_node_ids)

    @staticmethod
    def _extract_text_output(value):
        """Extract text payloads from common Comfy output structures."""
        values = ComfyClient._extract_text_outputs(value)
        return values[0] if values else ""

    @staticmethod
    def _extract_text_outputs(value):
        """Extract one or more text payloads from common Comfy output structures."""
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        out.append(text)
            return out
        if isinstance(value, dict):
            out = []
            for key in ("srt", "text", "value"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
            return out
        return []

    @staticmethod
    def _looks_like_srt(text):
        text = str(text or "")
        if "-->" not in text:
            return False
        return bool(re.search(r"\d{2}:\d{2}:\d{2}[,.:]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.:]\d{3}", text))

    def download_output_file(self, file_ref, destination_path):
        """Download a Comfy output reference to a local file path."""
        params = {
            "filename": file_ref.get("filename", ""),
            "subfolder": file_ref.get("subfolder", ""),
            "type": file_ref.get("type", "output"),
        }
        url = "{}/view?{}".format(self.base_url, urlencode(params))
        with urlopen(url, timeout=10.0) as response:
            data = response.read()

        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        with open(destination_path, "wb") as handle:
            handle.write(data)

    def download_image(self, image_ref, destination_path):
        self.download_output_file(image_ref, destination_path)
