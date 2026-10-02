"""Test doubles for the ai-generation editor tools under the headless stub.

``FakeFilesModel.add_files`` imports like ``files_model.add_files`` (a probed
File saved through ``classes.query``, so it joins the tool's undo step).
``FakeTimeline.addClip`` builds a clip the way ``Timeline.addClip`` does (from
the production-shaped fixture) and saves it. ``FakePoint`` stands in for
``QPointF``. ``FakeBackend`` answers ``generate_tts`` without the network.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import types

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "editor_tools")


def _fixture(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakePoint:
    def __init__(self, x=0.0, y=0.0):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeFilesModel:
    """files_model stand-in: add_files probes (duration from ``durations`` or ``default_duration``)."""

    def __init__(self, default_duration=3.2, kind="audio"):
        self.calls = []
        self.durations = {}
        self.default_duration = float(default_duration)
        self.kind = kind
        self.fail_paths = set()
        self._files = _fixture("files.json")

    def add_files(self, files, image_seq_details=None, quiet=False, prevent_image_seq=False,
                  prevent_recent_folder=False, skip_indexing=False):
        from classes.query import File
        if not isinstance(files, (list, tuple)):
            files = [files]
        self.calls.append({"files": list(files), "quiet": quiet, "prevent_image_seq": prevent_image_seq,
                           "prevent_recent_folder": prevent_recent_folder, "skip_indexing": skip_indexing})
        out = []
        for path in files:
            existing = File.get(path=path)
            if existing:
                out.append(existing)
                continue
            if path in self.fail_paths:
                continue
            data = copy.deepcopy(self._files[self.kind])
            data.pop("id", None)
            data["path"] = path
            duration = float(self.durations.get(path, self.default_duration))
            data["duration"] = duration
            fps = data.get("fps") or {"num": 30, "den": 1}
            data["video_length"] = str(int(round(duration * fps["num"] / fps["den"])))
            f = File()
            f.data = data
            f.save()
            out.append(f)
        return out


class FakeTimeline:
    """win.timeline stand-in with the real addClip's placement rules (position, layer, duration)."""

    def __init__(self, editor):
        self.editor = editor
        self.calls = []
        self._clip = _fixture("clip.json")

    def addClip(self, file_id, position, track, ignore_refresh=False, call_manual_move=True, auto_transition=False):
        from classes.query import Clip, File
        self.calls.append({"file_id": file_id, "position": position.x(), "track": track,
                           "call_manual_move": call_manual_move})
        f = File.get(id=file_id)
        if not f:
            return None
        data = copy.deepcopy(self._clip)
        data.pop("id", None)
        fps = self.editor.get("fps")
        fps_f = float(fps["num"]) / float(fps["den"])
        duration = float(f.data.get("duration") or 0.0)
        if f.data.get("media_type") == "image":
            duration = float(self.editor.settings["default-image-length"])
        duration = max(1, int(round(duration * fps_f))) / fps_f
        data.update({
            "file_id": file_id,
            "title": f.data.get("name") or os.path.basename(f.data.get("path", "clip")),
            "reader": copy.deepcopy(f.data),
            "layer": int(track),
            "position": float(position.x()),
            "start": 0.0,
            "end": duration,
            "duration": duration,
            "effects": [],
        })
        if f.data.get("media_type") == "audio" or f.data.get("has_video") is False:
            data["has_video"] = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
        c = Clip()
        c.data = data
        c.save()
        return copy.deepcopy(c.data)

    def update_clip_data(self, clip_data, only_basic_props=True, ignore_reader=False, ignore_refresh=False,
                         transaction_id=None):
        from classes.query import Clip
        c = Clip.get(id=clip_data.get("id"))
        if not c:
            c = Clip()
        c.data = copy.deepcopy(clip_data)
        c.save()


class FakeBackend:
    """get_backend_client() stand-in: generate_tts returns a fixed MP3 payload or an error."""

    def __init__(self, audio=b"ID3fake-mp3-bytes", error=""):
        self.audio = audio
        self.error = error
        self.tts_calls = []

    def generate_tts(self, text, voice="alloy", model="tts-1", speed=1.0):
        self.tts_calls.append({"text": text, "voice": voice, "model": model, "speed": speed})
        if self.error:
            return {"success": False, "error": self.error}
        return {"success": True, "audio_base64": base64.b64encode(self.audio).decode("ascii")}


def install(editor, monkeypatch, tmp_path, backend=None, duration=3.2):
    """Wire the fakes into *editor* and return a namespace with them."""
    import qt_api
    from classes import api_client, assets

    files_model = FakeFilesModel(default_duration=duration)
    timeline = FakeTimeline(editor)
    editor.window.files_model = files_model
    editor.window.timeline = timeline
    backend = backend or FakeBackend()
    monkeypatch.setattr(qt_api, "QPointF", FakePoint, raising=False)
    monkeypatch.setattr(api_client, "get_backend_client", lambda: backend)
    counter = {"n": 0}

    def _durable(ext=".mp4", project_file_path=None):
        counter["n"] += 1
        return str(tmp_path / ("generated_%03d%s" % (counter["n"], ext)))

    monkeypatch.setattr(assets, "durable_media_path", _durable)
    return types.SimpleNamespace(files_model=files_model, timeline=timeline, backend=backend)


# ---------------------------------------------------------------------------
# A fake ComfyUI server (the HTTP API ComfyClient speaks)
# ---------------------------------------------------------------------------

class FakeComfyServer:
    """ComfyUI stand-in on 127.0.0.1:<ephemeral>: /system_stats, /prompt, /upload/image, /history, /view, /queue.

    Records every request. A queued prompt finishes at once: /history answers with one
    output file per Save* node of the posted graph (or ``fail_with`` as a failed status).
    """

    def __init__(self, output_bytes=b"\x89PNG fake output"):
        import http.server
        import threading

        self.requests = []
        self.prompts = []
        self.uploads = []
        self.output_bytes = output_bytes
        self.fail_with = ""
        self._next = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload, raw=False):
                body = payload if raw else json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.requests.append(("GET", self.path))
                if self.path.startswith("/system_stats"):
                    return self._send(200, {"system": {"os": "fake"}, "devices": []})
                if self.path.startswith("/history/"):
                    pid = self.path.split("/history/", 1)[1]
                    return self._send(200, server._history(pid))
                if self.path.startswith("/view"):
                    return self._send(200, server.output_bytes, raw=True)
                if self.path.startswith("/queue"):
                    return self._send(200, {"queue_running": [], "queue_pending": []})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.requests.append(("POST", self.path))
                if self.path == "/prompt":
                    data = json.loads(body.decode("utf-8"))
                    server._next += 1
                    pid = "prompt-%d" % server._next
                    server.prompts.append({"prompt_id": pid, **data})
                    return self._send(200, {"prompt_id": pid, "number": server._next})
                if self.path.startswith("/upload/image"):
                    import re as _re
                    m = _re.search(rb'filename="([^"]+)"', body)
                    name = m.group(1).decode("utf-8") if m else "upload.bin"
                    server.uploads.append(name)
                    return self._send(200, {"name": name, "subfolder": "", "type": "input"})
                if self.path in ("/interrupt", "/queue"):
                    return self._send(200, {})
                return self._send(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def _history(self, pid):
        prompt = next((p for p in self.prompts if p["prompt_id"] == pid), None)
        if prompt is None:
            return {}
        if self.fail_with:
            return {pid: {"status": {"status_str": "error", "messages": [["execution_error",
                                                                          {"exception_message": self.fail_with}]]},
                          "outputs": {}}}
        outputs = {}
        for node_id, node in (prompt.get("prompt") or {}).items():
            if str(node.get("class_type", "")).lower().startswith("save") or \
                    str(node.get("class_type", "")).lower() == "vhs_videocombine":
                outputs[node_id] = {"images": [{"filename": "zenvi_out_%s.png" % node_id, "subfolder": "",
                                                "type": "output"}]}
        return {pid: {"status": {"status_str": "success", "completed": True}, "outputs": outputs,
                      "prompt": [0, pid, prompt.get("prompt"), {"client_id": prompt.get("client_id")}, []]}}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port_url():
    """A URL nothing listens on (connection refused)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


def install_comfy(editor, monkeypatch, tmp_path, url=""):
    """A real GenerationService + GenerationQueueManager (without their QThreads) on *editor*."""
    import sys
    from collections import deque
    from unittest.mock import MagicMock

    from classes import info
    from classes.comfy_templates import ComfyTemplateRegistry

    stub = types.ModuleType("windows.generate")
    stub.GenerateMediaDialog = type("GenerateMediaDialog", (), {})
    monkeypatch.setitem(sys.modules, "windows.generate", stub)
    import classes.generation_queue as gq
    import classes.generation_service as gs

    monkeypatch.setattr(gs, "get_app", lambda: editor.app)
    monkeypatch.setattr(info, "COMFYUI_PATH", str(tmp_path / "comfyui"))
    monkeypatch.setattr(info, "COMFYUI_OUTPUT_PATH", str(tmp_path / "comfyui-output"))
    message_boxes = MagicMock()
    monkeypatch.setattr(gs, "QMessageBox", message_boxes)

    service = gs.GenerationService.__new__(gs.GenerationService)
    service.win = editor.window
    service.template_registry = ComfyTemplateRegistry()
    service._generation_temp_files = []
    service._comfy_status_cache = {"checked_at": 0.0, "available": False, "url": ""}
    service._last_logged_comfy_state = None
    service._comfy_check_request_id = 0
    service._latest_comfy_check_request_id = 0
    service._comfy_check_callbacks = {}
    service._request_comfy_check = MagicMock()

    queue = gq.GenerationQueueManager.__new__(gq.GenerationQueueManager)
    queue.jobs = {}
    queue._queued = deque()
    queue._running_job_id = None
    queue._active_file_jobs = {}
    for signal in ("job_added", "job_updated", "job_finished", "job_removed", "file_job_changed",
                   "queue_changed", "_run_job", "_cancel_job"):
        setattr(queue, signal, MagicMock())

    status_bar = MagicMock()
    editor.window.generation_service = service
    editor.window.generation_queue = queue
    editor.window.statusBar = status_bar

    def cancel_generation_job(job_id):          # MainWindow.cancel_generation_job
        if queue.cancel_job(job_id):
            status_bar.showMessage("Generation canceled", 3000)

    editor.window.cancel_generation_job = cancel_generation_job
    files_model = FakeFilesModel(kind="image")
    editor.window.files_model = files_model
    editor.settings["comfy-ui-url"] = url

    worker = gq._GenerationWorker()
    worker.job_finished = MagicMock()
    worker.progress_changed = MagicMock()
    worker.progress_detail_changed = MagicMock()
    worker.progress_sub_changed = MagicMock()

    def run_next():
        """Run the job the queue just started (what the worker QThread does), then deliver its result."""
        job_id, request = queue._run_job.emit.call_args[0]
        worker._run_comfy_job(job_id, request)
        args = worker.job_finished.emit.call_args[0]
        queue._on_job_finished(*args)
        service.on_generation_job_finished(job_id, queue.jobs[job_id]["status"])
        return queue.jobs[job_id]

    return types.SimpleNamespace(service=service, queue=queue, worker=worker, files_model=files_model,
                                 status_bar=status_bar, message_boxes=message_boxes, run_next=run_next, gs=gs)


# ---------------------------------------------------------------------------
# The Recording dock: the real AudioRecordingDockContent methods on fake widgets
# ---------------------------------------------------------------------------

class FakeWidget:
    def __init__(self, text=""):
        self._text = text
        self._enabled = True
        self._tip = ""
        self._visible = True

    def setEnabled(self, value):
        self._enabled = bool(value)

    def isEnabled(self):
        return self._enabled

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setToolTip(self, tip):
        self._tip = tip

    def toolTip(self):
        return self._tip

    def setStyleSheet(self, _css):
        pass

    def setVisible(self, value):
        self._visible = bool(value)

    def setActive(self, value):
        self.active = bool(value)


class FakeButton(FakeWidget):
    def __init__(self, text="", checked=False):
        super().__init__(text)
        self._checked = checked

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked


class FakeSpin(FakeWidget):
    def __init__(self, value=0):
        super().__init__()
        self._value = value

    def setValue(self, value):
        self._value = int(value)

    def value(self):
        return self._value


class FakeCombo(FakeWidget):
    """QComboBox subset; currentIndexChanged is delivered to *on_change* unless signals are blocked."""

    def __init__(self, items=(), on_change=None):
        super().__init__()
        self._items = [(str(t), d) for t, d in items]
        self._index = 0 if self._items else -1
        self._blocked = False
        self.on_change = on_change

    def addItem(self, *args):
        text, data = (args[-2], args[-1]) if len(args) >= 2 else (args[0], None)
        self._items.append((str(text), data))
        if self._index < 0:
            self._index = 0

    def clear(self):
        self._items, self._index = [], -1

    def count(self):
        return len(self._items)

    def itemText(self, i):
        return self._items[i][0]

    def itemData(self, i, role=None):
        return self._items[i][1]

    def findData(self, value):
        for i, (_t, d) in enumerate(self._items):
            if d == value:
                return i
        return -1

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, i):
        changed = i != self._index
        self._index = i
        if changed and not self._blocked and self.on_change:
            self.on_change()

    def currentData(self):
        return self._items[self._index][1] if 0 <= self._index < len(self._items) else None

    def currentText(self):
        return self._items[self._index][0] if 0 <= self._index < len(self._items) else ""

    def blockSignals(self, value):
        previous, self._blocked = self._blocked, bool(value)
        return previous


class FakeCard(FakeButton):
    """RecordingSourceCard: setChecked refuses when unavailable and emits toggled -> dock._source_toggled()."""

    def __init__(self, dock, available=True, tip=""):
        super().__init__()
        self._dock = dock
        self._available = available
        self._enabled = available
        self._tip = tip

    def setChecked(self, checked):
        checked = bool(checked) and self._available
        if self._checked == checked:
            return
        self._checked = checked
        self._dock._sender = self
        self._dock._source_toggled()


def make_recording_dock(mics=("Built-in Microphone", "USB Mic"), cameras=("FaceTime HD Camera",),
                        screens=None, available=("mic", "screen", "webcam"), system_audio=True,
                        mono_only=False):
    """A dock object of the real class (its __init__ skipped) with fake widgets and devices."""
    import windows.audio_recording as ar

    screens = screens if screens is not None else [
        {"id": "screen-0", "label": "Built-in Retina Display (2880x1800)", "x": 0, "y": 0, "width": 2880,
         "height": 1800, "all": False, "primary": True},
        {"id": "screen-1", "label": "DELL U2720Q (3840x2160)", "x": 0, "y": 0, "width": 3840, "height": 2160,
         "all": False, "primary": False},
    ]

    class Dock(ar.AudioRecordingDockContent):
        def __init__(self):     # the real one builds Qt widgets; the stub QWidget is a MagicMock
            super(ar.AudioRecordingDockContent, self).__init__()

        def sender(self):
            return self._sender

        def _backend_available(self):
            return bool(available)

        def _system_audio_available(self):
            return bool(system_audio)

        def refresh_devices(self):
            self.device_refreshes += 1
            current = self.device_combo.currentData()
            self.device_combo.clear()
            self.device_combo.addItem("Default input", ("", ""))
            for name in mics:
                self.device_combo.addItem(name, (name, "CoreAudio"))
            index = self.device_combo.findData(current)
            if index >= 0:
                self.device_combo._index = index

        def refresh_cameras(self):
            self._camera_devices_refreshed = True
            self.camera_refreshes += 1
            current = self.camera_combo.currentData()
            self.camera_combo.clear()
            if not cameras:
                self.camera_combo.addItem("No webcam found", None)
            for name in cameras:
                self.camera_combo.addItem(name, "device-" + name)
            index = self.camera_combo.findData(current)
            if index >= 0:
                self.camera_combo._index = index
            self.camera_size_combo.clear()
            for size in ((1920, 1080), (1280, 720), (640, 480)):
                self.camera_size_combo.addItem("%s x %s" % size, size)
            self.camera_size_combo._index = 1
            self.camera_fps_combo.clear()
            for fps in (15, 30):
                self.camera_fps_combo.addItem(str(fps), fps)
            self.camera_fps_combo._index = 1

        def _sync_channel_options(self):
            self.mono_button.setEnabled(True)
            self.stereo_button.setEnabled(not mono_only)
            self._set_channels(self._channels, restart=False)

        def _set_wait_cursor(self, enabled):
            pass

        def _restart_monitoring(self):
            self.monitoring = bool(self.mic_card.isChecked())

        def _ensure_monitoring(self):
            self.monitoring = bool(self.mic_card.isChecked())

        def _stop_monitoring(self):
            self.monitoring = False

        def _restart_webcam_preview(self):
            self.webcam_preview = bool(self.camera_card.isChecked())

        def _stop_webcam_preview(self):
            self.webcam_preview = False

    dock = Dock()
    dock.device_refreshes = dock.camera_refreshes = 0
    dock.monitoring = dock.webcam_preview = False
    dock._sender = None
    dock._recording = dock._starting = False
    dock._context_start = dock._context_track = None
    dock._camera_devices_refreshed = False
    dock._channels, dock._preferred_format, dock._sample_rate = 1, "flac", 48000
    dock._audio_channel_support_cache = {}
    dock._screen_window_id = ""
    dock._hide_openshot_user_set = False
    dock._preview_before_screen, dock._preview_forced_off = "full", False
    dock._webcam_layout_default_state = None
    tips = {"mic": "Audio recording is not available.",
            "screen": "Screen recording is not available for this platform or libopenshot build.",
            "webcam": "Webcam recording is not available for this platform or libopenshot build."}
    dock.mic_card = FakeCard(dock, "mic" in available, "" if "mic" in available else tips["mic"])
    dock.screen_card = FakeCard(dock, "screen" in available, "" if "screen" in available else tips["screen"])
    dock.camera_card = FakeCard(dock, "webcam" in available, "" if "webcam" in available else tips["webcam"])
    for name in ("mic_section", "screen_section", "camera_section", "preview_label", "screen_status_label"):
        setattr(dock, name, FakeWidget())
    dock.mono_button, dock.stereo_button = FakeButton("Mono", True), FakeButton("Stereo")
    dock.full_screen_button = FakeButton("Full Screen", True)
    dock.window_button, dock.region_button = FakeButton("Window"), FakeButton("Region")
    dock.record_button = FakeButton("Start Recording")
    dock.device_combo = FakeCombo(on_change=lambda: dock._mic_device_changed())
    dock.format_combo = FakeCombo([("WAV", "wav"), ("FLAC", "flac"), ("MP3", "mp3")],
                                  on_change=lambda: dock._format_changed())
    dock.format_combo._index = 1
    dock.sample_rate_combo = FakeCombo([("%s Hz" % r, r) for r in (44100, 48000, 96000)],
                                       on_change=lambda: dock._sample_rate_changed())
    dock.channels_combo = FakeCombo([("Mono", 1), ("Stereo", 2)], on_change=lambda: dock._channels_changed())
    dock.screen_display_edit = FakeCombo([(s["label"], s) for s in screens],
                                         on_change=lambda: dock._screen_source_changed())
    dock.screen_x_spin, dock.screen_y_spin = FakeSpin(0), FakeSpin(0)
    dock.screen_width_spin, dock.screen_height_spin = FakeSpin(2880), FakeSpin(1800)
    dock.system_audio_combo = FakeCombo([("On", True), ("Off", False)])
    dock.capture_cursor_combo = FakeCombo([("On", True), ("Off", False)])
    dock.hide_openshot_combo = FakeCombo([("Yes", True), ("No", False)], on_change=lambda: dock._hide_openshot_changed())
    dock.hide_openshot_combo._index = 1
    dock.video_fps_combo = FakeCombo([(str(f), f) for f in (15, 24, 30, 60)])
    dock.video_fps_combo._index = 2
    dock.camera_combo = FakeCombo(on_change=lambda: None)
    dock.camera_size_combo = FakeCombo(on_change=lambda: None)
    dock.camera_fps_combo = FakeCombo()
    dock.webcam_layout_combo = FakeCombo([(k, k) for k in ("bottom-right", "top-right", "bottom-left", "top-left",
                                                           "left", "right", "center", "full")])
    dock.webcam_layout_size_combo = FakeCombo([("Corner (20%)", 0.2), ("Corner (30%)", 0.3), ("Corner (40%)", 0.4)])
    dock.webcam_layout_size_combo._index = 1
    dock.webcam_corner_radius_combo = FakeCombo([("Rectangle", 0.0), ("Rounded", 0.15), ("Oval", 0.5)])
    dock.track_combo = FakeCombo()
    dock.preview_combo = FakeCombo([("Off", "none"), ("Full", "full"), ("Half", "half"), ("Quarter", "quarter")])
    dock.preview_combo._index = 1
    dock._sync_backend_state()
    return dock


def install_recording(editor, monkeypatch, **kwargs):
    """Attach a fake-widget Recording dock and the main-window hooks prepare_recording_tool uses."""
    from unittest.mock import MagicMock

    import windows.audio_recording as ar

    monkeypatch.setattr(ar, "get_app", lambda: editor.app)
    editor.app._tr = lambda text: text
    dock = make_recording_dock(**kwargs)
    win = editor.window
    win.audio_recording_content = dock
    win._ensure_audio_recording_dock_content = MagicMock()
    win.SeekSignal = MagicMock()
    win.actionAudio_Recording_View_trigger = MagicMock()
    shown = []

    def show_audio_recording_dock(start_time=None, track_number=None):     # MainWindow's
        shown.append((start_time, track_number))
        dock.set_recording_context(start_time, track_number)

    win.show_audio_recording_dock = show_audio_recording_dock
    return types.SimpleNamespace(dock=dock, window=win, shown=shown)
