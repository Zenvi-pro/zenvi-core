"""Outlined titles (zenvi-web typography): the desktop edits the text, not the stale outlines."""

import re

from classes import title_svg
from titles_text_fakes import receipt, tt  # noqa: F401  (fixture)

# Shaped as packages/engine/src/typography/outline.ts writes it: the <text> stays,
# hidden (display="none", data-zenvi-outlined="1"), and a <g data-zenvi-outline="family">
# of <path>s follows it, carrying the text's id (+ "-outline"), transform, opacity and filter.
OUTLINED = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <defs><filter id="glow"><feGaussianBlur stdDeviation="4"/></filter></defs>
  <rect id="bg" width="1920" height="1080" style="fill:#000000;fill-opacity:0"/>
  <text id="title" x="960" y="540" transform="translate(0,0)" style="font-family:Inter;font-size:120px;font-weight:700;text-anchor:middle;fill:#ffffff;filter:url(#glow)" display="none" data-zenvi-outlined="1"><tspan x="960" y="540">The Title</tspan></text><g data-zenvi-outline="inter" id="title-outline" transform="translate(0,0)" filter="url(#glow)"><path d="M 100 100 L 200 100 L 200 200 Z" fill="#ffffff"/></g>
  <text id="sub" x="960" y="700" style="font-family:Inter;font-size:60px;fill:#ffffff" display="none" data-zenvi-outlined="1"><tspan x="960" y="700">Sub-Title</tspan></text><g data-zenvi-outline="inter" id="sub-outline"><path d="M 10 10 L 20 10 L 20 20 Z" fill="#ffffff"/></g>
</svg>
"""


def _write(tmp_path, text=OUTLINED):
    path = tmp_path / "Outlined.svg"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _outline_groups(xmldoc):
    return [g for g in xmldoc.getElementsByTagName("g") if g.hasAttribute(title_svg.OUTLINE_ATTR)]


def test_load_drops_outlines_and_shows_the_real_text(tmp_path):
    doc = title_svg.load(_write(tmp_path))
    assert _outline_groups(doc) == []
    assert doc.getElementsByTagName("path") == []
    texts = title_svg.text_nodes(doc)
    assert [t.getAttribute("id") for t in texts] == ["title", "sub"]
    for t in texts:
        assert not t.hasAttribute("display") and not t.hasAttribute(title_svg.OUTLINED_ATTR)
    assert title_svg.line_texts(doc) == ["The Title", "Sub-Title"]
    # untouched: the <text>'s own styling and the shared defs survive
    assert "filter:url(#glow)" in texts[0].getAttribute("style")
    assert doc.getElementsByTagName("filter")[0].getAttribute("id") == "glow"


def test_edit_and_save_writes_the_new_words_visibly_with_no_stale_paths(tmp_path):
    path = _write(tmp_path)
    doc = title_svg.load(path)
    title_svg.set_line_texts(doc, ["Day 2"])
    title_svg.write(doc, path)
    saved = open(path, encoding="utf-8").read()
    assert "data-zenvi-outline" not in saved and "data-zenvi-outlined" not in saved
    assert "<path" not in saved and 'display="none"' not in saved
    assert re.search(r">Day 2</tspan>", saved)
    assert title_svg.line_texts(title_svg.parse_string(saved)) == ["Day 2", "Sub-Title"]


def test_parse_string_strips_too_and_plain_titles_are_untouched(tmp_path):
    assert _outline_groups(title_svg.parse_string(OUTLINED)) == []
    plain = title_svg.find_template("Standard_1")
    before = open(plain, encoding="utf-8").read()
    doc = title_svg.load(plain)
    assert title_svg.strip_outlines(doc) == 0
    assert title_svg.to_xml(doc) == title_svg.to_xml(title_svg.parse_string(before))


def test_a_text_the_engine_did_not_hide_keeps_its_own_display(tmp_path):
    # display="none" only goes when the engine marked the text as outlined
    svg = OUTLINED.replace('<text id="sub" x="960" y="700" style="font-family:Inter;font-size:60px;fill:#ffffff" display="none" data-zenvi-outlined="1">',
                           '<text id="sub" x="960" y="700" style="font-family:Inter;font-size:60px;fill:#ffffff" display="none">')
    doc = title_svg.parse_string(svg)
    sub = [t for t in title_svg.text_nodes(doc) if t.getAttribute("id") == "sub"][0]
    assert sub.getAttribute("display") == "none"


def test_strip_counts_groups_and_survives_nesting():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg"><g data-zenvi-outline="a"><g data-zenvi-outline="b"><path d="M0 0"/></g></g>'
           '<text data-zenvi-outlined="1" display="none">x</text></svg>')
    import xml.dom.minidom as minidom
    doc = minidom.parseString(svg)
    assert title_svg.strip_outlines(doc) == 1
    assert doc.getElementsByTagName("g") == []
    assert not doc.getElementsByTagName("text")[0].hasAttribute("display")


def test_edit_title_tool_on_an_outlined_title_leaves_visible_text(tt, tmp_path):
    first = receipt(tt.call("add_title_tool", text="Base", template="Standard_1", subtitle="Role"))
    outlined = _write(tmp_path)
    rec = receipt(tt.call("add_title_tool", template=outlined, text="Hello", position_seconds=6))
    edit = receipt(tt.call("edit_title_tool", timeline_clip_id=rec["timeline_clip_id"], text="Bye"))
    saved = open(edit["path"], encoding="utf-8").read()
    assert first["path"] != edit["path"]
    assert "data-zenvi-outline" not in saved and "<path" not in saved and 'display="none"' not in saved
    assert title_svg.line_texts(title_svg.parse_string(saved))[0] == "Bye"
    assert "Bye" in saved and "Hello" not in saved
