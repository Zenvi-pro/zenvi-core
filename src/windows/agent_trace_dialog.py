"""Agent Trace dialog — inspect tool/LLM/plan telemetry for the active chat session."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
)


class _TraceFetchWorker(QThread):
    finished_ok = pyqtSignal(dict)
    finished_err = pyqtSignal(str)

    def __init__(self, session_id: str, limit: int = 300, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.limit = limit

    def run(self):
        try:
            from classes.api_client import get_backend_client

            data = get_backend_client().get_session_trace(self.session_id, limit=self.limit)
            self.finished_ok.emit(data or {})
        except Exception as e:
            self.finished_err.emit(str(e))


class AgentTraceDialog(QDialog):
    """Browse session telemetry: tool_start/tool_end args + results, plan steps, llm_end."""

    def __init__(self, session_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agent Trace")
        self.setMinimumSize(820, 520)
        self.resize(960, 600)
        self._session_id = session_id or ""
        self._events: List[Dict[str, Any]] = []
        self._worker: Optional[_TraceFetchWorker] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        self._status = QLabel(f"Session: {self._session_id or '(none)'}")
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self._status, 1)

        self._filter = QComboBox()
        self._filter.addItem("All events", "")
        for t in ("tool_start", "tool_end", "plan_step", "llm_end", "subagent"):
            self._filter.addItem(t, t)
        self._filter.currentIndexChanged.connect(self._rebuild_list)
        top.addWidget(self._filter)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self._refresh_btn)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select)
        split.addWidget(self._list)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        mono = QFont("Consolas", 10)
        if not mono.exactMatch():
            mono = QFont("Courier New", 10)
        self._detail.setFont(mono)
        self._detail.setPlaceholderText("Select an event to inspect args / result.")
        split.addWidget(self._detail)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)

        hint = QLabel(
            "Shows structured JSONL telemetry from the backend "
            "(tool args/results, plan skips, timings). "
            "Failed tools stay visible here even if chat collapses them."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(hint)

        self.refresh()

    def refresh(self):
        if not self._session_id:
            self._status.setText("No active session")
            return
        if self._worker and self._worker.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._status.setText(f"Loading trace for {self._session_id}…")
        self._worker = _TraceFetchWorker(self._session_id, limit=400, parent=self)
        self._worker.finished_ok.connect(self._on_loaded)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    @pyqtSlot(dict)
    def _on_loaded(self, data: dict):
        self._refresh_btn.setEnabled(True)
        self._events = list(data.get("events") or [])
        err = data.get("error")
        self._status.setText(
            f"Session: {self._session_id}  ·  {len(self._events)} event(s)"
            + (f"  ·  {err}" if err else "")
        )
        self._rebuild_list()

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        self._refresh_btn.setEnabled(True)
        self._status.setText(f"Error: {msg}")
        self._detail.setPlainText(msg)

    def _rebuild_list(self):
        want = self._filter.currentData() or ""
        self._list.clear()
        self._filtered: List[Dict[str, Any]] = []
        for ev in self._events:
            if want and ev.get("type") != want:
                continue
            self._filtered.append(ev)
            item = QListWidgetItem(self._row_label(ev))
            if ev.get("type") == "tool_end" and (
                ev.get("error") or not ev.get("ok", True)
            ):
                item.setForeground(Qt.red)
            self._list.addItem(item)
        if self._filtered:
            self._list.setCurrentRow(len(self._filtered) - 1)

    def _row_label(self, ev: Dict[str, Any]) -> str:
        et = str(ev.get("type") or "?")
        tool = str(ev.get("tool") or ev.get("step_id") or "")
        ms = ev.get("duration_ms")
        ms_s = f"  {ms}ms" if ms is not None else ""
        status = ""
        if et == "tool_end":
            status = " OK" if ev.get("ok") else " FAIL"
        elif et == "plan_step":
            status = f" {ev.get('status') or ''}"
            if ev.get("reason"):
                status += f" ({ev.get('reason')})"
        return f"{et}{status}  {tool}{ms_s}".strip()

    def _on_select(self, row: int):
        if row < 0 or row >= len(getattr(self, "_filtered", [])):
            self._detail.clear()
            return
        ev = self._filtered[row]
        self._detail.setPlainText(json.dumps(ev, indent=2, ensure_ascii=False, default=str))
