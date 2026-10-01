"""Contract every registered editor tool must meet (classes.editor_tools).

These run over whatever is registered, so a new tool is checked the moment it
is added: name rules the backend depends on, an explicit schema that matches
the handler, a real description, dispatch flags that agree with
tool_handlers, and the one-delete-tool rule for timeline clips.
"""

import inspect
import re

import pytest

from classes import tool_handlers
from classes.editor_tools import REGISTRY, coverage, workstream_of
from classes.editor_tools._registry import RESERVED_NAMES, coerce, ToolArgumentError, editor_tool
from classes.editor_tools._base import obj, string, number, boolean, enum, array, ToolError, ok

SPECS = sorted(REGISTRY.values(), key=lambda s: s.name)
IDS = [s.name for s in SPECS]
CAPABILITY_IDS = {c[0] for c in coverage.CAPABILITIES}

# Removing things other than timeline clips gets its own clearly named tool;
# deleting timeline clips stays delete_from_timeline_tool alone (the backend's
# contract test enforces one clip-delete tool in the catalog).
ALLOWED_REMOVAL_NOUNS = ("effect", "effects", "transition", "transitions", "marker", "markers",
                         "track", "file", "files", "keyframe", "keyframes", "look", "caption",
                         "captions", "proxy", "proxies", "gap", "gaps", "profile", "title")


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_tool_is_dispatchable_and_labelled(spec):
    assert tool_handlers.AGENT_TOOL_HANDLERS[spec.name] is spec.func
    assert tool_handlers.TOOL_DISPLAY_LABELS[spec.name] == spec.label and spec.label.strip()
    assert (spec.name in tool_handlers.READ_ONLY_TOOLS) == spec.read_only
    assert (spec.name in tool_handlers.BACKGROUND_SAFE_TOOLS) == spec.background_safe
    if not spec.read_only:
        assert spec.name not in tool_handlers._UNGROUPED_TOOLS, "mutating tools are one undo step"


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_tool_name_survives_the_backend(spec):
    assert spec.name.endswith("_tool") and len(spec.name) <= 58
    assert spec.name not in RESERVED_NAMES
    m = re.search(r"(^|_)(delete|remove|clear)_(\w+?)_tool$", spec.name)
    if re.search(r"(^|_)(delete|clear)(_|$)", spec.name):
        pytest.fail(f"{spec.name}: only delete_from_timeline_tool may delete/clear timeline clips")
    if m:
        noun = m.group(3).split("_")[0]
        assert noun in ALLOWED_REMOVAL_NOUNS, f"{spec.name}: removal tools must name a non-clip noun"
        assert "clip" in spec.description.lower() or "never" in spec.description.lower(), (
            f"{spec.name}: say in the description that it never deletes timeline clips")


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_tool_description_is_written_for_the_model(spec):
    desc = spec.description
    assert len(desc) >= 80, f"{spec.name}: describe when to use it and what it changes"
    first = desc.split("\n", 1)[0]
    assert first and first[0].isupper(), f"{spec.name}: start with a sentence"


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_tool_schema_matches_handler(spec):
    params = inspect.signature(inspect.unwrap(spec.func)).parameters
    assert set(params) == set(spec.schema["properties"])
    assert spec.schema["additionalProperties"] is False
    for name, prop in spec.schema["properties"].items():
        assert prop.get("description", "").strip(), f"{spec.name}.{name} needs a description"
        default = params[name].default
        if default is not inspect.Parameter.empty and "default" in prop:
            assert prop["default"] == default, f"{spec.name}.{name}: schema default != handler default"


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_tool_covers_known_capabilities(spec):
    assert spec.covers, f"{spec.name}: list the coverage ids it fulfils"
    unknown = set(spec.covers) - CAPABILITY_IDS
    assert not unknown, f"{spec.name}: unknown coverage ids {sorted(unknown)}"
    ws = workstream_of(spec.domain)
    assert ws, f"{spec.name}: registered from {spec.domain}, which is no workstream's module"
    owned = {c[0] for c in coverage.CAPABILITIES if c[1] == ws}
    assert set(spec.covers) <= owned, (
        f"{spec.name} ({ws}) claims capabilities owned by other workstreams: "
        f"{sorted(set(spec.covers) - owned)}")


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_unknown_arguments_are_refused_with_the_accepted_list(spec):
    out = spec.func(definitely_not_an_argument=1)
    assert out.startswith("Error") and "definitely_not_an_argument" in out


# --- registry mechanics (independent of registered tools) ---------------------

def test_coerce_handles_what_models_send():
    assert coerce("x", "1.5", {"type": "number"}) == 1.5
    assert coerce("x", "2", {"type": "integer"}) == 2
    assert coerce("x", "true", {"type": "boolean"}) is True
    assert coerce("x", '["a","b"]', {"type": "array", "items": {"type": "string"}}) == ["a", "b"]
    assert coerce("x", "a, b", {"type": "array", "items": {"type": "string"}}) == ["a", "b"]
    assert coerce("x", "FAST", {"enum": ["fast", "slow"]}) == "fast"
    assert coerce("x", '{"k": "1"}', {"type": "object", "properties": {"k": {"type": "number"}}}) == {"k": 1.0}
    with pytest.raises(ToolArgumentError):
        coerce("x", "abc", {"type": "number"})
    with pytest.raises(ToolArgumentError):
        coerce("x", 5, {"type": "number", "maximum": 1})
    with pytest.raises(ToolArgumentError):
        coerce("x", "medium", {"enum": ["fast", "slow"]})


def test_registration_rejects_schema_drift_and_bad_names():
    schema = obj({"a": string("A.", "")})

    with pytest.raises(ValueError):
        @editor_tool("split_clip_tool", label="x", schema=schema, covers=("clip.slice",))
        def _reserved(a=""):
            """Reserved name."""

    with pytest.raises(ValueError):
        @editor_tool("drift_probe_tool", label="x", schema=schema, covers=("clip.slice",))
        def _drift(a="", b=""):
            """Schema lists a, handler takes a and b."""

    with pytest.raises(ValueError):
        @editor_tool("kwargs_probe_tool", label="x", schema=schema, covers=("clip.slice",))
        def _kw(a="", **kw):
            """Handlers take explicit arguments."""

    assert "drift_probe_tool" not in REGISTRY and "kwargs_probe_tool" not in REGISTRY


def test_wrapper_coerces_and_maps_tool_errors():
    schema = obj({"speed": number("Speed.", 1.0, minimum=0.1), "fast": boolean("Fast.", False),
                  "mode": enum(["a", "b"], "Mode.", "a"), "ids": array({"type": "string"}, "Ids.", [])})
    seen = {}

    @editor_tool("wrapper_probe_tool", label="Probe", schema=schema, covers=("clip.speed",))
    def probe(speed=1.0, fast=False, mode="a", ids=None):
        """Probe tool used only by this test; long enough description for the contract."""
        seen.update(speed=speed, fast=fast, mode=mode, ids=ids)
        if speed > 5:
            raise ToolError("too fast")
        return ok("done", speed=speed)

    try:
        assert probe(speed="2", fast="yes", mode="B", ids="x,y").startswith("done")
        assert seen == {"speed": 2.0, "fast": True, "mode": "b", "ids": ["x", "y"]}
        assert probe(speed=9) == "Error: too fast"
        assert probe(speed="fast").startswith("Error: wrapper_probe_tool: argument 'speed'")
        assert probe(speed=0.01).startswith("Error")
    finally:
        REGISTRY.pop("wrapper_probe_tool", None)
