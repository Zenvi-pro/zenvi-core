"""Registry for the editor tools in ``classes.editor_tools``.

Each workstream module declares its tools with :func:`editor_tool`, giving the
handler, the chat label, an explicit JSON schema, the dispatch flags and the
coverage ids it fulfils (``coverage.py``). ``tool_handlers`` merges the
registry into ``AGENT_TOOL_HANDLERS`` and its dispatch sets, the in-app MCP
server lists the explicit schemas, and ``scripts/export_editor_tool_manifest.py``
hands the same schemas to zenvi-backend, so the Zenvi Assistant and external
agent CLIs see one definition of every tool.

The schema is the contract. At registration the registry checks that its
properties match the handler's keyword parameters (so they cannot drift), and
at call time it coerces what the model sent ("1.5", "true", '["a"]') to the
schema's types and refuses unknown arguments with the list of accepted ones.
"""

from __future__ import annotations

import copy
import functools
import inspect
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

# Backend aliases (zenvi-backend core/planning/tool_aliases.py) silently
# rewrite these names to other tools; a tool registered under one would never
# be reached from the Zenvi Assistant.
RESERVED_NAMES = frozenset({
    "search_freesound_tool", "freesound_search_tool", "freesound_tool", "search_music_tool",
    "search_pexels_tool", "pexels_search_tool", "search_stock_tool", "get_clips_metadata_tool",
    "get_clip_metadata_tool", "get_timeline_tool", "list_timeline_tool", "import_stock_tool",
    "import_media_tool", "add_clip_tool", "add_to_timeline_tool", "split_clip_tool",
    "split_file_tool", "slice_at_best_match_tool", "generate_video_tool", "ai_video_tool",
    "object_replace_tool", "clip_edit_tool", "ai_morph_tool", "morph_transition_tool",
    "generate_tts_tool", "tts_tool", "stock_video_tool", "stock_music_tool", "product_demo_tool",
    "motion_graphics_tool", "product_launch_tool", "retag_project_file_tool", "retag_tool",
    "delete_clip_tool", "delete_clips_tool", "remove_clips_tool", "remove_clip_from_timeline_tool",
    "clear_track_tool", "delete_track_tool", "delete_timeline_clip_tool",
})
MAX_NAME_LENGTH = 58  # OpenCode exposes zenvi_<name>; OpenAI caps tool names at 64.


class ToolArgumentError(Exception):
    """The model sent an argument the schema does not allow."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    func: Callable[..., str]
    label: str
    schema: dict
    domain: str
    read_only: bool = False
    background_safe: bool = False
    covers: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def description(self) -> str:
        return inspect.cleandoc(inspect.unwrap(self.func).__doc__ or "")


REGISTRY: Dict[str, ToolSpec] = {}


def _check_schema(name: str, func: Callable, schema: dict) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"{name}: schema must be a JSON object schema")
    if schema.get("additionalProperties", False) is not False:
        raise ValueError(f"{name}: schema must set additionalProperties: false")
    props = schema.get("properties") or {}
    for pname, pschema in props.items():
        if not (pschema.get("description") or "").strip():
            raise ValueError(f"{name}: property {pname!r} needs a description (the model reads it)")
    sig = inspect.signature(func)
    params = {
        p.name: p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        raise ValueError(f"{name}: editor tools take explicit keyword arguments, not **kwargs")
    if set(params) != set(props):
        raise ValueError(
            f"{name}: schema properties {sorted(props)} != handler parameters {sorted(params)}")
    required = set(schema.get("required") or [])
    for pname, param in params.items():
        has_default = param.default is not inspect.Parameter.empty
        if pname in required and has_default:
            raise ValueError(f"{name}: required argument {pname!r} must not have a default")
        if pname not in required and not has_default:
            raise ValueError(f"{name}: optional argument {pname!r} needs a default")


def editor_tool(
    name: str,
    *,
    label: str,
    schema: dict,
    read_only: bool = False,
    background_safe: bool = False,
    covers: Tuple[str, ...] | list = (),
):
    """Register the decorated function as the agent tool *name*.

    label           -- chat tool-block title ("Add effect").
    schema          -- JSON schema of the arguments (``_base.obj`` & co.).
    read_only       -- never mutates the project or Qt state; runs on the
                       calling worker thread, outside any undo group.
    background_safe -- does slow work (disk, network, render) off the GUI
                       thread and marshals its brief Qt/project touches with
                       ``_base.on_main``. Still grouped into one undo step.
    covers          -- ids from ``coverage.CAPABILITIES`` this tool fulfils.
    """
    if not name.endswith("_tool"):
        raise ValueError(f"agent tool names end in _tool: {name!r}")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{name}: longer than {MAX_NAME_LENGTH} characters")
    if name in RESERVED_NAMES:
        raise ValueError(f"{name}: the backend rewrites this name to another tool; pick another")
    if read_only and background_safe:
        raise ValueError(f"{name}: read_only already implies off-GUI-thread dispatch")

    def deco(func):
        if name in REGISTRY:
            raise ValueError(f"agent tool {name!r} registered twice")
        if not (func.__doc__ or "").strip():
            raise ValueError(f"{name}: the docstring is the model-facing description; write one")
        _check_schema(name, func, schema)
        tool = _wrap(name, func, schema)
        module = getattr(func, "__module__", "") or ""
        REGISTRY[name] = ToolSpec(
            name=name, func=tool, label=label, schema=copy.deepcopy(schema),
            domain=module.rsplit(".", 1)[-1], read_only=read_only,
            background_safe=background_safe, covers=tuple(covers),
        )
        return tool

    return deco


# ---------------------------------------------------------------------------
# Argument coercion from the schema
# ---------------------------------------------------------------------------

_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


def coerce(path: str, value: Any, schema: dict) -> Any:
    """Coerce a JSON-ish *value* to *schema*; ToolArgumentError when impossible."""
    if value is None:
        if schema.get("nullable") or "null" in _types(schema):
            return None
        raise ToolArgumentError(f"argument {path!r} must not be null")
    if "enum" in schema:
        choices = schema["enum"]
        if value in choices:
            return value
        if isinstance(value, str):
            for c in choices:
                if isinstance(c, str) and c.lower() == value.strip().lower():
                    return c
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            for c in choices:
                if isinstance(c, str) and c == str(value):
                    return c
        raise ToolArgumentError(f"argument {path!r} must be one of {choices}, got {value!r}")
    kinds = _types(schema)
    last = None
    for kind in kinds:
        try:
            return _coerce_kind(path, value, kind, schema)
        except ToolArgumentError as exc:
            last = exc
    if last:
        raise last
    return value


def _types(schema: dict) -> list:
    t = schema.get("type")
    if isinstance(t, list):
        return [k for k in t if k != "null"] or ["null"]
    if t:
        return [t]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            out = []
            for sub in schema[key]:
                out.extend(_types(sub))
            return out
    return []


def _coerce_kind(path, value, kind, schema):
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ToolArgumentError(f"argument {path!r} must be true or false, got {value!r}")
    if kind in ("number", "integer"):
        if isinstance(value, bool):
            raise ToolArgumentError(f"argument {path!r} must be a number, got {value!r}")
        try:
            f = float(value) if isinstance(value, (int, float)) else float(str(value).strip().rstrip("s"))
        except ValueError:
            raise ToolArgumentError(f"argument {path!r} must be a number, got {value!r}") from None
        if not math.isfinite(f):
            raise ToolArgumentError(f"argument {path!r} must be finite, got {value!r}")
        if kind == "integer":
            if f != int(f):
                raise ToolArgumentError(f"argument {path!r} must be a whole number, got {value!r}")
            f = int(f)
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and f < lo:
            raise ToolArgumentError(f"argument {path!r} must be >= {lo}, got {value!r}")
        if hi is not None and f > hi:
            raise ToolArgumentError(f"argument {path!r} must be <= {hi}, got {value!r}")
        return f
    if kind == "string":
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)
    if kind == "array":
        if isinstance(value, str):
            s = value.strip()
            if not s:
                value = []
            elif s.startswith("["):
                try:
                    value = json.loads(s)
                except ValueError:
                    raise ToolArgumentError(f"argument {path!r} is not a valid JSON list") from None
            else:
                value = [p.strip() for p in s.split(",") if p.strip()]
        elif isinstance(value, tuple):
            value = list(value)
        elif not isinstance(value, list):
            value = [value]
        items = schema.get("items")
        if isinstance(items, dict) and items:
            value = [coerce(f"{path}[{i}]", v, items) for i, v in enumerate(value)]
        return value
    if kind == "object":
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return {}
            try:
                value = json.loads(s)
            except ValueError:
                raise ToolArgumentError(f"argument {path!r} is not a valid JSON object") from None
        if not isinstance(value, dict):
            raise ToolArgumentError(f"argument {path!r} must be an object, got {type(value).__name__}")
        props = schema.get("properties") or {}
        if props:
            out = {}
            for k, v in value.items():
                if k in props:
                    out[k] = coerce(f"{path}.{k}", v, props[k])
                elif schema.get("additionalProperties", True) is False:
                    raise ToolArgumentError(f"argument {path!r} has unknown key {k!r}; allowed: {sorted(props)}")
                else:
                    out[k] = v
            for k in schema.get("required") or []:
                if k not in out:
                    raise ToolArgumentError(f"argument {path!r} needs key {k!r}")
            return out
        return value
    return value


def _wrap(name: str, func: Callable, schema: dict) -> Callable[..., str]:
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])

    @functools.wraps(func)
    def _tool(**kwargs):
        from classes.editor_tools._base import ToolError, fail

        # chat_session_id / transaction_id ride along from the dispatchers.
        kwargs.pop("chat_session_id", None)
        kwargs.pop("transaction_id", None)
        unknown = sorted(k for k in kwargs if k not in props)
        if unknown:
            return fail(f"{name}: unknown argument(s) {', '.join(unknown)}; "
                        f"accepted: {', '.join(props) or 'none'}")
        missing = [k for k in required if k not in kwargs or kwargs[k] in (None, "")]
        if missing:
            return fail(f"{name}: missing required argument(s) {', '.join(missing)}")
        try:
            clean = {k: coerce(k, v, props[k]) for k, v in kwargs.items()}
            return func(**clean)
        except ToolArgumentError as exc:
            return fail(f"{name}: {exc}")
        except ToolError as exc:
            return fail(str(exc))

    return _tool
