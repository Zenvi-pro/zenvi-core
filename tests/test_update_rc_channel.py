"""RC update channel: ZENVI_UPDATE_CHANNEL=rc also sees the releases-test prerelease."""

import os
import sys
import types

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.modules.setdefault("openshot", types.ModuleType("openshot"))

from classes import auto_updater as au  # noqa: E402


def _rel(tag, *assets, draft=False):
    return {"tag_name": tag, "draft": draft, "assets": [{"name": a} for a in assets]}


def test_version_comes_from_asset_name_when_tag_is_not_a_version():
    rc = _rel("releases-test", "Zenvi-v1.2.1-arm64.dmg")
    assert au.release_version(rc) == "1.2.1"
    assert au.release_version(_rel("v1.2.0")) == "1.2.0"


def test_rc_wins_only_when_newer_than_stable():
    stable = _rel("v1.2.0")
    rc = _rel("releases-test", "Zenvi-v1.2.1-arm64.dmg")
    assert au.pick_release(stable, [rc])[1] == "1.2.1"
    same = _rel("releases-test", "Zenvi-v1.2.0-arm64.dmg")
    assert au.pick_release(stable, [same])[0] is stable


def test_drafts_and_other_tags_are_ignored():
    stable = _rel("v1.2.0")
    draft = _rel("releases-test", "Zenvi-v1.2.1-arm64.dmg", draft=True)
    other = _rel("v9.9.9", "Zenvi-v9.9.9-arm64.dmg")
    assert au.pick_release(stable, [draft, other])[0] is stable


def test_rc_found_without_a_stable_release():
    rc = _rel("releases-test", "Zenvi-v1.2.1-arm64.dmg")
    assert au.pick_release(None, [rc])[1] == "1.2.1"


def test_channel_is_opt_in(monkeypatch):
    monkeypatch.delenv(au.UPDATE_CHANNEL_ENV, raising=False)
    assert au.update_channel() == "stable"
    monkeypatch.setenv(au.UPDATE_CHANNEL_ENV, "RC")
    assert au.update_channel() == "rc"
