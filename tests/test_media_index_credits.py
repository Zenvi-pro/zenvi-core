"""The About dialog credits the data and models whose licences ask for it."""

from __future__ import annotations

from classes.media_index import credits, gazetteer, people_models


def test_the_credits_name_the_place_data_and_the_voice_model_exactly_as_the_tools_report_them():
    lines = credits.third_party_credits()
    assert gazetteer.ATTRIBUTION in lines
    assert people_models.MODELS["voice"]["attribution"] in lines
    assert all("CC BY 4.0" in line for line in lines if "GeoNames" in line or "WeSpeaker" in line)


def test_the_html_is_escaped_and_carries_every_line():
    out = credits.credits_html()
    assert "GeoNames" in out and "WeSpeaker" in out and "<script" not in out
