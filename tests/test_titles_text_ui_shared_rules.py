"""The Title Editor, Emojis dock and Animated Title dialog call the shared title rules.

The dialogs need a real Qt, so their refactored methods run here on stand-in
``self`` objects: what matters is that they now go through classes.title_svg /
emoji_catalog / blender_titles and still produce what the dialog used to.
"""

import json
import os
import re
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from classes import blender_titles, title_svg
from titles_text_fakes import tt  # noqa: F401  (fixture)


@pytest.fixture
def title_editor_cls(monkeypatch):
    """windows.title_editor.TitleEditor; classes.metrics reads settings at import, so stub it."""
    if "windows.title_editor" not in sys.modules:
        metrics = types.ModuleType("classes.metrics")
        metrics.track_metric_screen = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "classes.metrics", metrics)
    from windows.title_editor import TitleEditor
    return TitleEditor


def _editor_self(template="Standard_1"):
    doc = title_svg.load(title_svg.find_template(template))
    return SimpleNamespace(
        xmldoc=doc,
        text_nodes=doc.getElementsByTagName("text"),
        tspan_nodes=doc.getElementsByTagName("tspan"),
        rect_node=doc.getElementsByTagName("rect"),
        line_nodes=title_svg.line_nodes(doc),
        update_timer=MagicMock(),
        title_style_string="",
        bg_style_string="",
    )


class _LineEdit:
    def __init__(self, name, text):
        self._name, self._text = name, text

    def objectName(self):
        return self._name

    def text(self):
        return self._text


def test_title_editor_font_colour_background_and_lines(title_editor_cls, monkeypatch):
    import windows.title_editor as title_editor_module
    monkeypatch.setattr(title_editor_module, "QLineEdit", _LineEdit)
    TitleEditor = title_editor_cls

    me = _editor_self()
    me.qfont = SimpleNamespace(family=lambda: "Georgia", italic=lambda: True, bold=lambda: False)
    TitleEditor.set_font_attributes(me, 2.0)
    style = title_svg.node_style(me.tspan_nodes[0])
    assert style["font-family"] == "'Georgia'" and style["font-style"] == "italic"
    assert style["font-weight"] == "normal" and style["font-size"].startswith("262.5")
    assert me.title_style_string.startswith("font-style")

    TitleEditor.set_font_color_elements(me, "#00ff00", 0.4)
    assert title_svg.text_color(me.xmldoc) == ("#00ff00", 0.4)
    TitleEditor.set_bg_style(me, "#112233", 0.9)
    assert title_svg.background_color(me.xmldoc) == ("#112233", 0.9)

    boxes = [_LineEdit("txtFileName", "file"), _LineEdit("", "New title"), _LineEdit("", "New sub")]
    me.settingsContainer = MagicMock()
    me.settingsContainer.children.return_value = boxes
    TitleEditor.txtLine_changed(me, None)
    assert title_svg.line_texts(me.xmldoc) == ["New title", "New sub"]
    me.update_timer.start.assert_called_once()


def test_title_editor_writes_staged_files(tmp_path, title_editor_cls):
    TitleEditor = title_editor_cls
    me = _editor_self()
    me.filename = str(tmp_path / "Saved Title")
    TitleEditor.writeToFile(me, me.xmldoc)
    assert me.filename.endswith("Saved Title.svg") and os.listdir(tmp_path) == ["Saved Title.svg"]
    assert title_svg.line_texts(title_svg.load(me.filename)) == ["The Title", "Sub-Title"]


def test_emoji_dock_add_file_uses_the_shared_catalog(tt):
    from windows.views.emojis_listview import EmojisListView
    from classes import emoji_catalog
    fire = next(e for e in emoji_catalog.entries() if e.code == "1F525")
    first = EmojisListView.add_file(None, fire.path, "Fire")
    again = EmojisListView.add_file(None, fire.path, "Fire")
    assert first.id == again.id and tt.file(first.id)["name"] == "Fire"
    assert EmojisListView.add_file(None, "/nope/missing.svg") is None      # logged, not raised


def test_animated_title_dialog_params_and_script(tt, tmp_path):
    from windows.views.blender_listview import BlenderListView
    from classes import info
    settings = {"blender_gpu_enabled": False}
    me = SimpleNamespace(
        app=SimpleNamespace(project=tt.store, get_settings=lambda: SimpleNamespace(get=settings.get)),
        params={"file_name": "MyTitle", "title": "Hello"},
        unique_folder_name="F1",
    )
    me.get_project_params = lambda is_preview=True: BlenderListView.get_project_params(me, is_preview)
    params = BlenderListView.get_project_params(me, False)
    assert params["output_path"] == os.path.join(info.BLENDER_PATH, "F1", "MyTitle")
    assert params["resolution_percentage"] == 100 and params["resolution_x"] == 1920
    source = os.path.join(blender_titles.blender_dir(), "scripts", "fly_by_1.py.in")
    out = str(tmp_path / "fly_by_1.py")
    BlenderListView.inject_params(me, source, out, frame=10)
    body = open(out).read()
    injected = json.loads(re.search(r'params_json = r"""(.*?)"""', body, re.S).group(1))
    assert injected["title"] == "Hello" and injected["resolution_percentage"] == 50
