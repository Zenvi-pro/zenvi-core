"""Colour-agent ColorGrade payloads rendered by the real libopenshot.

Needs real PyQt5 + libopenshot (ZENVI_REAL_QT=1 and openshot on PYTHONPATH);
skipped under the headless stub.
"""

import json

import pytest

QtGui = pytest.importorskip("PyQt5.QtGui")
openshot = pytest.importorskip("openshot")
if not hasattr(openshot, "EffectInfo"):
    pytest.skip("needs the real libopenshot module", allow_module_level=True)

from classes import color_agent as ca  # noqa: E402


def _fresh_grade():
    effect = openshot.EffectInfo().CreateEffect("ColorGrade")
    effect.Id("grade1")
    return json.loads(effect.Json())


def _render_grey(payload, level, tmp_path):
    """Run a solid grey frame through a ColorGrade payload; return its RGB."""
    effect = openshot.EffectInfo().CreateEffect("ColorGrade")
    effect.SetJson(json.dumps(payload))
    frame = openshot.Frame(1, 16, 16, "#%02x%02x%02x" % (level, level, level))
    out_path = str(tmp_path / f"grey_{level}.png")
    effect.GetFrame(frame, 1).Save(out_path, 1.0, "PNG", 100)
    color = QtGui.QColor(QtGui.QImage(out_path).pixel(8, 8))
    return color.red(), color.green(), color.blue()


@pytest.mark.parametrize("level", [64, 128, 192])
def test_neutral_agent_grade_leaves_pixels_alone(level, tmp_path):
    # With BEZIER curve nodes this came out (4, 4, 4) for level 64.
    assert _render_grey(ca.blank_color_grade("g"), level, tmp_path) == (level, level, level)
