"""The shared handoff editor tools, called through execute_tool like the chat does (contract-3 receipts)."""

import json
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
    # the real module may be imported already (its own tests); hide it so the broken finder is consulted
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


def test_after_effects_prores422_renders_are_linked_as_opaque(linked, tmp_path):
    import json as _json
    from classes.handoff.aftereffects_link import AfterEffectsProvider, _schema_cache
    host = FakeHost()
    _schema_cache.clear()
    try:
        host.tools = [{"name": "ae_render_for_zenvi", "inputSchema": {"type": "object", "properties": {
            "comp": {}, "output_dir": {}}}}]

        def render(name, args):
            out = os.path.join(args["output_dir"], "Promo.mov")
            with open(out, "wb") as fh:
                fh.write(b"prores 422")
            linked.probe.durations[out] = 1.0
            receipt = {"contract": 3, "status": "applied", "tool": name, "host": "aftereffects", "summary": "ok",
                       "data": {"path": out, "comp_name": "Promo", "width": 1920, "height": 1080, "fps": 30,
                                "duration": 1.0, "codec": "prores422"}}
            return {"content": [{"type": "text", "text": _json.dumps(receipt)}], "structuredContent": receipt,
                    "isError": False}

        host.call = render
        write_discovery(linked.user_path, host)
        lm.register_provider(AfterEffectsProvider())
        out = lm.import_linked({"kind": "aftereffects", "source": {"composition": "Promo"}}, position=0.0)
        stored = lm.read_link(linked.file(out["file_id"]))
        assert stored["render"]["codec"] == "prores422" and "prores422" not in lm.ALPHA_CODECS
    finally:
        host.stop()


AE_CATALOG = [
    {"name": "ae_get_state", "title": "Get state", "description": "App, project, active comp.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}, "outputSchema": {"type": "object"}, "timeoutMs": 5000},
    {"name": "ae_add_text", "title": "Add text", "description": "Text layer with tasteful defaults.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "The text."}},
                     "required": ["text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False}, "returnsImage": False},
]
PREMIERE_CATALOG = [
    {"name": "premiere_get_state", "title": "Premiere Pro state", "description": "App, project, sequence.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
]
DETAIL_KEYS = {"name", "title", "description", "inputSchema", "annotations"}


def _big_catalog(n):
    """*n* host tools whose schemas weigh what real ones do (~2 KB each: AE has 30 at ~70 KB in all)."""
    props = {"option_%d" % i: {"type": "string", "description": "An option of this tool, described at the "
                               "length the real After Effects tools use for theirs."} for i in range(12)}
    return [{"name": "ae_tool_%02d" % i, "title": "Tool number %02d" % i,
             "description": "Does one After Effects thing, with the caveats a model needs to know. " * 5,
             "inputSchema": {"type": "object", "properties": props, "additionalProperties": False},
             "annotations": {"readOnlyHint": False}, "outputSchema": {"type": "object", "properties": props},
             "timeoutMs": 60000} for i in range(n)]


def _second_host(linked, catalog=None, tools_error=None):
    premiere = FakeHost(app="premiere")
    premiere.tools = catalog if catalog is not None else PREMIERE_CATALOG
    premiere.tools_error = tools_error
    write_discovery(linked.user_path, premiere, app="premiere")
    return premiere


def test_include_tools_lists_each_connected_hosts_tools_by_name_and_title(linked, ae_host):
    ae_host.tools = AE_CATALOG
    r = linked.call_receipt("list_link_hosts_tool")
    assert "tools" not in r["data"]["hosts"][0]  # off by default: the listing stays small
    r = linked.call_receipt("list_link_hosts_tool", include_tools=True)
    ae, pr = r["data"]["hosts"]
    assert ae["tools"] == [{"name": "ae_get_state", "title": "Get state"}, {"name": "ae_add_text", "title": "Add text"}]
    assert "tools" not in pr and "tools_error" not in pr  # not connected: nothing to list
    assert "(2 tools)" in r["summary"] and r["status"] == "applied" and r["undoSteps"] == 0


def test_named_tools_come_back_in_full_and_unknown_names_per_host(linked, ae_host):
    ae_host.tools = AE_CATALOG
    premiere = _second_host(linked)
    try:
        r = linked.call_receipt("list_link_hosts_tool", tools=["ae_add_text", "ae_nope", " ae_add_text "])
        ae, pr = r["data"]["hosts"]
        assert [t["name"] for t in ae["tools"]] == ["ae_add_text"]  # only what was asked for, once
        assert set(ae["tools"][0]) == DETAIL_KEYS  # no outputSchema / timeoutMs / returnsImage
        assert ae["tools"][0]["inputSchema"]["required"] == ["text"]  # what call_link_host_tool needs
        assert ae["unknown_tools"] == ["ae_nope"] and "deferred_tools" not in ae
        assert pr["tools"] == [] and pr["unknown_tools"] == ["ae_add_text", "ae_nope"]
        assert "(1 described)" in r["summary"] and "No connected app has ae_nope." in r["summary"]
        assert r["status"] == "applied" and r["undoSteps"] == 0
        r = linked.call_receipt("list_link_hosts_tool", tools="premiere_get_state")  # a bare name works too
        ae, pr = r["data"]["hosts"]
        assert ae["tools"] == [] and [t["name"] for t in pr["tools"]] == ["premiere_get_state"]
        assert "No connected app has" not in r["summary"]
    finally:
        premiere.stop()


def test_include_tools_with_named_tools_expands_just_those(linked, ae_host):
    ae_host.tools = AE_CATALOG
    r = linked.call_receipt("list_link_hosts_tool", include_tools=True, tools=["ae_add_text"])
    tools = r["data"]["hosts"][0]["tools"]
    assert tools[0] == {"name": "ae_get_state", "title": "Get state"}
    assert set(tools[1]) == DETAIL_KEYS and tools[1]["name"] == "ae_add_text"
    assert "(2 tools, 1 described)" in r["summary"]


def test_tool_listings_stay_small_enough_for_the_assistant_to_read(linked, ae_host):
    from classes import tool_handlers
    from classes.editor_tools import handoff as handoff_tools
    from classes.handoff import adobe_link
    ae_host.tools = _big_catalog(60)
    whole = sum(handoff_tools._json_bytes(adobe_link.tool_details(t)) for t in ae_host.tools)
    assert whole > 51_200  # every schema at once would be cut off (OpenCode keeps 51,200 bytes of a result)
    listing = tool_handlers.execute_tool("list_link_hosts_tool", {"include_tools": True})
    assert len(listing.encode("utf-8")) < 6_000, len(listing.encode("utf-8"))
    names = [t["name"] for t in ae_host.tools]
    out = tool_handlers.execute_tool("list_link_hosts_tool", {"tools": names})  # asking for everything
    assert len(out.encode("utf-8")) < 51_200
    ae = json.loads(out)["data"]["hosts"][0]
    described = [t["name"] for t in ae["tools"]]
    assert described and ae["deferred_tools"] and described + ae["deferred_tools"] == names
    assert "ask again with tools=[%s]" % ", ".join(ae["deferred_tools"]) in json.loads(out)["summary"]


def test_host_tool_catalogs_are_cached_per_host_session(linked, ae_host):
    from classes.handoff import adobe_link
    ae_host.tools = AE_CATALOG
    lists = lambda: sum(1 for c in ae_host.calls if c.get("method") == "tools/list")  # noqa: E731
    linked.call_receipt("list_link_hosts_tool", include_tools=True)
    linked.call_receipt("list_link_hosts_tool", tools=["ae_add_text"])
    adobe_link.tool_timeout("aftereffects", "ae_get_state")  # the call timeout reads the same cache
    assert lists() == 1
    write_discovery(linked.user_path, ae_host, pid=os.getppid())  # the extension restarted: new pid
    linked.call_receipt("list_link_hosts_tool", include_tools=True)
    assert lists() == 2
    ae_host.tools = AE_CATALOG[:1]  # restarted again in the same helper process, same port: a new start time
    write_discovery(linked.user_path, ae_host, pid=os.getppid(), started_at="2026-10-05T09:30:00Z")
    r = linked.call_receipt("list_link_hosts_tool", include_tools=True)
    assert lists() == 3 and [t["name"] for t in r["data"]["hosts"][0]["tools"]] == ["ae_get_state"]


def test_a_host_that_cannot_list_its_tools_reports_the_error(linked, ae_host):
    ae_host.tools = AE_CATALOG
    premiere = _second_host(linked, tools_error="catalog failed to load")
    try:
        for args in ({"include_tools": True}, {"tools": ["ae_add_text", "premiere_get_state"]}):
            r = linked.call_receipt("list_link_hosts_tool", **args)
            ae, pr = r["data"]["hosts"]
            assert pr["connected"] and "tools" not in pr and "unknown_tools" not in pr
            assert pr["tools_error"]["code"] == "HOST_ERROR"
            assert "catalog failed to load" in pr["tools_error"]["message"]
            assert ae["tools"] and "(its tool list failed)" in r["summary"]  # the other app still lists
            assert "No connected app has" not in r["summary"]  # Premiere may have it: we could not tell
    finally:
        premiere.stop()
