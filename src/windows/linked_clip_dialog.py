"""
 @file
 @brief Linked Source > Edit Props...: a linked clip's source, freshness and props, with Apply & Re-render.

 The props editor is built from the props' current values (string -> line
 edit, ``#RRGGBB`` -> colour button, whole number -> spin box, number ->
 decimal spin box, true/false -> check box, lists/objects -> JSON field);
 the Raw JSON tab edits the whole object. The dialog only edits values: the
 re-render runs off the GUI thread after it closes
 (``windows.handoff_menus.rerender_files``).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional, Tuple

from qt_api import (
    QButtonGroup, QCheckBox, QColor, QColorDialog, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from classes.app import get_app

COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
INT_LIMIT = 2_000_000_000


# ---------------------------------------------------------------------------
# Pure rules (tested headlessly)
# ---------------------------------------------------------------------------

def editor_kind(value: Any) -> str:
    """Which editor a prop value gets: color, text, int, float, bool or json."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if abs(value) < INT_LIMIT else "float"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "color" if COLOR_RE.match(value.strip()) else "text"
    if value is None:
        return "text"
    return "json"


def parse_json_field(text: str, name: str) -> Any:
    """A JSON field's value; ValueError names the prop."""
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{name}: not valid JSON ({exc})") from None


def parse_props_json(text: str) -> Dict[str, Any]:
    """The Raw JSON tab's object; ValueError when it is not one."""
    try:
        value = json.loads(text or "{}")
    except ValueError as exc:
        raise ValueError(f"the props are not valid JSON ({exc})") from None
    if not isinstance(value, dict):
        raise ValueError("the props must be a JSON object, e.g. {\"title\": \"Hello\"}")
    return value


def merge_edited_props(stored: Dict[str, Any], editable: Dict[str, Any], edited: Dict[str, Any]) -> Dict[str, Any]:
    """The link's new props after the dialog: the stored props, the edited values on top, and
    the editable keys the user removed (Raw JSON) taken out. Props the provider did not offer
    for editing are kept; defaults it offered and the user left alone are stored as shown."""
    out = dict(stored or {})
    for key in editable or {}:
        if key not in edited:
            out.pop(key, None)
    out.update(edited or {})
    return out


def state_text(state: Optional[str], detail: str = "", tr=lambda s: s) -> str:
    labels = {"fresh": tr("Up to date"), "stale": tr("Source changed — re-render to update"),
              "rendering": tr("Rendering…"), "error": tr("Last render failed"),
              "missing_source": tr("Source not found")}
    text = labels.get(state or "", tr("Not checked yet"))
    return f"{text}: {detail}" if detail and state in ("error", "missing_source", "stale") else text


def describe_source(link: dict) -> str:
    source = link.get("source") or {}
    parts = []
    if source.get("composition"):
        parts.append(str(source["composition"]))
    if source.get("project_dir"):
        parts.append(str(source["project_dir"]))
    if source.get("aep"):
        parts.append(str(source["aep"]))
    if source.get("file"):
        line = source.get("line")
        parts.append("%s%s" % (source["file"], ":%d" % line if line else ""))
    return " — ".join(parts)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class _ColorButton(QPushButton):
    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        self.changed = None
        self._value = value
        self.clicked.connect(self._pick)
        self._paint()

    def value(self) -> str:
        return self._value

    def _paint(self):
        self.setText(self._value)
        self.setStyleSheet("QPushButton { background-color: %s; }" % self._value[:7])

    def _pick(self):
        color = QColorDialog.getColor(QColor(self._value[:7]), self, get_app()._tr("Choose Colour"))
        if color.isValid():
            alpha = self._value[7:9] if len(self._value) == 9 else ""
            self.set_value(color.name().upper() + alpha)

    def set_value(self, value: str) -> None:
        self._value = value
        self._paint()
        changed = getattr(self, "changed", None)
        if callable(changed):
            changed()


class LinkedClipDialog(QDialog):
    """Edit a linked clip's props; ``props()`` is the full new props object after ``exec_()`` accepts."""

    def __init__(self, link: dict, *, check: Optional[dict] = None, name: str = "", can_render: bool = True,
                 props: Optional[Dict[str, Any]] = None, note: str = "", parent=None):
        """*props*: what to edit -- the provider's ``editable_props(link)`` (default: the link's props)."""
        super().__init__(parent)
        _ = get_app()._tr
        self._ = _
        self.setObjectName("LinkedClipDialog")
        self.link = dict(link or {})
        self._props: Dict[str, Any] = dict(props if props is not None else (self.link.get("props") or {}))
        self._editors: Dict[str, Tuple[str, QWidget]] = {}
        self._result: Optional[Dict[str, Any]] = None
        from classes.handoff.linked_media import kind_label
        kind = kind_label(str(self.link.get("kind") or ""))
        self.setWindowTitle(_("Linked Clip: %s") % (name or kind))
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        info = QFormLayout()
        info.addRow(_("Kind:"), QLabel(kind))
        source_label = QLabel(describe_source(self.link) or _("(not recorded)"))
        source_label.setWordWrap(True)
        info.addRow(_("Source:"), source_label)
        state = (check or {}).get("state")
        self.state_label = QLabel(state_text(state, (check or {}).get("detail") or "", _))
        self.state_label.setWordWrap(True)
        info.addRow(_("State:"), self.state_label)
        render = self.link.get("render") or {}
        if render.get("rendered_at"):
            fps = render.get("fps") or {}
            rate = (float(fps.get("num", 0)) / float(fps.get("den", 1))) if fps else 0
            info.addRow(_("Rendered:"), QLabel("%s · %s · %sx%s · %s fps" % (
                render.get("rendered_at"), render.get("codec") or "?", render.get("width") or "?",
                render.get("height") or "?", ("%.3g" % rate) if rate else "?")))
        layout.addLayout(info)

        # Page switch: two checkable buttons over a tab widget whose own tab bar is hidden.
        # The dark themes style QTabBar tabs as icon-only dock tabs, which clipped the labels
        # ("Prop", "aw JSO"); buttons size to their text in every theme.
        switch = QHBoxLayout()
        switch.setSpacing(4)
        self.page_buttons = QButtonGroup(self)
        self.page_buttons.setExclusive(True)
        for index, text in enumerate((_("Props"), _("Raw JSON"))):
            button = QPushButton(text)
            button.setObjectName("propsPage%d" % index)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 28)
            self.page_buttons.addButton(button, index)
            button.clicked.connect(lambda _checked=False, i=index: self.tabs.setCurrentIndex(i))
            switch.addWidget(button)
        switch.addStretch(1)
        layout.addLayout(switch)
        # the selected page stands out in every theme (the dark themes have no :checked style)
        self.setStyleSheet("QPushButton#propsPage0:checked, QPushButton#propsPage1:checked "
                           "{ border: 1px solid #4d9cf6; background-color: rgba(77, 156, 246, 0.22); }")
        self.tabs = QTabWidget(self)
        self.tabs.tabBar().hide()
        self.form_page = QWidget()
        self.form = QFormLayout(self.form_page)
        self.tabs.addTab(self.form_page, _("Props"))
        self.json_edit = QPlainTextEdit()
        self.json_edit.setObjectName("propsJson")
        self.tabs.addTab(self.json_edit, _("Raw JSON"))
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

        self.error_label = QLabel("")
        self.error_label.setObjectName("propsError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #e5534b;")
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(self)
        self.apply_button = self.buttons.addButton(_("Apply && Re-render"), QDialogButtonBox.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        if not can_render:
            self.apply_button.setEnabled(False)
            self.error_label.setText(_("This Zenvi build cannot render %s links; the clip keeps its last render.")
                                     % kind)
        elif note:
            self.error_label.setText(note)
        layout.addWidget(self.buttons)
        self._build_form(self._props)

    # -- form <-> JSON ------------------------------------------------------
    def _build_form(self, props: Dict[str, Any]) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._editors.clear()
        self._dirty = set()
        if not props:
            self.form.addRow(QLabel(self._("This composition has no props to edit.")))
        for name, value in props.items():
            kind = editor_kind(value)
            widget: QWidget
            if kind == "bool":
                widget = QCheckBox()
                widget.setChecked(bool(value))
                widget.toggled.connect(lambda _v, n=name: self._dirty.add(n))
            elif kind == "int":
                widget = QSpinBox()
                widget.setRange(-INT_LIMIT, INT_LIMIT)
                widget.setValue(int(value))
                widget.valueChanged.connect(lambda _v, n=name: self._dirty.add(n))
            elif kind == "float":
                # a text field, not a spin box: a spin box would round 0.123456 or clamp 1.7e12
                widget = QLineEdit(repr(float(value)) if isinstance(value, float) else str(value))
                widget.textEdited.connect(lambda _t, n=name: self._dirty.add(n))
            elif kind == "color":
                widget = _ColorButton(str(value).strip())
                widget.changed = lambda n=name: self._dirty.add(n)  # type: ignore[attr-defined]
            elif kind == "json":
                widget = QPlainTextEdit(json.dumps(value, indent=2, ensure_ascii=False))
                widget.setMaximumHeight(110)
                widget.textChanged.connect(lambda n=name: self._dirty.add(n))
            else:
                widget = QLineEdit("" if value is None else str(value))
                widget.textEdited.connect(lambda _t, n=name: self._dirty.add(n))
            widget.setObjectName("prop_" + str(name))
            self._editors[name] = (kind, widget)
            self.form.addRow(str(name), widget)

    def _read_form(self) -> Dict[str, Any]:
        """The props, with every field the user did not touch exactly as it was."""
        out: Dict[str, Any] = {}
        for name, (kind, widget) in self._editors.items():
            original = self._props.get(name)
            if name not in self._dirty:
                out[name] = original
            elif kind == "bool":
                out[name] = widget.isChecked()  # type: ignore[attr-defined]
            elif kind == "int":
                out[name] = int(widget.value())  # type: ignore[attr-defined]
            elif kind == "float":
                text = widget.text().strip()  # type: ignore[attr-defined]
                try:
                    number = float(text)
                except ValueError:
                    raise ValueError(f"{name}: {text!r} is not a number") from None
                out[name] = int(number) if isinstance(original, int) and number == math.floor(number) else number
            elif kind == "color":
                out[name] = widget.value()  # type: ignore[attr-defined]
            elif kind == "json":
                out[name] = parse_json_field(widget.toPlainText(), name)  # type: ignore[attr-defined]
            else:
                text = widget.text()  # type: ignore[attr-defined]
                out[name] = None if (original is None and text == "") else text
        return out

    def _tab_changed(self, index: int) -> None:
        button = self.page_buttons.button(index)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.error_label.setText("")
        if index == 1:
            try:
                self._props = self._read_form()
            except ValueError as exc:
                self.error_label.setText(str(exc))
            self.json_edit.setPlainText(json.dumps(self._props, indent=2, ensure_ascii=False))
        else:
            try:
                self._props = parse_props_json(self.json_edit.toPlainText())
            except ValueError as exc:
                self.error_label.setText(str(exc))
                return
            self._build_form(self._props)

    def _apply(self) -> None:
        try:
            if self.tabs.currentIndex() == 1:
                props = parse_props_json(self.json_edit.toPlainText())
            else:
                props = self._read_form()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self._result = props
        self.accept()

    def props(self) -> Optional[Dict[str, Any]]:
        """The edited props (None until the dialog was accepted)."""
        return self._result
