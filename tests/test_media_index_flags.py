"""Media index v2 is how indexing works; only people recognition is a preference."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import flags  # noqa: E402


def settings_file():
    return json.loads((SRC / "settings" / "_default.settings").read_text(encoding="utf-8"))


def test_there_is_no_media_index_v2_preference_but_people_recognition_stays_opt_in():
    names = {s["setting"]: s for s in settings_file()}
    assert "media-index-v2" not in names
    assert names["media-index-people"]["value"] is False


def test_v2_is_on_whenever_the_app_has_settings_and_off_without_an_app(monkeypatch):
    import classes.app as app

    monkeypatch.setattr(app, "get_settings", lambda: SimpleNamespace(get=lambda key: False))
    assert flags.v2_enabled() is True, "no setting can turn it off"
    monkeypatch.setattr(app, "get_settings", lambda: None)
    assert flags.v2_enabled() is False
    monkeypatch.setattr(app, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError("no app")))
    assert flags.v2_enabled() is False


def test_people_recognition_still_needs_its_own_preference(monkeypatch):
    import classes.app as app

    values = {"media-index-people": False}
    monkeypatch.setattr(app, "get_settings", lambda: SimpleNamespace(get=lambda key: values.get(key)))
    assert flags.people_enabled() is False
    values["media-index-people"] = True
    assert flags.people_enabled() is True
