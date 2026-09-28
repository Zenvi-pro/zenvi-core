"""Effect icons ported from OpenShot 4.0 (Shadow, Glow, Displacement Map, Denoise).

The Effects dock resolves an icon as ``src/effects/icons/<class_name lowercased,
spaces removed>.png`` (see ``EffectsModel.update_model``). libopenshot 1.0 exposes
these four new video effects, so each needs a 1x and @2x icon or the panel shows
a blank thumbnail.
"""

import os

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
ICONS_DIR = os.path.join(SRC, "effects", "icons")

# libopenshot 1.0 ``EffectInfo`` class names -> icon basename used by EffectsModel
NEW_EFFECTS = {
    "Displace": "displace",
    "Glow": "glow",
    "Shadow": "shadow",
    "DenoiseImage": "denoiseimage",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _icon_name(class_name):
    # Mirrors EffectsModel.update_model's icon_name rule.
    return class_name.lower().replace(" ", "")


@pytest.mark.parametrize("class_name,basename", sorted(NEW_EFFECTS.items()))
def test_effect_icon_name_rule(class_name, basename):
    assert _icon_name(class_name) == basename


@pytest.mark.parametrize("basename", sorted(NEW_EFFECTS.values()))
@pytest.mark.parametrize("suffix", ["", "@2x"])
def test_effect_icon_present_and_valid_png(basename, suffix):
    path = os.path.join(ICONS_DIR, "%s%s.png" % (basename, suffix))
    assert os.path.isfile(path), "missing effect icon %s" % path
    with open(path, "rb") as fh:
        assert fh.read(len(PNG_MAGIC)) == PNG_MAGIC, "%s is not a PNG" % path
