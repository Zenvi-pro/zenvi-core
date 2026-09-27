#!/usr/bin/env python3
import json
import os
import threading
import time
from pathlib import Path

from qt_api import QEvent, QObject, QTimer
from qt_api import QAction, QDialog, QDockWidget

from classes import info
from classes.app import get_app
from classes.logger import log


def _env_true(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    value = os.getenv(name)
    if value is None:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


class UiTraceRecorder(QObject):
    """Record UI traces in a centralized, env-configured subsystem."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._updates_handle = None
        self._selections_handle = None
        self._events_handle = None
        self._dialog_connections = {}
        self._action_connections = []
        self._dock_connections = []
        self._cache_timer = None
        self._last_cache_signature = None
        self._updates_listener_connected = False
        self._selection_connected = False
        self._dialog_filter_installed = False

        self._include_load = _env_true("OPENSHOT_UI_TRACE_INCLUDE_LOAD", default=False)
        self._include_ignored = _env_true("OPENSHOT_UI_TRACE_INCLUDE_IGNORED", default=False)
        self._trace_actions = _env_true("OPENSHOT_UI_TRACE_ACTIONS", default=True)
        self._trace_dialogs = _env_true("OPENSHOT_UI_TRACE_DIALOGS", default=True)
        self._trace_docks = _env_true("OPENSHOT_UI_TRACE_DOCKS", default=True)
        self._trace_cache = _env_true("OPENSHOT_UI_TRACE_CACHE", default=False)
        self._cache_interval_ms = max(100, int(_env_float("OPENSHOT_UI_TRACE_CACHE_INTERVAL", 0.5) * 1000))
        self.enabled = False

        updates_path, selections_path, events_path = self._resolve_paths()
        if not updates_path and not selections_path and not events_path:
            return

        try:
            if updates_path:
                self._updates_handle = self._open_jsonl(updates_path)
            if selections_path:
                self._selections_handle = self._open_jsonl(selections_path)
            if events_path:
                self._events_handle = self._open_jsonl(events_path)

            if self._updates_handle or self._events_handle:
                get_app().updates.add_listener(self)
                self._updates_listener_connected = True
            if self._selections_handle or self._events_handle:
                self.window.SelectionChanged.connect(self._on_selection_changed)
                self._selection_connected = True
            if self._events_handle and self._trace_actions:
                self._connect_actions()
            if self._events_handle and self._trace_dialogs:
                get_app().installEventFilter(self)
                self._dialog_filter_installed = True
            if self._events_handle and self._trace_docks:
                self._connect_docks()
            if self._events_handle and self._trace_cache:
                self._start_cache_timer()

            self.enabled = True
            self._write_meta()
            log.info(
                "UI trace recorder enabled. updates=%s selections=%s events=%s",
                updates_path,
                selections_path,
                events_path,
            )
        except Exception:
            log.error("Failed to initialize UI trace recorder", exc_info=1)
            self.close()

    def _resolve_paths(self):
        updates_path = os.getenv("OPENSHOT_UI_TRACE_UPDATES", "").strip()
        selections_path = os.getenv("OPENSHOT_UI_TRACE_SELECTIONS", "").strip()
        events_path = os.getenv("OPENSHOT_UI_TRACE_EVENTS", "").strip()

        trace_dir = os.getenv("OPENSHOT_UI_TRACE_DIR", "").strip()
        trace_enabled = _env_true("OPENSHOT_UI_TRACE", default=False)

        if trace_dir and (trace_enabled or updates_path or selections_path or events_path):
            stamp = time.strftime("%Y%m%d_%H%M%S")
            pid = os.getpid()
            trace_root = Path(trace_dir).expanduser().resolve()
            trace_root.mkdir(parents=True, exist_ok=True)
            if not updates_path:
                updates_path = str(trace_root / f"updates_{stamp}_{pid}.jsonl")
            if not selections_path:
                selections_path = str(trace_root / f"selections_{stamp}_{pid}.jsonl")
            if not events_path:
                events_path = str(trace_root / f"events_{stamp}_{pid}.jsonl")

        if not trace_enabled and not updates_path and not selections_path and not events_path:
            return None, None, None

        return updates_path or None, selections_path or None, events_path or None

    @staticmethod
    def _open_jsonl(path):
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return open(p, "a", encoding="utf-8", buffering=1)

    def _next_seq(self):
        with self._lock:
            self._seq += 1
            return self._seq

    def _event_base(self, kind):
        return {
            "event": kind,
            "seq": self._next_seq(),
            "ts": round(time.time(), 6),
        }

    def _write_jsonl(self, handle, payload):
        if handle is None or self._closed:
            return
        line = json.dumps(payload, ensure_ascii=False, default=str)
        handle.write(line + "\n")

    def _write_event(self, payload, include_updates=False, include_selections=False):
        self._write_jsonl(self._events_handle, payload)
        if include_updates:
            self._write_jsonl(self._updates_handle, payload)
        if include_selections:
            self._write_jsonl(self._selections_handle, payload)

    def _write_meta(self):
        payload = self._event_base("meta")
        payload.update(
            {
                "pid": os.getpid(),
                "include_load": self._include_load,
                "include_ignored": self._include_ignored,
                "trace_actions": self._trace_actions,
                "trace_dialogs": self._trace_dialogs,
                "trace_docks": self._trace_docks,
                "trace_cache": self._trace_cache,
                "cache_interval_ms": self._cache_interval_ms,
                "project_path": str(get_app().project.current_filepath or ""),
            }
        )
        self._write_event(payload, include_updates=True, include_selections=True)

    def changed(self, action):
        if (self._updates_handle is None and self._events_handle is None) or self._closed:
            return
        try:
            ignore_history = bool(get_app().updates.ignore_history)
            if action.type == "load" and not self._include_load:
                return
            if ignore_history and not self._include_ignored:
                return
            payload = self._event_base("update")
            payload.update(
                {
                    "action_type": action.type,
                    "transaction": action.transaction,
                    "key": action.key,
                    "value": action.values,
                    "old_values": action.old_values,
                    "ignore_history": ignore_history,
                    "data_version": int(get_app().updates.data_version),
                }
            )
            self._write_event(payload, include_updates=True)
        except Exception:
            log.error("UI trace update write failed", exc_info=1)

    def _on_selection_changed(self):
        if (self._selections_handle is None and self._events_handle is None) or self._closed:
            return
        try:
            payload = self._event_base("selection")
            payload.update(
                {
                    "selected_items": json.loads(json.dumps(self.window.selected_items)),
                    "selected_clips": list(self.window.selected_clips),
                    "selected_transitions": list(self.window.selected_transitions),
                    "selected_effects": list(self.window.selected_effects),
                    "selected_tracks": list(self.window.selected_tracks),
                    "selected_markers": list(self.window.selected_markers),
                    "show_property_id": self.window.show_property_id,
                    "show_property_type": self.window.show_property_type,
                }
            )
            self._write_event(payload, include_selections=True)
        except Exception:
            log.error("UI trace selection write failed", exc_info=1)

    def _connect_actions(self):
        for action in self.window.findChildren(QAction):
            try:
                slot = self._make_action_slot(action)
                action.triggered.connect(slot)
                self._action_connections.append((action, slot))
            except Exception:
                log.debug("Failed to connect QAction trace for %s", action.objectName(), exc_info=1)

    def _make_action_slot(self, action):
        def _slot(checked=False):
            self._on_action_triggered(action, checked)

        return _slot

    def _on_action_triggered(self, action, checked):
        if self._events_handle is None or self._closed:
            return
        payload = self._event_base("action_triggered")
        payload.update(
            {
                "action_name": action.objectName() or "",
                "action_text": (action.text() or "").replace("&", ""),
                "checked": bool(checked),
            }
        )
        self._write_event(payload)

    def _connect_docks(self):
        for dock in self.window.findChildren(QDockWidget):
            try:
                visibility_slot = self._make_dock_visibility_slot(dock)
                dock.visibilityChanged.connect(visibility_slot)
                self._dock_connections.append((dock.visibilityChanged, visibility_slot))
            except Exception:
                log.debug("Failed to connect dock visibility trace for %s", dock.objectName(), exc_info=1)

    def _make_dock_visibility_slot(self, dock):
        def _slot(visible):
            self._on_dock_visibility_changed(dock, visible)

        return _slot

    def _on_dock_visibility_changed(self, dock, visible):
        if self._events_handle is None or self._closed:
            return
        payload = self._event_base("dock_visibility")
        payload.update(
            {
                "object_name": dock.objectName() or "",
                "window_title": dock.windowTitle() or "",
                "visible": bool(visible),
                "floating": bool(dock.isFloating()),
            }
        )
        self._write_event(payload)

    def eventFilter(self, obj, event):
        if self._closed or not self._trace_dialogs or self._events_handle is None:
            return False
        try:
            if isinstance(obj, QDialog):
                event_type = event.type()
                if event_type == QEvent.Show:
                    self._attach_dialog(obj)
                    self._emit_dialog("shown", obj)
                elif event_type == QEvent.Hide:
                    self._emit_dialog("hidden", obj)
                elif event_type == QEvent.Close:
                    self._emit_dialog("closed", obj)
        except Exception:
            log.debug("UI trace dialog eventFilter failed", exc_info=1)
        return False

    def _attach_dialog(self, dialog):
        key = id(dialog)
        if key in self._dialog_connections:
            return
        slots = []
        try:
            accepted_slot = lambda: self._emit_dialog("accepted", dialog, result="accepted")
            dialog.accepted.connect(accepted_slot)
            slots.append((dialog.accepted, accepted_slot))
        except Exception:
            pass
        try:
            rejected_slot = lambda: self._emit_dialog("rejected", dialog, result="rejected")
            dialog.rejected.connect(rejected_slot)
            slots.append((dialog.rejected, rejected_slot))
        except Exception:
            pass
        try:
            finished_slot = lambda code: self._emit_dialog("finished", dialog, result=int(code))
            dialog.finished.connect(finished_slot)
            slots.append((dialog.finished, finished_slot))
        except Exception:
            pass
        self._dialog_connections[key] = slots

    def _emit_dialog(self, phase, dialog, result=None):
        payload = self._event_base("dialog_lifecycle")
        payload.update(
            {
                "phase": phase,
                "class_name": dialog.__class__.__name__,
                "object_name": dialog.objectName() or "",
                "window_title": dialog.windowTitle() or "",
                "modal": bool(dialog.isModal()),
            }
        )
        if result is not None:
            payload["result"] = result
        self._write_event(payload)

    def _start_cache_timer(self):
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(self._cache_interval_ms)
        self._cache_timer.timeout.connect(self._sample_cache_progress)
        self._cache_timer.start()

    @staticmethod
    def _metric_call(obj, name):
        attr = getattr(obj, name, None)
        if attr is None:
            return None
        try:
            return attr() if callable(attr) else attr
        except Exception:
            return None

    def _collect_cache_state(self):
        state = {
            "current_frame": int(getattr(self.window, "current_frame", 0) or 0),
        }
        cache_obj = getattr(self.window, "cache_object", None)
        if cache_obj is not None:
            state["cache_class"] = cache_obj.__class__.__name__
            metrics = {}
            for name in (
                "Count",
                "GetCount",
                "Size",
                "GetSize",
                "Bytes",
                "GetBytes",
                "FrameCount",
                "GetFrameCount",
                "MaxBytes",
                "GetMaxBytes",
            ):
                value = self._metric_call(cache_obj, name)
                if value is not None:
                    metrics[name] = value
            if metrics:
                state["metrics"] = metrics

        preview_cache = Path(info.PREVIEW_CACHE_PATH)
        if preview_cache.exists():
            file_count = 0
            total_bytes = 0
            for child in preview_cache.iterdir():
                try:
                    if child.is_file():
                        file_count += 1
                        total_bytes += child.stat().st_size
                except Exception:
                    continue
            state["preview_cache_files"] = file_count
            state["preview_cache_bytes"] = total_bytes
        return state

    def _sample_cache_progress(self):
        if self._events_handle is None or self._closed:
            return
        try:
            state = self._collect_cache_state()
            signature = json.dumps(state, sort_keys=True, default=str)
            if signature == self._last_cache_signature:
                return
            self._last_cache_signature = signature
            payload = self._event_base("cache_progress")
            payload.update(state)
            self._write_event(payload)
        except Exception:
            log.debug("UI trace cache sampler failed", exc_info=1)

    def close(self):
        if self._closed:
            return
        self._closed = True

        if self._cache_timer:
            try:
                self._cache_timer.stop()
            except Exception:
                pass
            self._cache_timer = None

        try:
            if self._updates_listener_connected:
                get_app().updates.disconnect_listener(self)
        except Exception:
            pass
        self._updates_listener_connected = False

        try:
            if self._selection_connected:
                self.window.SelectionChanged.disconnect(self._on_selection_changed)
        except Exception:
            pass
        self._selection_connected = False

        if self._dialog_filter_installed:
            try:
                get_app().removeEventFilter(self)
            except Exception:
                pass
            self._dialog_filter_installed = False

        for action, slot in self._action_connections:
            try:
                action.triggered.disconnect(slot)
            except Exception:
                pass
        self._action_connections = []

        for signal, slot in self._dock_connections:
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        self._dock_connections = []

        for slots in self._dialog_connections.values():
            for signal, slot in slots:
                try:
                    signal.disconnect(slot)
                except Exception:
                    pass
        self._dialog_connections.clear()

        for handle in (self._updates_handle, self._selections_handle, self._events_handle):
            try:
                if handle:
                    handle.flush()
                    handle.close()
            except Exception:
                pass
        self._updates_handle = None
        self._selections_handle = None
        self._events_handle = None
