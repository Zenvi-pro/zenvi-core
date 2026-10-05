"""classes.handoff.ui_registry and plugins: how the handoff packages plug into the menus."""

import importlib.abc
import importlib.machinery
import sys
import types

import pytest

from classes.handoff import plugins, ui_registry as reg


@pytest.fixture
def clean_registry(monkeypatch):
    saved = {k: dict(v) for k, v in reg._actions.items()}
    for actions in reg._actions.values():  # handoff packages imported at collection (C2-C5) register real entries
        actions.clear()
    yield reg
    for k, v in saved.items():
        reg._actions[k].clear()
        reg._actions[k].update(v)


def test_actions_sort_by_order_then_id_and_replace_by_id(clean_registry):
    calls = []
    reg.register_export_action("zz", "Z export", calls.append, order=50)
    reg.register_export_action("aa", "A export", calls.append, order=50)
    reg.register_export_action("first", lambda: "Translated", calls.append, order=1)
    assert [a.id for a in reg.export_actions()][:3] == ["first", "aa", "zz"]
    assert reg.export_actions()[0].text() == "Translated"
    assert reg.export_actions()[1].text(lambda s: s.upper()) == "A EXPORT"
    reg.register_export_action("aa", "A again", calls.append, order=60)
    assert [a.id for a in reg.export_actions()][:3] == ["first", "zz", "aa"]
    reg.unregister_action("aa")
    assert "aa" not in [a.id for a in reg.export_actions()]


def test_send_actions_name_a_host(clean_registry):
    reg.register_send_action("ae", "After Effects", "aftereffects", lambda w: None)
    assert reg.send_actions()[0].host_app == "aftereffects"
    with pytest.raises(ValueError):
        reg.register_send_action("x", "X", "photoshop", lambda w: None)
    with pytest.raises(ValueError):
        reg.register_import_action("y", "Y", None)  # type: ignore[arg-type]


def test_listeners_hear_registrations(clean_registry):
    heard = []
    reg.add_listener(lambda: heard.append(1))
    try:
        reg.register_import_action("imp", "Import", lambda w: None)
    finally:
        reg._listeners.clear()
    assert heard == [1]


class _Broken(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "classes.handoff.remotion":
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise RuntimeError("remotion plugin is broken")


def test_missing_plugins_are_skipped_and_a_broken_one_is_logged(monkeypatch, caplog):
    from classes.handoff import linked_media
    monkeypatch.setattr(linked_media, "_PROVIDERS", dict(linked_media._PROVIDERS))
    monkeypatch.setattr(plugins, "_done", False)
    monkeypatch.setattr(plugins, "_loaded", [])
    good = types.ModuleType("classes.handoff.hyperframes")
    monkeypatch.setitem(sys.modules, "classes.handoff.hyperframes", good)
    # the real Remotion package (C4) is imported by its own tests at collection; hide it so the
    # broken finder below is consulted (monkeypatch puts it back afterwards)
    monkeypatch.delitem(sys.modules, "classes.handoff.remotion", raising=False)
    finder = _Broken()
    sys.meta_path.insert(0, finder)
    try:
        loaded = plugins.load_plugins()
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop("classes.handoff.remotion", None)
    assert loaded == ["classes.handoff.aftereffects_link", "classes.handoff.hyperframes"]
    assert "remotion failed to load" in caplog.text
    assert plugins.load_plugins() == loaded  # once per session


def test_unregister_notifies_listeners(clean_registry):
    heard = []
    reg.register_import_action("gone", "Gone", lambda w: None)
    reg.add_listener(lambda: heard.append(1))
    try:
        reg.unregister_action("gone")
        reg.unregister_action("never-registered")
    finally:
        reg._listeners.clear()
    assert heard == [1]
