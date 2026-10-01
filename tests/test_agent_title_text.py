"""add_title_tool fills the visible title line, not just a template's mirrored reflection."""

from __future__ import annotations

import os
from xml.dom import minidom

import pytest

from classes.agent_tools.titles import _node_text, _set_svg_text

TITLES = os.path.join(os.path.dirname(__file__), "..", "src", "titles")


def _tspan_texts(doc):
    return [_node_text(n) for n in doc.getElementsByTagName("tspan")]


@pytest.mark.parametrize("name", ["Bar_1.svg", "Oval_4.svg", "Smoke_3.svg", "Standard_2.svg"])
def test_reflection_templates_get_the_text_on_both_copies(name):
    doc = minidom.parse(os.path.join(TITLES, name))
    _set_svg_text(doc, "HELLO")
    # The mirrored copy and the real line both carry the text; before the fix the
    # real (second) line was blanked, so only the upside-down reflection showed.
    assert _tspan_texts(doc) == ["HELLO", "HELLO"]


def test_other_placeholders_are_blanked():
    doc = minidom.parseString(
        '<svg><text><tspan>The Title</tspan></text>'
        '<text><tspan>Sub title</tspan></text></svg>')
    _set_svg_text(doc, "HELLO")
    assert _tspan_texts(doc) == ["HELLO", ""]


def test_title_plus_subtitle_template_keeps_one_line():
    doc = minidom.parse(os.path.join(TITLES, "Bubbles_2.svg"))
    _set_svg_text(doc, "HELLO")
    assert _tspan_texts(doc) == ["HELLO", ""]
