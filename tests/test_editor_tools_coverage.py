"""Every editor capability is reachable by the Zenvi Assistant, or excluded with a reason.

coverage.CAPABILITIES is the checklist built from a full inventory of the
editor UI. A capability is covered when a registered tool lists it in
``covers=`` or when a pre-existing tool serves it (LEGACY_COVERAGE). A
workstream that has registered any tool must cover all of its capabilities;
set ZENVI_EDITOR_TOOLS_STRICT=1 (integration branch, CI) to require every
workstream to be complete.
"""

import os

import pytest

from classes import tool_handlers
from classes.editor_tools import REGISTRY, coverage, workstream_of

IDS = [c[0] for c in coverage.CAPABILITIES]
WORKSTREAMS = sorted({c[1] for c in coverage.CAPABILITIES})


def _covered():
    out = {}
    for spec in REGISTRY.values():
        for cid in spec.covers:
            out.setdefault(cid, []).append(spec.name)
    for cid, tools in coverage.LEGACY_COVERAGE.items():
        live = [t for t in tools if t in tool_handlers.AGENT_TOOL_HANDLERS]
        if live:
            out.setdefault(cid, []).extend(live)
    return out


def test_capability_ids_are_unique_and_well_formed():
    assert len(IDS) == len(set(IDS))
    for cid, ws, where, what in coverage.CAPABILITIES:
        assert "." in cid and ws and where and what


def test_out_of_scope_entries_have_reasons_and_do_not_shadow_capabilities():
    for cid, reason in coverage.OUT_OF_SCOPE.items():
        assert len(reason) > 30, f"{cid}: give a concrete reason"
        assert cid not in IDS, f"{cid} is both a capability and out of scope"


def test_legacy_coverage_points_at_registered_tools():
    for cid, tools in coverage.LEGACY_COVERAGE.items():
        assert cid in IDS
        for t in tools:
            assert t in tool_handlers.AGENT_TOOL_HANDLERS, f"{cid}: {t} is not a registered tool"


@pytest.mark.parametrize("workstream", WORKSTREAMS)
def test_workstream_covers_all_of_its_capabilities(workstream):
    has_tools = any(workstream_of(s.domain) == workstream for s in REGISTRY.values())
    strict = os.environ.get("ZENVI_EDITOR_TOOLS_STRICT") == "1"
    if not has_tools and not strict:
        pytest.skip(f"{workstream} has not registered tools yet")
    covered = _covered()
    missing = [cid for cid, ws, _w, _d in coverage.CAPABILITIES if ws == workstream and cid not in covered]
    assert not missing, f"{workstream} capabilities with no tool: {missing}"
