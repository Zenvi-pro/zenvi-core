"""Which Zenvi titles become editable After Effects layers (classes.exporters.after_effects_titles)."""

import os

import pytest

from classes.exporters.after_effects_titles import NotNative, RectItem, TextItem, parse_title_svg

TITLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "titles")

# Templates whose visible content is only solid text and rectangles (checked one by one).
NATIVE = {"Box", "Footer_1", "Footer_2", "Footer_3", "Gray_Box_1", "Gray_Box_2", "Gray_Box_3", "Gray_Box_4",
          "Header_1", "Header_2", "Header_3", "Solid_Color", "Standard_1", "Standard_3", "Standard_4"}


def _read(name):
    with open(os.path.join(TITLES, name + ".svg"), encoding="utf-8") as fh:
        return fh.read()


def _templates():
    return sorted(os.path.splitext(n)[0] for n in os.listdir(TITLES) if n.endswith(".svg"))


@pytest.mark.parametrize("name", _templates())
def test_every_bundled_template_is_classified(name):
    if name in NATIVE:
        layout = parse_title_svg(_read(name))
        assert layout.items and (layout.width, layout.height) == (1920.0, 1080.0)
    else:
        with pytest.raises(NotNative) as err:
            parse_title_svg(_read(name))
        assert str(err.value)  # a reason for the export report


def test_box_layout_matches_the_svg():
    layout = parse_title_svg(_read("Box"))
    rects = [i for i in layout.items if isinstance(i, RectItem)]
    texts = [i for i in layout.items if isinstance(i, TextItem)]
    assert [(r.x, r.y, r.width, r.height) for r in rects] == [(0, 0, 1920, 1080), (360, 337, 1200, 406)]
    assert rects[0].fill == pytest.approx((0x50 / 255, 0x14 / 255, 0xB4 / 255))
    assert rects[1].fill is None and rects[1].stroke_width == 20
    assert [t.text for t in texts] == ["THE TITLE", "SUB-TITLE"]
    assert texts[0].anchor == "middle" and texts[0].bold and texts[0].family == "DejaVu Sans"
    assert (texts[0].x, texts[0].y, texts[0].size) == (964.10156, 500.0, 120.0)


def test_group_translations_are_composed_and_faint_outlines_are_noted():
    layout = parse_title_svg(_read("Standard_1"))
    title = layout.items[0]
    assert title.text == "The Title"
    # text x 722.81982 + group translate 239.99808 + layer translate 0.0019328
    assert title.x == pytest.approx(722.81982 + 239.99808 + 1.9328e-3)
    assert title.y == pytest.approx(532.01318 - 8.8790134e-3)
    assert title.stroke is None and any("faint outline" in n for n in layout.notes)


def test_a_solid_outline_stays_on_the_text():
    layout = parse_title_svg(_read("Gray_Box_1"))
    text = next(i for i in layout.items if isinstance(i, TextItem))
    assert text.stroke == pytest.approx((0x39 / 255,) * 3) and text.stroke_width == pytest.approx(3.75001788)
    box = next(i for i in layout.items if isinstance(i, RectItem))
    assert box.fill_opacity == pytest.approx(0.52511417) and box.rx > 40


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"{box}>{body}</svg>')


@pytest.mark.parametrize("body, reason", [
    ('<rect width="10" height="10" style="fill:url(#g)"/>', "gradient"),
    ('<text x="1" y="2" style="filter:url(#f)"><tspan>Hi</tspan></text>', "filter"),
    ('<path d="M0 0L10 10"/>', "path"),
    ('<g transform="rotate(10)"><text x="1" y="2">Hi</text></g>', "rotate"),
    ('<text x="1" y="2" transform="scale(1,-1)">Hi</text>', "mirrored"),
    ('<text x="1" y="2"><tspan dx="3">Hi</tspan></text>', "dx"),
    ('<image href="a.png" width="5" height="5"/>', "image"),
])
def test_features_native_layers_cannot_reproduce_keep_the_png(body, reason):
    with pytest.raises(NotNative) as err:
        parse_title_svg(SVG.format(box="", body=body))
    assert reason in str(err.value)


def test_hidden_and_blank_lines_are_skipped_and_viewbox_scales():
    svg = SVG.format(box=' viewBox="0 0 960 540"', body=(
        '<text x="100" y="200" style="font-size:40px;fill:#ff0000"><tspan>Hello</tspan></text>'
        '<text x="10" y="20" style="display:none"><tspan>Hidden</tspan></text>'
        '<text x="10" y="20"><tspan> </tspan></text>'))
    layout = parse_title_svg(svg)
    assert len(layout.items) == 1
    text = layout.items[0]
    assert (text.x, text.y, text.size) == (200.0, 400.0, 80.0)
    assert text.fill == (1.0, 0.0, 0.0)
