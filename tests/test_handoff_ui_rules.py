"""Pure rules behind the linked-clip UI (headless): which editor a prop gets, JSON parsing, captions."""

import pytest

from windows import linked_clip_dialog as dlg


@pytest.mark.parametrize("value, kind", [
    ("Hello", "text"), ("#FF5A36", "color"), ("#ff5a3680", "color"), ("#FFF", "text"), (12, "int"),
    (3_000_000_000, "float"), (0.5, "float"), (True, "bool"), (None, "text"), ([1, 2], "json"),
    ({"a": 1}, "json"),
])
def test_editor_kind(value, kind):
    assert dlg.editor_kind(value) == kind


def test_props_json_must_be_an_object():
    assert dlg.parse_props_json('{"title": "Hi", "n": 2}') == {"title": "Hi", "n": 2}
    assert dlg.parse_props_json("") == {}
    with pytest.raises(ValueError, match="JSON object"):
        dlg.parse_props_json("[1, 2]")
    with pytest.raises(ValueError, match="not valid JSON"):
        dlg.parse_props_json("{title: Hi}")
    with pytest.raises(ValueError, match="items"):
        dlg.parse_json_field("[1,", "items")


def test_state_and_source_captions():
    assert dlg.state_text("fresh") == "Up to date"
    assert dlg.state_text("stale", "the source changed") == "Source changed — re-render to update: the source changed"
    assert dlg.state_text(None) == "Not checked yet"
    link = {"source": {"composition": "Intro", "project_dir": "/p", "file": "src/Intro.tsx", "line": 12}}
    assert dlg.describe_source(link) == "Intro — /p — src/Intro.tsx:12"
    assert dlg.describe_source({"source": {"aep": "/x/promo.aep", "composition": "Promo"}}) == "Promo — /x/promo.aep"
