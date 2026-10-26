"""Engine tools the desktop runs through the engine bridge (``classes.engine_bridge``).

Prototype, off unless ``ZENVI_ENGINE_BRIDGE`` is set. Every tool of the engine's
catalog that the desktop does not implement itself is listed next to the
desktop's own (chat, MCP) and runs like this:

1. GUI thread, briefly: serialize the open project as it is in memory (no
   undo history). The project must have been saved: the engine writes the
   tool's files into its ``<name>_assets`` folder.
2. Worker thread: the CLI runs the tool and answers the change as desktop
   UpdateManager actions, the files it wrote and the tool's receipt.
3. GUI thread: refuse when the project changed meanwhile; otherwise resolve
   ``@transitions``-style paths with the desktop's own rules and apply the
   actions inside the caller's transaction, with the timeline refreshing
   once. execute_tool groups the call into one undo step; a refused or
   failed call applies nothing and leaves no undo step. ``update_untracked``
   actions (cached transcripts, the saved playhead) go through
   ``UpdateManager.update_untracked``: saved with the project, never undone.

New files arrive as Project Files inserts, so the files model makes their
thumbnails as it does for any insert; fingerprints are backfilled on save.
They are not indexed (the engine's generated drawings never need it).
"""

from __future__ import annotations

import json
import os
from typing import Optional

from classes import engine_bridge
from classes.editor_tools._base import fail, get_app, on_main
from classes.logger import log

# Desktop tools of the same name always win; these never bridge (the desktop owns them).
NEVER_BRIDGED = frozenset({"undo_tool", "redo_tool", "new_project_tool", "open_project_tool",
                           "save_project_tool", "save_project_as_tool"})
SURFACE_NOTE = "Runs in the Zenvi engine (engine bridge)."

# name -> engine catalog entry (name, title, description, inputSchema, annotations)
BRIDGED: dict = {}

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


def _handler(name: str, description: str):
    def handler(**args):
        return run_tool(name, args)
    handler.__name__ = name
    handler.__doc__ = description
    return handler


def register_tools(existing, catalog: Optional[list] = None) -> dict:
    """Handlers for the engine tools not in *existing*, by name ({} when the bridge is off)."""
    if catalog is None:
        if not engine_bridge.bridge_mode():
            return {}
        try:
            catalog = load_catalog(engine_bridge.get_client())
        except engine_bridge.BridgeUnavailable as exc:
            log.warning("Engine bridge unavailable: %s", exc)
            return {}
    handlers = {}
    for tool in catalog:
        name = tool.get("name")
        if not name or name in existing or name in NEVER_BRIDGED or not tool.get("available", True):
            continue
        BRIDGED[name] = tool
        handlers[name] = _handler(name, f"{tool.get('description', '').strip()} {SURFACE_NOTE}".strip())
    if handlers:
        log.info("Engine bridge: %d engine tools listed", len(handlers))
    return handlers


def tool_def(name: str) -> Optional[dict]:
    """The MCP tool definition of a bridged tool, or None."""
    tool = BRIDGED.get(name)
    if tool is None:
        return None
    return {"name": name, "description": f"{tool.get('description', '').strip()} {SURFACE_NOTE}".strip(),
            "inputSchema": tool.get("inputSchema") or {"type": "object"}}


def label(name: str) -> str:
    return str((BRIDGED.get(name) or {}).get("title") or name)


# ---------------------------------------------------------------------------
# One call
# ---------------------------------------------------------------------------

def _snapshot() -> tuple:
    """(project JSON, project file, data version) on the GUI thread."""
    app = get_app()
    project_file = getattr(app.project, "current_filepath", None)
    if not project_file:
        return None, None, None
    data = {k: v for k, v in app.project._data.items() if k != "history"}
    return json.dumps(data), os.path.abspath(project_file), getattr(app.updates, "data_version", None)


def _apply(answer: dict, project_file: str, version) -> Optional[str]:
    """Apply the answer's actions on the GUI thread; an error message when refused."""
    app = get_app()
    if getattr(app.updates, "data_version", None) != version:
        return "the project changed while the engine ran the tool; nothing was applied, try again"
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


def _backend_token() -> Optional[str]:
    try:
        from classes.auth_manager import AuthManager
        return AuthManager.instance().get_access_token()
    except Exception:
        log.debug("no desktop session for the engine bridge", exc_info=True)
        return None


def run_tool(name: str, args: dict) -> str:
    """Run bridged tool *name*; the engine's receipt JSON, or ``Error: ...``."""
    project_json, project_file, version = on_main(_snapshot)
    if not project_file:
        return fail(f"{name} runs in the Zenvi engine, which needs the project saved first: save it, then try again")
    try:
        answer = engine_bridge.get_client().run(name, args, project_json, project_file, _backend_token())
    except engine_bridge.BridgeUnavailable as exc:
        return fail(f"engine bridge: {exc}")
    text = str(answer.get("text") or "")
    if not answer.get("ok"):
        return text if text.lstrip().startswith("{") else fail(text or f"{name} failed in the engine")
    if answer.get("actions"):
        refusal = on_main(_apply, answer, project_file, version)
        if refusal:
            _discard(answer.get("files") or [], project_file)
            return fail(refusal)
    return text


def _discard(files: list, project_file: str) -> None:
    """Remove files a refused call wrote (only inside the project's assets folder)."""
    from classes.assets import get_assets_path
    assets = os.path.abspath(get_assets_path(project_file, create_paths=False)) + os.sep
    for path in files:
        if os.path.abspath(path).startswith(assets):
            try:
                os.remove(path)
            except OSError:
                log.debug("could not remove %s", path, exc_info=True)
