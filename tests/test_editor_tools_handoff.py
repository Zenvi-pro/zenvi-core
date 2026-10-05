"""The shared handoff editor tools, called through execute_tool like the chat does (contract-3 receipts)."""

import os

import pytest

from classes.handoff import linked_media as lm
from handoff_fakes import FakeHost, FakeProvider, linked, remotion_link, tt, write_discovery  # noqa: F401


@pytest.fixture
def provider(linked):
    p = FakeProvider(probe=linked.probe)
    lm.register_provider(p)
    return p


@pytest.fixture
def ae_host(linked):
    host = FakeHost()
    write_discovery(linked.user_path, host)
    yield host
    host.stop()


def _failed(r):
    """A refused call: ToolError comes back as status error, schema problems as refused; both say Error."""
    return r["status"] in ("error", "refused") and r["summary"].startswith("Error") and r["undoSteps"] == 0


def _import(linked, provider, **link_over):
    out = lm.import_linked(dict(remotion_link(), **link_over), position=0.0)
    linked.mark()
    return out


# --- Adobe hosts -------------------------------------------------------------------------------

def test_list_link_hosts_reports_connection_and_how_to_connect(linked, ae_host):
    r = linked.call_receipt("list_link_hosts_tool")
    assert r["status"] == "applied" and r["undoSteps"] == 0
    ae, pr = r["data"]["hosts"]
    assert ae["connected"] and ae["active"] and r["data"]["active"] == "after-effects"
    assert not pr["connected"] and "Zenvi Link" in pr["how_to_connect"]


def test_call_link_host_passes_through_receipt_and_images(linked, ae_host):
    from classes import tool_handlers
    out = tool_handlers.execute_tool_rich("call_link_host_tool", {
        "host": "aftereffects", "tool": "ae_capture_frame", "arguments": {"time": 2.0}})
    r = out.receipt
    assert r.status == "applied" and r.undoSteps == 0 and r.summary.startswith("After Effects:")
    assert r.data["host_status"] == "ok" and r.data["receipt"]["data"] == {"args": {"time": 2.0}}
    assert len(out.images) == 1 and out.images[0].mime_type == "image/png"
    assert ae_host.calls[-1]["params"] == {"name": "ae_capture_frame", "arguments": {"time": 2.0}}


def test_call_link_host_maps_host_refusals_and_bad_requests(linked, ae_host):
    r = linked.call_receipt("call_link_host_tool", host="aftereffects", tool="ae_fail", arguments={})
    assert r["status"] == "refused" and r["summary"].startswith("Error") and r["data"]["host_status"] == "refused"
    r = linked.call_receipt("call_link_host_tool", host="aftereffects", tool="premiere_get_state")
    assert _failed(r) and "host='premiere'" in r["summary"]
    r = linked.call_receipt("call_link_host_tool", host="premiere", tool="premiere_get_state")
    assert _failed(r) and "not connected" in r["summary"] and "Zenvi Link" in r["summary"]
    r = linked.call_receipt("call_link_host_tool", host="photoshop", tool="ps_x")
    assert r["status"] == "refused" and "must be one of" in r["summary"]
    assert linked.undo_steps_since_mark() == 0


# --- Linked clips ------------------------------------------------------------------------------

def test_import_linked_media_copies_into_the_links_folder_as_one_step(linked):
    src = linked.media("Promo.mov", seconds=3.0)
    r = linked.call_receipt("import_linked_media_tool", path=src, position=1.0,
                            link={"kind": "aftereffects", "source": {"composition": "Promo", "composition_key": 12}})
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    data = r["data"]
    assert data["path"] == os.path.join(linked.user_path, "links", "aftereffects", "Promo.mov")
    assert os.path.isfile(src) and os.path.isfile(data["path"])  # copied, the original stays
    f = linked.file(data["file_id"])
    assert f["zenvi_link"]["source"]["composition_key"] == 12 and f["name"] == "Promo"
    linked.undo()
    assert linked.file(data["file_id"]) is None


def test_import_linked_media_refuses_bad_links_without_history(linked):
    src = linked.media("x.mov")
    r = linked.call_receipt("import_linked_media_tool", path=src, link={"kind": "remotion", "source": {"path": "/x"}})
    assert _failed(r) and "must not use the key 'path'" in r["summary"]
    r = linked.call_receipt("import_linked_media_tool", path="/nope.mov", link={"kind": "remotion"})
    assert _failed(r) and "no media file" in r["summary"]
    assert linked.undo_steps_since_mark() == 0 and not linked.get("files")


def test_get_linked_clip_reports_state_props_and_clips(linked, provider):
    out = _import(linked, provider)
    r = linked.call_receipt("get_linked_clip_tool", clip_id=out["timeline_clip_id"])
    d = r["data"]
    assert r["status"] == "applied" and r["undoSteps"] == 0
    assert d["state"] == "fresh" and d["kind"] == "remotion" and d["props"] == {"title": "Hello"}
    assert d["editable_props"] == {"title": "Hello"} and d["can_open_studio"] and d["provider_available"]
    assert [c["timeline_clip_id"] for c in d["clips"]] == [out["timeline_clip_id"]]
    provider.version = 2
    r = linked.call_receipt("get_linked_clip_tool", file_id=out["file_id"])
    assert r["data"]["state"] == "stale" and "Error" not in r["summary"]
    r = linked.call_receipt("get_linked_clip_tool")
    assert _failed(r) and "clip_id" in r["summary"]


def test_update_linked_clip_rerenders_with_merged_props_in_one_step(linked, provider):
    out = _import(linked, provider)
    r = linked.call_receipt("update_linked_clip_tool", clip_id=out["timeline_clip_id"], props={"accent": "#FF5A36"})
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    assert provider.renders[-1]["props"] == {"title": "Hello", "accent": "#FF5A36"}
    assert linked.file(out["file_id"])["zenvi_link"]["props"] == {"title": "Hello", "accent": "#FF5A36"}
    linked.undo()
    assert linked.file(out["file_id"])["zenvi_link"]["props"] == {"title": "Hello"}


def test_update_without_rerender_stores_props_and_goes_stale(linked, provider):
    out = _import(linked, provider)
    r = linked.call_receipt("update_linked_clip_tool", file_id=out["file_id"], props={"title": "Bye"}, rerender=False)
    assert r["status"] == "applied" and r["undoSteps"] == 1 and r["data"]["state"] == "stale"
    assert len(provider.renders) == 1
    assert lm.link_state(linked.file(out["file_id"])) == "stale"
    linked.mark()
    r = linked.call_receipt("update_linked_clip_tool", file_id=out["file_id"], props={"title": "Bye"}, rerender=False)
    assert r["status"] == "unchanged" and linked.undo_steps_since_mark() == 0


def test_failed_rerender_is_an_error_and_changes_nothing(linked, provider):
    out = _import(linked, provider)
    provider.fail = RuntimeError("Composition 'Intro' not found")
    r = linked.call_receipt("rerender_linked_clip_tool", file_id=out["file_id"])
    assert r["status"] == "error" and "not found" in r["summary"] and linked.undo_steps_since_mark() == 0
    assert linked.call_receipt("get_linked_clip_tool", file_id=out["file_id"])["data"]["state"] == "error"
    provider.fail = None
    r = linked.call_receipt("rerender_linked_clip_tool", file_id=out["file_id"])
    assert r["status"] == "applied" and r["undoSteps"] == 1


def test_rerender_without_a_provider_is_refused(linked, provider):
    out = _import(linked, provider)
    lm.unregister_provider("remotion")
    r = linked.call_receipt("rerender_linked_clip_tool", file_id=out["file_id"])
    assert _failed(r) and "cannot render" in r["summary"]


def test_open_linked_source_uses_the_provider_or_the_recorded_file(linked, provider, tmp_path, monkeypatch):
    out = _import(linked, provider)
    r = linked.call_receipt("open_linked_source_tool", file_id=out["file_id"], target="studio")
    assert r["status"] == "applied" and provider.opened[-1][0] == "studio"
    linked.call_receipt("open_linked_source_tool", file_id=out["file_id"])
    assert provider.opened[-1][0] == "code"

    lm.unregister_provider("remotion")
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Intro.tsx").write_text("export {}")
    lm.update_link(out["file_id"], {"source": {"project_dir": str(project)}})
    seen = {}
    from classes.handoff import open_source
    monkeypatch.setattr(open_source, "open_in_editor",
                        lambda file, line=None, **kw: seen.update(file=file, line=line) or
                        {"editor": "Cursor", "command": [], "file": file, "line": line})
    r = linked.call_receipt("open_linked_source_tool", file_id=out["file_id"])
    assert r["status"] == "applied" and seen == {"file": str(project / "src" / "Intro.tsx"), "line": 12}
    r = linked.call_receipt("open_linked_source_tool", file_id=out["file_id"], target="studio")
    assert _failed(r) and "no studio" in r["summary"]


def test_unlink_clip_is_one_step_and_refuses_plain_files(linked, provider):
    out = _import(linked, provider)
    r = linked.call_receipt("unlink_clip_tool", clip_id=out["timeline_clip_id"])
    assert r["status"] == "applied" and r["undoSteps"] == 1
    assert "zenvi_link" not in linked.file(out["file_id"])
    linked.undo()
    assert linked.file(out["file_id"])["zenvi_link"]["kind"] == "remotion"
    plain = linked.add_file("video")
    r = linked.call_receipt("unlink_clip_tool", file_id=plain)
    assert _failed(r) and "not a linked clip" in r["summary"]


def test_every_handoff_package_tool_module_present_imports_cleanly():
    """The editor tools skip a broken package module (so the others keep working); this fails loudly instead."""
    import importlib
    import importlib.util
    from classes.editor_tools import handoff
    for name in handoff.PACKAGE_TOOL_MODULES:
        full = "classes.editor_tools." + name
        if importlib.util.find_spec(full) is not None:
            importlib.import_module(full)
    assert handoff.package_tool_errors() == {}


def test_a_broken_package_tool_module_is_logged_and_skipped(monkeypatch, caplog):
    import importlib.abc
    import importlib.machinery
    import sys
    from classes.editor_tools import handoff

    class _Broken(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "classes.editor_tools.handoff_remotion":
                return importlib.machinery.ModuleSpec(fullname, self)
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            raise SyntaxError("broken package")

    monkeypatch.setattr(handoff, "_package_tool_errors", {})
    # the real module (C4) is imported at collection; hide it so the broken finder is consulted
    monkeypatch.delitem(sys.modules, "classes.editor_tools.handoff_remotion", raising=False)
    finder = _Broken()
    sys.meta_path.insert(0, finder)
    try:
        handoff._load_package_tools()
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop("classes.editor_tools.handoff_remotion", None)
    assert "handoff_remotion" in handoff.package_tool_errors()
    assert "failed to load" in caplog.text


def test_after_effects_provider_renders_through_zenvi_link(linked, tmp_path):
    """The AE provider maps comp / output folder onto the host tool's own argument names."""
    import json as _json
    from classes.handoff.aftereffects_link import AfterEffectsProvider, _schema_cache
    host = FakeHost()
    _schema_cache.clear()
    try:
        host.tools = [{"name": "ae_render_for_zenvi", "inputSchema": {"type": "object", "properties": {
            "comp": {}, "output_dir": {}, "project": {}}}}]
        aep = tmp_path / "promo.aep"
        aep.write_bytes(b"aep")

        def render(name, args):
            out = os.path.join(args["output_dir"], "Promo.mov")
            with open(out, "wb") as fh:
                fh.write(b"prores")
            linked.probe.durations[out] = 2.0
            receipt = {"contract": 3, "status": "applied", "tool": name, "host": "aftereffects", "summary": "ok",
                       "data": {"path": out, "comp_id": 12, "comp_name": "Promo", "project": str(aep),
                                "width": 1920, "height": 1080, "fps": 30, "duration": 2.0, "codec": "prores4444"}}
            return {"content": [{"type": "text", "text": _json.dumps(receipt)}], "structuredContent": receipt,
                    "isError": False}

        host.call = render
        write_discovery(linked.user_path, host)
        lm.register_provider(AfterEffectsProvider())
        link = {"kind": "aftereffects", "source": {"aep": str(aep), "composition": "Promo"}}
        out = lm.import_linked(link, position=0.0)
        f = linked.file(out["file_id"])
        stored = lm.read_link(f)
        assert stored["source"]["composition_key"] == 12 and stored["render"]["codec"] == "prores4444"
        assert stored["render"]["duration_frames"] == 60 and f["path"].endswith(".mov")
        call = [c for c in host.calls if c.get("method") == "tools/call"][-1]["params"]["arguments"]
        assert call["comp"] == "Promo" and call["project"] == str(aep) and os.path.basename(call["output_dir"])
        assert lm.link_state(f) == "fresh"
        aep.write_bytes(b"aep saved again")
        assert lm.link_state(f) == "stale"
    finally:
        host.stop()


def test_import_linked_media_leaves_the_callers_file_alone_when_refused(linked):
    import tempfile
    fd, src = tempfile.mkstemp(suffix=".mov")
    os.write(fd, b"movie")
    os.close(fd)
    linked.probe.durations[src] = 3.0
    try:
        r = linked.call_receipt("import_linked_media_tool", path=src, track="7",
                                link={"kind": "aftereffects", "source": {"composition": "Promo"}})
        assert _failed(r) and os.path.isfile(src)  # not moved out from under the caller's retry
        assert not os.path.exists(os.path.join(linked.user_path, "links", "aftereffects", os.path.basename(src)))
    finally:
        if os.path.exists(src):
            os.unlink(src)


def test_import_linked_media_fingerprints_so_the_clip_can_go_stale(linked, tmp_path):
    from classes.handoff.aftereffects_link import AfterEffectsProvider
    lm.register_provider(AfterEffectsProvider())
    aep = tmp_path / "promo.aep"
    aep.write_bytes(b"v1")
    src = linked.media("Promo.mov", seconds=2.0)
    r = linked.call_receipt("import_linked_media_tool", path=src,
                            link={"kind": "aftereffects", "source": {"aep": str(aep), "composition": "Promo"}})
    f = linked.file(r["data"]["file_id"])
    assert lm.read_link(f)["render"]["fingerprint"] and lm.link_state(f) == "fresh"
    aep.write_bytes(b"v2 saved in After Effects")
    assert lm.link_state(f) == "stale"


def test_a_late_commit_is_not_reported_as_a_failed_render(linked, provider, monkeypatch):
    from classes.editor_tools.titles_text_common import CommitTimeout
    out = _import(linked, provider)
    monkeypatch.setattr(lm, "rerender_linked", lambda *a, **k: (_ for _ in ()).throw(
        CommitTimeout("the editor was too busy; the change is still running -- check before trying again")))
    r = linked.call_receipt("rerender_linked_clip_tool", file_id=out["file_id"])
    assert _failed(r) and "still running" in r["summary"] and "Nothing changed" not in r["summary"]
