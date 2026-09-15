"""Agent-supplied time arguments must never surface as `could not convert
string to float`. Either they parse, or they raise a ValueError naming the
value so the handler can answer with a usable Error string."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

from classes.clip_placement import parse_seconds_arg, parse_timecode_token


@pytest.mark.parametrize(
    "value,expected",
    [
        (12, 12.0),
        (12.34, 12.34),
        ("12", 12.0),
        ("12.34", 12.34),
        ("  12.5  ", 12.5),
        ("0", 0.0),
        ("-3", -3.0),
        ("12s", 12.0),
        ("12 s", 12.0),
        ("12sec", 12.0),
        ("12 secs", 12.0),
        ("12 seconds", 12.0),
        ("1,200", 1200.0),
        ("0:12", 12.0),
        ("1:02.5", 62.5),
        ("1:02:03", 3723.0),
        ("00:00:30", 30.0),
    ],
)
def test_values_that_parse(value, expected):
    assert parse_seconds_arg(value, field="position_seconds") == pytest.approx(expected)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_means_not_supplied(blank):
    assert parse_seconds_arg(blank) is None
    assert parse_seconds_arg(blank, default=0.0) == 0.0


@pytest.mark.parametrize(
    "value",
    [
        "soon",
        "end of last clip",
        "start",
        "auto",
        "F0A1B2C3D4",          # a file id
        "12:34:56:78",         # too many timecode parts
        "1:xx",
        "abc",
        True,                  # a bool is not a time
    ],
)
def test_values_that_raise(value):
    with pytest.raises(ValueError) as excinfo:
        parse_seconds_arg(value, field="position_seconds")
    message = str(excinfo.value)
    assert "position_seconds" in message
    assert repr(value) in message


def test_error_message_suggests_the_accepted_forms():
    with pytest.raises(ValueError) as excinfo:
        parse_seconds_arg("whenever", field="start_seconds")
    assert "timecode" in str(excinfo.value)


def test_field_name_is_optional():
    with pytest.raises(ValueError) as excinfo:
        parse_seconds_arg("nope")
    assert "value=" in str(excinfo.value)


def test_timecode_token_returns_none_instead_of_raising():
    assert parse_timecode_token("1:30") == 90.0
    assert parse_timecode_token("nope") is None
    assert parse_timecode_token("") is None
    assert parse_timecode_token(None) is None
