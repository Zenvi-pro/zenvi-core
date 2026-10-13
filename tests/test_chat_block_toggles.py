"""zenvi-core #314 — each Thinking / tool dropdown toggles itself, in any turn.

Clicking the Thinking header of an older turn used to open or close the
newest turn's block, and the tool dropdowns of a finished turn could not be
opened at all: both click handlers looked up the *current* turn's state.

The real chat.js source runs under node against a minimal fake DOM.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHAT_JS = Path(__file__).resolve().parents[1] / "src" / "chat_ui" / "chat.js"


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


_HARNESS = r"""
function makeEl() {
    var el = { _cls: [], style: {}, children: [], _listeners: {}, parentNode: null };
    el.classList = {
        add: function () { for (var i = 0; i < arguments.length; i++) if (el._cls.indexOf(arguments[i]) < 0) el._cls.push(arguments[i]); },
        remove: function () { for (var i = 0; i < arguments.length; i++) { var k = el._cls.indexOf(arguments[i]); if (k >= 0) el._cls.splice(k, 1); } },
        contains: function (c) { return el._cls.indexOf(c) >= 0; },
        toggle: function (c) { var on = !el.classList.contains(c); if (on) el.classList.add(c); else el.classList.remove(c); return on; }
    };
    Object.defineProperty(el, 'className', {
        get: function () { return el._cls.join(' '); },
        set: function (v) { el._cls = String(v).split(/\s+/).filter(Boolean); }
    });
    el.appendChild = function (c) { c.parentNode = el; el.children.push(c); return c; };
    el.setAttribute = function () {};
    el.querySelector = function () { return null; };
    el.addEventListener = function (t, fn) { el._listeners[t] = fn; };
    el.click = function () { el._listeners.click(); };
    return el;
}
var document = { createElement: makeEl };
var window = {};
var messagesEl = makeEl();
var TOOL_CHEVRON_SVG = '', ACTIVITY_SPINNER_SVG = '';
var thinkingBlockEl = null, thinkingBlockBody = null, thinkingBlockHeader = null;
var thinkingBlockCollapsed = false, thinkingBlockStartedAt = null, proseCommittedBelowThinking = false;
var firstAnswerTokenReceived = false, activityContainer = null, activitySteps = [], currentReasoningStep = null;
var toolBlocks = {}, lastEnterAt = 0, enterStagger = 0;
function scrollToBottomIfPinned() {}
function clearReasoningStep() {}
function escapeHtml(s) { return s; }

%s
%s

function open(el) { return el.classList.contains('expanded'); }

// Turn 1: a Thinking block with one tool that logged its args.
openThinkingBlock();
var oldThinking = thinkingBlockEl, oldHeader = thinkingBlockHeader, oldBody = thinkingBlockBody;
window.addToolBlock({ call_id: 't1', title: 'List files', args_detail: '{}' });
var oldTool = toolBlocks.t1;
setToolBlockExpanded(oldTool, false);

// End of turn 1, as setProcessing(false) leaves it: collapsed, state dropped.
oldThinking.classList.remove('expanded');
oldBody.style.display = 'none';
thinkingBlockEl = thinkingBlockBody = thinkingBlockHeader = activityContainer = null;
toolBlocks = {};

// Turn 2 is live and expanded.
openThinkingBlock();
var newThinking = thinkingBlockEl;

var out = {};
oldHeader.click();
out.oldOpened = open(oldThinking) && oldBody.style.display === 'block';
out.newUntouched = open(newThinking);
oldHeader.click();
out.oldClosed = !open(oldThinking) && oldBody.style.display === 'none';
out.newStillUntouched = open(newThinking);

oldTool.header.click();
out.oldToolOpened = open(oldTool.el) && oldTool.body.style.display === 'block';
oldTool.header.click();
out.oldToolClosed = !open(oldTool.el);

// No turn running at all: the last turn's block still toggles.
thinkingBlockEl = thinkingBlockBody = thinkingBlockHeader = null;
oldHeader.click();
out.opensWhenIdle = open(oldThinking);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_older_turn_dropdowns_toggle_themselves():
    src = _CHAT_JS.read_text(encoding="utf-8")
    thinking = _between(src, "function openThinkingBlock(", "window.openThinkingBlock = openThinkingBlock;")
    tools = _between(src, "function staggerEntrance(", "/* A tool that failed because nobody")
    proc = subprocess.run(
        [shutil.which("node"), "-e", _HARNESS % (thinking, tools)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert all(out.values()), out
