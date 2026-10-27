"""Engine tools the desktop runs through the engine bridge (``classes.engine_bridge``).

On once the engine runtime is installed (``classes.engine_runtime``), unless
``ZENVI_ENGINE_BRIDGE`` or the ``engine-bridge`` setting turn it off; without
the runtime no engine tool is listed anywhere. Every tool of the engine's catalog the desktop can use is listed
next to the desktop's own (chat, MCP, the editor-tool manifest), except:

* tools the desktop implements itself -- by name, or as a NATIVE_TWINS
  simple-surface twin of a native tool (native always wins);
* tools the bridge host cannot run (``available: false``): the web editor's
  and the CLI's UI, player, storage and exporter tools.

Linked clips the engine renders (Zenvi Motion blocks, Zenvi 3D scenes, Zenvi
FX) get ``get_linked_clip_tool`` / ``rerender_linked_clip_tool`` from
``engine_links`` when the desktop has no linked-clip tools of its own.

The catalog is read off the GUI thread at start-up (``register_in_background``);
the MCP server lists tools as they arrive. A call runs like this:

1. GUI thread, briefly: the project file, the data version and a reference to
   the project data. The project must have been saved: the engine writes the
   tool's files into its ``<name>_assets`` folder.
2. Worker thread: serialize the project (no undo history), the CLI runs the
   tool and answers the change as desktop UpdateManager actions, the files it
   wrote and the tool's contract-3 receipt (plus any frames it rendered).
3. GUI thread: refuse when the project changed meanwhile (which also catches a
   copy torn by an edit during step 2) -- unless only bin files the answer
   does not touch changed (indexing, thumbnails during a long render); otherwise resolve ``@transitions``-
   style paths with the desktop's own rules and apply the actions inside the
   caller's transaction, with the timeline refreshing once. execute_tool
   groups the call into one undo step; a refused or failed call applies
   nothing and leaves no undo step. ``update_untracked`` actions (cached
   transcripts, the saved playhead) go through
   ``UpdateManager.update_untracked``: saved with the project, never undone.

New files arrive as Project Files inserts, so the files model makes their
thumbnails as it does for any insert; fingerprints are backfilled on save.
They are not indexed (the engine's generated drawings never need it).
"""

from __future__ import annotations

import base64
import json
import os
import threading
from typing import Callable, Optional

from classes import engine_bridge
from classes.editor_tools._base import fail, get_app, on_main
from classes.logger import log

# Desktop tools of the same name always win; these never bridge (the desktop owns them).
NEVER_BRIDGED = frozenset({"undo_tool", "redo_tool", "new_project_tool", "open_project_tool",
                           "save_project_tool", "save_project_as_tool"})
# The engine's simple-surface tools (CLI / MCP) that do what a desktop tool does: a second
# implementation of the same edit on the desktop would drift from it, so the native one stays.
NATIVE_TWINS = {
    "move_clip_tool": "move_clips_tool",
    "trim_clip_tool": "trim_clips_tool",
    "split_clip_tool": "slice_clips_tool",
    "set_clip_property_tool": "set_clip_properties_tool",
    "set_effect_property_tool": "update_effect_tool",
    "add_transition_tool": "apply_transition_tool",
    "probe_media_tool": "inspect_media_tool",
}
SURFACE_NOTE = "Runs in the Zenvi engine (engine bridge)."

# name -> engine catalog entry (name, title, description, inputSchema, annotations)
BRIDGED: dict = {}
# Bumped when BRIDGED changes, so listings built from it (the MCP server's) know to rebuild.
REVISION = 0

STALE = "the project changed while the engine ran the tool; nothing was applied, try again"
SWITCHED = "another project was opened while the engine ran the tool; nothing was applied"

# Answer action kinds -> the UpdateManager call that applies them.
_APPLY = {
    "insert": lambda updates, key, value: updates.insert(key, value),
    "update": lambda updates, key, value: updates.update(key, value),
    "update_untracked": lambda updates, key, value: updates.update_untracked(key, value),
    "delete": lambda updates, key, value: updates.delete(key),
}


def _catalog_cache_path(cli: str) -> str:
    from classes import info
    st = os.stat(cli)
    return os.path.join(info.USER_PATH, "engine_bridge", f"catalog-{st.st_size}-{st.st_mtime_ns}.json")


def load_catalog(client: engine_bridge.EngineBridgeClient) -> list:
    """The CLI's catalog, cached per CLI build so a launch reads a file instead of starting Node."""
    try:
        cache = _catalog_cache_path(client.cli)
    except OSError:
        cache = ""
    if cache and os.path.isfile(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            log.debug("engine bridge catalog cache unreadable", exc_info=True)
    tools = client.tools()
    if cache:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(tools, fh)
        except OSError:
            log.debug("engine bridge catalog not cached", exc_info=True)
    return tools


def _handler(name: str, description: str, run: Optional[Callable] = None):
    def handler(**args):
        return run(**args) if run is not None else run_tool(name, args)
    handler.__name__ = name
    handler.__doc__ = description
    return handler


def bridgeable(tool: dict, existing) -> bool:
    """Whether the desktop lists engine tool *tool* (see the module docstring)."""
    name = tool.get("name")
    return bool(name) and name not in existing and name not in NEVER_BRIDGED and name not in NATIVE_TWINS \
        and bool(tool.get("available", True))


def register_tools(existing, catalog: Optional[list] = None) -> dict:
    """Handlers for the engine tools not in *existing*, by name ({} when the bridge is off).

    Blocks (the catalog cache, or the CLI when it is cold): off the GUI thread,
    or with *catalog* given (tests).
    """
    from classes.editor_tools import engine_links

    if catalog is None:
        if not engine_bridge.bridge_mode():
            return {}
        try:
            catalog = load_catalog(engine_bridge.get_client())
        except engine_bridge.BridgeUnavailable as exc:
            log.warning("Engine bridge unavailable, no engine tools listed: %s", exc)
            return {}
    handlers = {}
    for tool in catalog:
        if not bridgeable(tool, existing):
            continue
        name = tool["name"]
        BRIDGED[name] = tool
        handlers[name] = _handler(name, description(name))
    for tool, run in engine_links.desktop_tools(set(BRIDGED), existing):
        BRIDGED[tool["name"]] = tool
        handlers[tool["name"]] = _handler(tool["name"], description(tool["name"]), run)
    if handlers:
        log.info("Engine bridge: %d engine tools listed", len(handlers))
    return handlers


def register_in_background(existing: Callable[[], set], install: Callable[[dict], None]) -> Optional[threading.Thread]:
    """Read the catalog on a worker thread and hand the handlers to *install* (None when the bridge is off).

    *existing* is called on that thread, once the desktop's own tools are all
    registered; *install* runs there too (it only updates dicts). Whether the
    runtime is installed is checked there as well: it reads files and starts Node.
    """
    if engine_bridge.bridge_setting() == "off":
        return None

    def work():
        global REVISION
        try:
            handlers = register_tools(existing())
            if handlers:
                install(handlers)
                # After install: a listing rebuilt for this revision sees the handlers.
                REVISION += 1
        except Exception:
            log.error("Engine bridge: listing the engine tools failed", exc_info=True)
        finally:
            with _loading_lock:
                _loading[0] -= 1

    with _loading_lock:
        _loading[0] += 1
    thread = threading.Thread(target=work, name="engine-bridge-catalog", daemon=True)
    try:
        thread.start()
    except BaseException:
        with _loading_lock:
            _loading[0] -= 1
        raise
    return thread


# Catalog reads under way (start-up, or after an install): engine tools may still appear.
_loading = [0]
_loading_lock = threading.Lock()


def loading() -> bool:
    """Whether the engine tools are still being listed (in memory only: safe on the GUI thread)."""
    return _loading[0] > 0


def description(name: str) -> str:
    """A bridged tool's description as the desktop lists it."""
    tool = BRIDGED.get(name) or {}
    text = str(tool.get("description") or "").strip()
    # The desktop's own linked-clip tools run on the desktop (they call engine tools themselves).
    return text if tool.get("desktop") else f"{text} {SURFACE_NOTE}".strip()


def tool_def(name: str) -> Optional[dict]:
    """The MCP tool definition of a bridged tool, or None."""
    tool = BRIDGED.get(name)
    if tool is None:
        return None
    return {"name": name, "description": description(name), "inputSchema": tool.get("inputSchema") or {"type": "object"}}


def read_only(name: str) -> bool:
    return bool(((BRIDGED.get(name) or {}).get("annotations") or {}).get("readOnlyHint"))


def manifest_entries() -> list:
    """BRIDGED in the editor-tool manifest's shape (scripts/export_editor_tool_manifest.py), by name."""
    return [{
        "name": name, "label": label(name), "workstream": "engine", "read_only": read_only(name),
        "description": description(name), "input_schema": BRIDGED[name].get("inputSchema") or {"type": "object"},
        "covers": [], "surface": "desktop" if BRIDGED[name].get("desktop") else "engine-bridge",
    } for name in sorted(BRIDGED)]


def label(name: str) -> str:
    return str((BRIDGED.get(name) or {}).get("title") or name)


# ---------------------------------------------------------------------------
# One call
# ---------------------------------------------------------------------------

def _snapshot() -> tuple:
    """(project file, data version, project data) on the GUI thread: references only, no copy."""
    app = get_app()
    project_file = getattr(app.project, "current_filepath", None)
    if not project_file:
        return None, None, None
    return os.path.abspath(project_file), getattr(app.updates, "data_version", None), app.project._data


def _serialize(data: dict) -> Optional[str]:
    """The project as JSON without its history, on the worker thread (None when edits kept tearing it).

    The GUI thread may edit *data* meanwhile: a dict that changes size mid-dump
    raises and is retried; any other edit bumps the data version, so _apply
    refuses the answer computed from a torn copy.
    """
    for _attempt in range(3):
        try:
            return json.dumps({k: v for k, v in list(data.items()) if k != "history"})
        except RuntimeError:
            continue
    return None


def _apply(answer: dict, project_file: str, version) -> Optional[str]:
    """Apply the answer's actions on the GUI thread; an error message when refused."""
    app = get_app()
    # Before STALE: _apply_current reads the keys of a stale answer's actions.
    if not _well_formed(answer.get("actions") or []):
        return "the engine answered a malformed action; nothing was applied"
    current = getattr(app.project, "current_filepath", None)
    if not current or os.path.abspath(current) != project_file:
        return SWITCHED
    if getattr(app.updates, "data_version", None) != version:
        return STALE
    # The engine keeps the bundled-asset aliases (@transitions/…); resolve them like a project load.
    text = app.project.convert_paths_to_absolute(project_file, json.dumps(answer.get("actions") or []))
    actions = json.loads(text)
    unknown = sorted({str(a.get("type")) for a in actions} - set(_APPLY))
    if unknown:
        # Checked before applying anything: a partly applied answer would leave half a change.
        return f"the engine answered an action this desktop cannot apply ({', '.join(unknown)}); nothing was applied"
    updates = app.updates
    window = app.window
    window.IgnoreUpdates.emit(True, True)
    try:
        for action in actions:
            _APPLY[action["type"]](updates, action.get("key"), action.get("value"))
    finally:
        window.IgnoreUpdates.emit(False, True)
    return None


def _well_formed(actions) -> bool:
    """Whether *actions* are all ``{"type", "key": [str | {..}, ...], "value"}`` (no value for a delete)."""
    return isinstance(actions, list) and all(
        isinstance(a, dict) and isinstance(a.get("key"), list) and bool(a["key"])
        and all(isinstance(part, (str, dict)) for part in a["key"])
        and (a.get("type") == "delete" or "value" in a)
        for a in actions)


def _backend_token() -> Optional[str]:
    try:
        from classes.auth_manager import AuthManager
        return AuthManager.instance().get_access_token()
    except Exception:
        log.debug("no desktop session for the engine bridge", exc_info=True)
        return None


def _output(name: str, answer: dict, tracked: bool):
    """The engine's contract-3 receipt text; a ToolOutput with it when the engine rendered frames too."""
    from classes.agent_tools.output import ImageBlock, wrap_str_result

    text = str(answer.get("text") or "")
    images = []
    for image in answer.get("images") or []:
        try:
            images.append(ImageBlock(base64.b64decode(image.get("data") or ""), str(image.get("mimeType") or "image/png")))
        except (ValueError, TypeError, AttributeError):
            log.debug("engine bridge: an image of %s could not be decoded", name, exc_info=True)
    if not tracked:
        # The engine counts the undo step it would have made; untracked-only (or no) changes make none here.
        from classes.agent_tools.receipt import parse_receipt
        receipt = parse_receipt(text)
        if receipt is not None and receipt.get("undoSteps"):
            receipt["undoSteps"] = 0
            text = json.dumps(receipt)
    if not images:
        # execute_tool parses the receipt as is (an untracked-only change stays applied).
        return text
    out = wrap_str_result(name, text)
    out.images = images
    return out


def run_tool(name: str, args: dict):
    """Run bridged tool *name*: the engine's contract-3 receipt (a ToolOutput with frames), or ``Error: ...``."""
    project_file, version, data = on_main(_snapshot)
    if not project_file:
        return fail(f"{name} runs in the Zenvi engine, which needs the project saved first: save it, then try again")
    project_json = _serialize(data)
    if project_json is None:
        return fail("the project kept changing while it was read for the engine; nothing was applied, try again")
    try:
        answer = engine_bridge.get_client().run(name, args, project_json, project_file, _backend_token())
    except engine_bridge.BridgeUnavailable as exc:
        return fail(f"engine bridge: {exc}")
    text = str(answer.get("text") or "")
    if not answer.get("ok"):
        return text if text.lstrip().startswith("{") else fail(text or f"{name} failed in the engine")
    if answer.get("actions"):
        refusal = _apply_current(name, answer, project_file, version, project_json, data)
        if refusal:
            _discard(answer.get("files") or [], project_file)
            return fail(refusal)
    tracked = any(a.get("type") in ("insert", "update", "delete") for a in answer.get("actions") or [])
    return _output(name, answer, tracked)



def _version(data: dict, project_file: str):
    """The data version, or None when the open project is no longer the one the engine ran on."""
    app = get_app()
    current = getattr(app.project, "current_filepath", None)
    if app.project._data is not data or not current or os.path.abspath(current) != project_file:
        return None
    return getattr(app.updates, "data_version", None)


def _apply_current(name: str, answer: dict, project_file: str, version, project_json: str, data: dict) -> Optional[str]:
    """Apply the answer, also when the desktop changed only bin files it does not touch meanwhile.

    Indexing, thumbnails and fingerprints update Project Files records while a
    long engine call (a Blender render) runs. Those leave the answer valid;
    any other change -- clips, tracks, transitions, settings, or a file the
    answer itself updates -- refuses it.
    """
    refusal = on_main(_apply, answer, project_file, version)
    for _round in range(3):
        if refusal != STALE:
            return refusal
        version = on_main(_version, data, project_file)
        if version is None:
            return SWITCHED
        changed = what_changed(project_json, _serialize(data))
        conflicts = _conflicts(changed, answer.get("actions") or [])
        if conflicts:
            log.info("engine bridge: %s refused, the project changed meanwhile: %s", name, ", ".join(conflicts))
            return f"{STALE} (changed: {', '.join(conflicts[:4])})"
        if changed:
            log.info("engine bridge: %s applied over changes to bin files it does not touch: %s", name, ", ".join(changed))
        refusal = on_main(_apply, answer, project_file, version)
    return refusal


def _conflicts(changed: Optional[list], actions: list) -> list:
    """The changes made meanwhile that invalidate an answer of *actions* (None: unknown, everything)."""
    if changed is None:
        return ["the project"]
    touched = {f"{a['key'][0]}/{a['key'][1].get('id')}" for a in actions
               if len(a.get("key") or []) >= 2 and isinstance(a["key"][1], dict)}
    # Clips the answer writes were built from their files' records (duration, reader) as the engine saw them.
    touched |= {f"files/{fid}" for fid in _file_ids(actions)}
    # An update of the whole Project Files list would overwrite every record changed meanwhile.
    whole = any(a.get("key") == ["files"] and a.get("type") != "insert" for a in actions)
    # A file imported or updated meanwhile is fine; one removed may be what the answer's clips play.
    return [c for c in changed if not c.startswith("files/") or whole or c.endswith(" removed") or c in touched]


def _file_ids(value) -> set:
    """Every ``file_id`` inside *value* (actions, clips, readers)."""
    if isinstance(value, dict):
        found = {str(value["file_id"])} if value.get("file_id") else set()
        for v in value.values():
            found |= _file_ids(v)
        return found
    if isinstance(value, list):
        return set().union(*(_file_ids(v) for v in value)) if value else set()
    return set()


def what_changed(before: str, after: Optional[str]) -> Optional[list]:
    """Top-level keys, and ``<list>/<id>`` of top-level list items (``... removed``), that differ between two project JSON texts."""
    if after is None:
        return None
    old, new = json.loads(before), json.loads(after)
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if isinstance(a, list) and isinstance(b, list) and all(isinstance(x, dict) and "id" in x for x in a + b):
            then = {str(x["id"]): x for x in a}
            now = {str(x["id"]): x for x in b}
            out += [f"{key}/{i}" + (" removed" if i not in now else "")
                    for i in sorted(set(then) | set(now)) if then.get(i) != now.get(i)]
        else:
            out.append(key)
    return out


def _discard(files: list, project_file: str) -> None:
    """Remove files a refused call wrote (only inside the project's assets folder)."""
    from classes.assets import get_assets_path
    # Canonical paths: a symlink inside the assets folder must not lead the removal outside it.
    assets = os.path.realpath(get_assets_path(project_file, create_paths=False)) + os.sep
    for path in files:
        if isinstance(path, str) and os.path.realpath(path).startswith(assets):
            try:
                os.remove(path)
            except OSError:
                log.debug("could not remove %s", path, exc_info=True)
