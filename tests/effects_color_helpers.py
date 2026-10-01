"""Shared setup for the effects-color editor-tool tests: libopenshot's catalog and effect JSON from fixtures."""

import copy
import json
import os

from classes import effect_ops
from classes.editor_tools import effects_color as ec

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "editor_tools")


def install_effect_fixtures(editor, monkeypatch):
    """The editor harness with libopenshot's effect catalog and effect JSON taken from fixtures."""
    with open(os.path.join(_FIXTURES, "effect_catalog.json"), encoding="utf-8") as fh:
        cat = json.load(fh)
    effects = editor._fixtures["effects"]
    raw = {cn: {"info": v["info"], "properties": v["properties"], "defaults": effects.get(cn, {})}
           for cn, v in cat.items()}
    monkeypatch.setattr(ec, "_load_raw_catalog", lambda: raw)
    monkeypatch.setattr(ec, "_CATALOG", None)

    def create(class_name):
        if class_name not in effects:
            raise RuntimeError("no such effect")
        e = copy.deepcopy(effects[class_name])
        e["id"] = editor.store.generate_id()
        return e

    monkeypatch.setattr(effect_ops, "create_effect_json", create)
    return editor


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


def effect_of(editor, clip_id, class_name):
    return [e for e in editor.clip(clip_id)["effects"] if e["class_name"] == class_name]


def y(kf):
    return [p["co"]["Y"] for p in kf["Points"]]


def x(kf):
    return [p["co"]["X"] for p in kf["Points"]]
