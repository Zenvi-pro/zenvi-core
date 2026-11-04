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


def _untranslated_reads(class_name):
    """SCOPE_EXPLANATIONS reads in the class that are not the argument of a _tr(...) call."""
    tree = ast.parse(Path(scope_panel.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    translated = {id(call.args[0]) for call in ast.walk(cls)
                  if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_tr" and call.args}
    return [n for n in ast.walk(cls) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
            and n.value.id == "SCOPE_EXPLANATIONS" and id(n) not in translated]


@pytest.mark.parametrize("class_name, _key", SHOWN_BY)
def test_scope_explanations_are_shown_translated(class_name, _key):
    assert _untranslated_reads(class_name) == []


def test_translation_falls_back_to_the_english_text_without_an_app(monkeypatch):
    import classes.app
    monkeypatch.setattr(classes.app, "get_app", lambda: None)
    assert scope_panel._tr("Brightness") == "Brightness"
