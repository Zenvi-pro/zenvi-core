"""zenvi-core #141 — the chat keeps the newest reply in view (both web engines).

``scrollToBottomIfPinned`` in ``src/chat_ui/chat.js`` follows a reply only if
the list was at the bottom *before* it landed (sampled on scroll), so a tall
reply appended in one go is still shown. The pin must also reset when the
transcript is replaced: otherwise scrolling up in one chat and switching tabs
replays the other chat with its newest reply below the fold.

The real chat.js source runs under node against a fake scroll container.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHAT_JS = Path(__file__).resolve().parents[1] / "src" / "chat_ui" / "chat.js"


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i) + len(end)]


_HARNESS = r"""
var content = 0, top = 0, pending = false, listeners = [];
var messagesEl = {
    clientHeight: 400,
    get scrollHeight() { return Math.max(content, this.clientHeight); },
    get scrollTop() { return top; },
    set scrollTop(v) {
        var t = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight));
        if (t !== top) { top = t; pending = true; }
    },
    // Emptying the list clamps scrollTop to 0 without a dependable scroll event.
    set innerHTML(v) { content = 0; top = 0; },
    addEventListener: function (type, fn) { if (type === 'scroll') listeners.push(fn); }
};
var typingEl = null;
var window = { addEventListener: function () {} };
function flush() {   // the browser delivers scroll events later, between tasks
    if (pending) { pending = false; listeners.forEach(function (fn) { fn(); }); }
}
function append(h) { content += h; scrollToBottomIfPinned(); flush(); }
function fromBottom() { return messagesEl.scrollHeight - top - messagesEl.clientHeight; }

%s
%s

var out = {};
append(1000);                               // a tall reply lands while at the bottom
out.tallReply = fromBottom();
messagesEl.scrollTop = 0; flush();          // the user scrolls up to read
append(50);
out.scrolledUpTop = top;
window.clearMessages(); flush();            // switch to another tab: replay its transcript
append(100); append(1000);
out.afterReplay = fromBottom();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_newest_reply_stays_in_view_and_a_new_transcript_starts_pinned():
    src = _CHAT_JS.read_text(encoding="utf-8")
    pin = _slice(src, "function isPinnedToBottom(", "window.addEventListener('resize', scrollToBottomIfPinned);")
    clear = _slice(src, "window.clearMessages = function () {", "};")
    proc = subprocess.run(
        [shutil.which("node"), "-e", _HARNESS % (pin, clear)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["tallReply"] == 0
    # Scrolled away: a new message does not yank the reader back down.
    assert out["scrolledUpTop"] == 0
    assert out["afterReplay"] == 0
