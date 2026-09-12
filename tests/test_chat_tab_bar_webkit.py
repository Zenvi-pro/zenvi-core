"""zenvi-core #65 — the Windows WebKit chat must be able to list/switch/close sessions.

The Python session model is engine-agnostic; the break was purely in
``src/chat_ui/chat.js`` ``renderTabs()``: it called ``.forEach`` on the
``NodeList`` from ``querySelectorAll`` and used ``Element.closest`` — neither of
which exists on the Qt WebKit build used for the Windows embed — so the tab bar
threw before rendering a single tab.

``tests/fixtures/webkit_chat_tabs_harness.js`` runs the real ``renderTabs``
source under a fake DOM that mimics that WebKit (NodeList without ``forEach``,
elements without ``closest``).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CHAT_JS = _REPO / "src" / "chat_ui" / "chat.js"
_HARNESS = _REPO / "tests" / "fixtures" / "webkit_chat_tabs_harness.js"


def _render_tabs_source() -> str:
    src = _CHAT_JS.read_text(encoding="utf-8")
    start = src.index("function renderTabs(")
    depth = 0
    i = src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError("unbalanced braces for renderTabs")


def test_render_tabs_has_no_webkit_unsafe_dom_calls():
    """Static guard: the tab-bar path must not lean on modern-only DOM APIs."""
    body = _render_tabs_source()
    assert not re.search(r"querySelectorAll\([^)]*\)\s*\.\s*forEach", body), (
        "renderTabs iterates a NodeList with .forEach — breaks on Qt WebKit"
    )
    assert ".closest(" not in body, (
        "renderTabs uses Element.closest — not available on Qt WebKit"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_render_tabs_runs_on_webkit_like_dom():
    """Behavioural: renderTabs draws tabs and wires switch/close on old WebKit."""
    proc = subprocess.run(
        [shutil.which("node"), str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip(), f"harness produced no output (stderr: {proc.stderr})"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "renderTabs failed on a WebKit-like DOM: " + "; ".join(
        result["failures"]
    )
    assert ["switch", "s1"] in result["calls"]
    assert ["close", "s2"] in result["calls"]
    assert ["switch", "s2"] not in result["calls"]
