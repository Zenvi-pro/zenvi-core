"""Each scope panel says what it shows (issue #276).

The dock widgets cannot be built against the headless Qt stub from
tests/conftest.py: an unstubbed method call on a widget makes MagicMock call the
widget's __init__ with name=..., which these constructors reject. So the widget
checks read the class source: each class must read its explanation from
SCOPE_EXPLANATIONS, the text its label and tooltip show.
"""

import ast
from pathlib import Path

import openshot
import pytest

# color_grade_editor reads openshot constants as default arguments at import
# time, and the headless stub has none of them, so supply them for that import only.
with pytest.MonkeyPatch.context() as mp:
    for _name in ("AUTO", "BEZIER", "CONSTANT", "LINEAR"):
        if not hasattr(openshot, _name):
            mp.setattr(openshot, _name, 0, raising=False)
    from windows import scope_panel

SCOPE_KEYS = ("waveform", "histogram", "vectorscope", "audio")
SHOWN_BY = (
    ("WaveformDockContent", "waveform"),
    ("HistogramDockContent", "histogram"),
    ("VectorscopeDockContent", "vectorscope"),
    ("AudioMeterWidget", "audio"),
    ("AudioLevelsDockContent", "audio"),     # the visible line above the meter
)


def test_every_scope_has_a_non_empty_explanation():
    explanations = scope_panel.SCOPE_EXPLANATIONS
    missing = [key for key in SCOPE_KEYS if not str(explanations.get(key) or "").strip()]
    assert missing == []


def _explanation_keys_read_by(class_name):
    tree = ast.parse(Path(scope_panel.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {
        node.slice.value
        for node in ast.walk(cls)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "SCOPE_EXPLANATIONS"
        and isinstance(node.slice, ast.Constant)
    }


@pytest.mark.parametrize("class_name, key", SHOWN_BY)
def test_scope_widget_reads_its_own_explanation(class_name, key):
    assert key in _explanation_keys_read_by(class_name)


def test_the_audio_levels_dock_shows_its_explanation_not_just_a_tooltip():
    main_window = Path(scope_panel.__file__).with_name("main_window.py").read_text(encoding="utf-8")
    assert "self.dockAudio.setWidget(AudioLevelsDockContent(self.audio_meter))" in main_window
