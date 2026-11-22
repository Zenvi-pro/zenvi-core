"""classes.hyperframes_titles: the HyperFrames compositions add_title_tool renders."""

import json
import re

import pytest

from classes import hyperframes_titles as ht


def _attr(html, name):
    return re.search(r'%s="([^"]*)"' % name, html).group(1)


@pytest.mark.parametrize("style", list(ht.STYLES))
def test_every_style_compiles_to_a_seekable_composition(style):
    s = ht.spec(style, title="Tokyo Day 1", subtitle="Shibuya", lines=["A", "B"] if style == "credits" else None)
    html = ht.compile_composition(s, 1920, 1080, 4.0, 30)
    assert 'data-composition-id="zenvi-title"' in html and 'window.__timelines["zenvi-title"]=main' in html
    assert _attr(html, "data-duration") == "4" and _attr(html, "data-width") == "1920"
    assert "gsap.timeline({paused:true})" in html and "main.set({},{},END)" in html   # runs the whole duration
    assert ("Tokyo" in html) != (style == "credits")


def test_text_is_escaped_and_the_spec_rides_along():
    s = ht.spec("headline", title='<script>alert("x")</script> & co', subtitle="a<b")
    html = ht.compile_composition(s, 1920, 1080, 3.0)
    assert "<script>alert" not in html and "&lt;script&gt;alert" in html and "a&lt;b" in html
    assert json.loads(_attr(html, "data-zenvi-title").replace("&quot;", '"').replace("&amp;", "&")
                      .replace("&lt;", "<").replace("&gt;", ">").replace("&#x27;", "'"))["subtitle"] == "a<b"


def test_every_bold_box_is_gone_by_the_end():
    s = ht.spec("bold_box", title="One\nTwo", subtitle="Three")
    exit_ = ht.compile_composition(s, 1920, 1080, 1.0).split('.to(".box"')[1].split(");")[0]
    assert "stagger" not in exit_   # each box would end 0.05 s later than the last


def test_spec_fields_follow_the_style():
    lt = ht.spec("lower_third", title="Jane Doe\nFounder, Acme")
    assert (lt["title"], lt["subtitle"]) == ("Jane Doe", "Founder, Acme")
    box = ht.spec("bold_box", title="I tried this\nfor 30 days")
    assert box["title"] == "I tried this\nfor 30 days" and box["subtitle"] == ""
    credits = ht.spec("credits", title="Director\nJane", subtitle="Music: Bob")
    assert credits["lines"] == ["Director", "Jane", "Music: Bob"] and credits["title"] == ""
    h = ht.spec("headline", lines=["One", "Two", "Three"])
    assert (h["title"], h["subtitle"], h["lines"]) == ("One", "Two Three", [])
    assert ht.spec("kicker", title="x")["zone"] == "top" and ht.spec("kicker", title="x", zone="bottom")["zone"] == "bottom"


@pytest.mark.parametrize("kwargs, needle", [
    ({"style": "comic_sans"}, "unknown title style"),
    ({"style": "headline"}, "needs text"),
    ({"style": "headline", "title": "x", "text_color": "red"}, "text_color"),
    ({"style": "headline", "title": "x", "font": "Bad;font{}"}, "font must be"),
    ({"style": "headline", "title": "x", "zone": "left"}, "screen_position"),
    ({"style": "headline", "title": "x", "font_scale": 9}, "font_scale"),
])
def test_bad_specs_are_refused(kwargs, needle):
    with pytest.raises(ht.TitleError, match=needle):
        ht.spec(**kwargs)


def test_only_a_solid_full_frame_card_is_opaque():
    assert ht.is_opaque(ht.spec("end_card", title="Thanks"))
    assert not ht.is_opaque(ht.spec("end_card", title="Thanks", background_color="#00000080"))
    assert not ht.is_opaque(ht.spec("headline", title="Hi"))
    assert ht.is_opaque(ht.spec("headline", background_color="#ffffff"))          # a blank colour card
    assert not ht.is_opaque(ht.spec("headline", title="Hi", background_color="#00000080"))


def test_vertical_frames_keep_text_out_of_the_platform_bands():
    s = ht.spec("lower_third", title="Jane")
    tall = ht.compile_composition(s, 1080, 1920, 3.0)
    wide = ht.compile_composition(s, 1920, 1080, 3.0)
    pad = lambda h: [float(v) for v in re.search(r"padding:([\d.]+)px ([\d.]+)px ([\d.]+)px", h).groups()]  # noqa: E731
    assert pad(tall)[2] / 1920 > 0.15 and pad(wide)[2] / 1080 < 0.1
    assert _attr(tall, "data-height") == "1920"


def test_long_credits_roll_and_title_spec_reads_back():
    rows = ["Line %d" % i for i in range(ht.ROLL_LINES + 2)]
    roll = ht.compile_composition(ht.spec("credits", lines=rows, zone="bottom"), 1920, 1080, 8.0)
    # in from under the frame, out over its top, wherever it rests
    assert "y:function(i,el){return H-el.offsetTop}" in roll and "return -(el.offsetTop+el.offsetHeight)" in roll
    assert '.fromTo(".roll"' not in ht.compile_composition(ht.spec("credits", lines=rows[:3]), 1920, 1080, 8.0)
    # few entries too big for the frame roll as well
    assert '.fromTo(".roll"' in ht.compile_composition(ht.spec("credits", lines=rows[:6], font_scale=5), 1080, 1920, 8.0)
    s = ht.spec("headline", title="x")
    assert ht.title_spec({"ai_metadata": {"source": ht.SOURCE, "title": s}}) == s
    assert ht.title_spec({"ai_metadata": {"source": "hyperframes_captions"}}) is None
    assert ht.title_spec({"path": "a.svg"}) is None


def test_a_short_title_plays_its_entrance_before_its_exit():
    s = ht.spec("lower_third", title="Jane", subtitle="Host")
    short = ht.compile_composition(s, 1920, 1080, 1.0)
    # The entrance is its own timeline, sped up to end before the exit starts at END-OUT.
    assert "intro.timeScale(Math.max(1,intro.duration()/(END-OUT)))" in short
    # no room to come in and go out: a still
    still = ht.compile_composition(s, 1920, 1080, 1 / 30.0)
    assert ".fromTo(" not in still and "Jane" in still
    assert "main.add(intro,0)" in short and 'window.__timelines["zenvi-title"]=main' in short
    rows = ["Line %d" % i for i in range(ht.ROLL_LINES + 2)]
    roll = ht.compile_composition(ht.spec("credits", lines=rows), 1920, 1080, 8.0)
    assert 'main.fromTo(".roll"' in roll                    # the roll runs on the main clock, not sped up
