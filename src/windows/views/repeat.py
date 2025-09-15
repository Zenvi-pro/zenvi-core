"""
 @file
 @brief This file contains repeat time keyframe logic (for Time->Repeat menu)
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
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

import copy
import openshot
from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QComboBox, QSpinBox, QDoubleSpinBox,
    QDialogButtonBox, QHBoxLayout
)
from classes.app import get_app

_ = get_app()._tr


class RepeatDialog(QDialog):
    """Simple dialog to collect custom repeat options."""

    def __init__(self, parent=None, pattern="loop", direction=1):
        super().__init__(parent)
        self.setWindowTitle(_("Custom Repeat"))
        layout = QFormLayout(self)

        self.pattern_combo = QComboBox(self)
        self.pattern_combo.addItems([_("Loop"), _("Ping-Pong")])
        self.pattern_combo.setCurrentIndex(0 if pattern == "loop" else 1)
        layout.addRow(_("Pattern"), self.pattern_combo)

        self.direction_combo = QComboBox(self)
        self.direction_combo.addItems([_("Forward"), _("Reverse")])
        self.direction_combo.setCurrentIndex(0 if direction > 0 else 1)
        layout.addRow(_("Direction"), self.direction_combo)

        self.passes_spin = QSpinBox(self)
        self.passes_spin.setRange(2, 500)
        self.passes_spin.setValue(2)
        layout.addRow(_("Passes"), self.passes_spin)

        delay_layout = QHBoxLayout()
        self.delay_spin = QDoubleSpinBox(self)
        self.delay_spin.setRange(0.0, 100000.0)
        self.delay_spin.setDecimals(3)
        self.delay_unit = QComboBox(self)
        self.delay_unit.addItems([_("frames"), _("ms"), _("sec")])
        delay_layout.addWidget(self.delay_spin)
        delay_layout.addWidget(self.delay_unit)
        layout.addRow(_("Delay"), delay_layout)

        self.ramp_spin = QDoubleSpinBox(self)
        self.ramp_spin.setRange(-1000.0, 1000.0)
        self.ramp_spin.setDecimals(3)
        layout.addRow(_("Speed Ramp (%)"), self.ramp_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_values(self, fps_float):
        pattern = "loop" if self.pattern_combo.currentIndex() == 0 else "pingpong"
        direction = 1 if self.direction_combo.currentIndex() == 0 else -1
        passes = self.passes_spin.value()
        delay_val = self.delay_spin.value()
        unit = self.delay_unit.currentText()
        if unit == _("frames"):
            delay_frames = int(round(delay_val))
        elif unit == _("ms"):
            delay_frames = int(round((delay_val / 1000.0) * fps_float))
        else:
            delay_frames = int(round(delay_val * fps_float))
        ramp = self.ramp_spin.value() / 100.0
        return pattern, direction, passes, delay_frames, ramp


# Repeat logic


def _normalize_points(points, relative_y=False):
    """Normalize points so X starts at 1. Optionally shift Y similarly."""
    if not points:
        return []
    pts = sorted(points, key=lambda p: int(round(p.get("co", {}).get("X", 0))))
    x0 = int(round(pts[0]["co"]["X"]))
    y0 = int(round(pts[0]["co"].get("Y", 0))) if relative_y else 0
    out = []
    for p in pts:
        x = int(round(p["co"].get("X", 0))) - x0 + 1
        y = p["co"].get("Y")
        if relative_y and y is not None:
            y = int(round(y)) - y0 + 1
        out.append({"co": {"X": x, "Y": y}, "interpolation": p.get("interpolation", openshot.LINEAR)})
    return out


def _repeat_curve(points, span_x, dir_sign, passes, delay_frames, ramp, pattern, is_time=False, span_y=None):
    """Repeat normalized points applying ramp, delay, and direction."""
    new_points = []
    base = 0
    dir_local = dir_sign
    if is_time and span_y is None and points:
        span_y = int(round(points[-1]["co"].get("Y", 0)))
    for k in range(passes):
        speed = (1 + ramp) ** k
        scale = 1 / abs(speed)
        dur = max(1, int(round(span_x * scale)))
        pts_iter = points if dir_local > 0 else reversed(points)
        for p in pts_iter:
            x = int(round(p["co"].get("X", 0))) - 1
            y = p["co"].get("Y")
            nx_off = int(round(x * scale))
            if dir_local > 0:
                nx = base + nx_off + 1
                ny = y if not is_time else y
            else:
                nx = base + (dur - nx_off)
                ny = y if not is_time else span_y - y + 1
            new_points.append({"co": {"X": nx, "Y": ny}, "interpolation": p.get("interpolation", openshot.LINEAR)})
        base += dur
        if k < passes - 1 and delay_frames:
            last_y = new_points[-1]["co"].get("Y")
            new_points.append({"co": {"X": base, "Y": last_y}, "interpolation": openshot.LINEAR})
            new_points.append({"co": {"X": base + delay_frames, "Y": last_y}, "interpolation": openshot.LINEAR})
            base += delay_frames
        if pattern == "pingpong":
            dir_local *= -1
    return new_points, base

def apply_repeat(clip, pattern, start_dir, passes, delay_frames, ramp, fps_float):
    """Apply repeat stamping to a clip."""
    if passes < 2:
        return

    # Normalize existing time curve or build linear default
    orig_time = clip.data.get("time", {}).get("Points", [])
    if isinstance(orig_time, list) and len(orig_time) >= 2:
        base_time = _normalize_points(orig_time, relative_y=True)
    else:
        span_s = float(clip.data["end"]) - float(clip.data["start"])
        base_frames = max(1, int(round(span_s * fps_float)))
        base_time = [
            {"co": {"X": 1, "Y": 1}, "interpolation": openshot.LINEAR},
            {"co": {"X": base_frames, "Y": base_frames}, "interpolation": openshot.LINEAR},
        ]
    time_span_x = int(round(base_time[-1]["co"]["X"]))
    time_span_y = int(round(base_time[-1]["co"]["Y"]))

    # Store original data if not already
    if "repeat_cache" not in clip.data:
        cache = {
            "end": clip.data["end"],
            "duration": clip.data["duration"],
            "properties": {},
        }
        for k, v in clip.data.items():
            if isinstance(v, dict) and isinstance(v.get("Points"), list) and len(v["Points"]) > 1:
                cache["properties"][k] = copy.deepcopy(v)
        clip.data["repeat_cache"] = cache

    dir_sign = 1 if start_dir >= 0 else -1

    # Build time curve based on existing keyframes
    time_points, total_frames = _repeat_curve(
        base_time, time_span_x, dir_sign, passes, delay_frames, ramp, pattern, True, time_span_y
    )
    clip.data["time"] = {"Points": time_points}

    # Repeat animated properties
    cache = clip.data.get("repeat_cache", {})
    for prop, original in cache.get("properties", {}).items():
        if prop == "time":
            continue
        norm = _normalize_points(original.get("Points", []))
        span = int(round(norm[-1]["co"]["X"])) if norm else 0
        if span:
            new_points, used = _repeat_curve(norm, span, dir_sign, passes, delay_frames, ramp, pattern)
            clip.data[prop] = {"Points": new_points}
            total_frames = max(total_frames, used)

    # Update end/duration
    clip.data["end"] = float(clip.data["start"]) + (total_frames / fps_float)
    clip.data["duration"] = clip.data["end"] - clip.data["start"]


def reset_repeat(clip):
    cache = clip.data.pop("repeat_cache", None)
    if not cache:
        return
    clip.data["end"] = cache.get("end", clip.data.get("end"))
    clip.data["duration"] = cache.get("duration", clip.data.get("duration"))
    for prop, data in cache.get("properties", {}).items():
        clip.data[prop] = data
    if "time" not in cache.get("properties", {}):
        clip.data["time"] = {"Points": [{"co": {"X": 1, "Y": 1}, "interpolation": openshot.LINEAR}]}
