/*
 * Behaviour harness for the assistant's tool rows in src/chat_ui/chat.js.
 *
 * Pulls the real functions out of chat.js and runs them on a small fake DOM,
 * so the checks exercise the shipped code rather than a copy of it. Prints one
 * JSON line to stdout and exits non-zero when a check fails.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const CHAT_JS = path.join(__dirname, '..', '..', 'src', 'chat_ui', 'chat.js');
const src = fs.readFileSync(CHAT_JS, 'utf8');

// ---- source extraction --------------------------------------------------

function sliceBlock(text, start) {
    const open = text.indexOf('{', start);
    let depth = 0;
    for (let i = open; i < text.length; i++) {
        if (text[i] === '{') depth++;
        else if (text[i] === '}') {
            depth--;
            if (depth === 0) return text.slice(start, i + 1);
        }
    }
    throw new Error('unbalanced braces at ' + start);
}

function fnSource(name) {          // function name(...) { ... }
    const start = src.indexOf('function ' + name + '(');
    return start < 0 ? '' : sliceBlock(src, start);
}

function assignSource(name) {      // window.name = function (...) { ... };
    const start = src.indexOf('window.' + name + ' = function');
    return start < 0 ? '' : sliceBlock(src, start) + ';';
}

function varSource(name) {         // var name = ...;
    const start = src.indexOf('var ' + name + ' =');
    return start < 0 ? '' : src.slice(start, src.indexOf(';', start) + 1);
}

const FUNCTIONS = [
    fnSource('setToolBlockExpanded'),
    fnSource('staggerEntrance'),
    fnSource('addSignInPrompt'),
    fnSource('fetchToolDetail'),
    fnSource('setToolDetailRow'),
    fnSource('showToolDetail'),
    fnSource('currentToolBatch'),
    fnSource('makeToolGroup'),
    fnSource('refreshToolGroup'),
    assignSource('addToolBlock'),
    assignSource('appendToolLog'),
    assignSource('completeToolBlock'),
    assignSource('replayToolBlock'),
    assignSource('appendThinkingText'),
];

const PRELUDE = [
    'var toolBlocks = {};',
    'var activityContainer = null;',
    'var lastEnterAt = 0, enterStagger = 0;',
    'var signInPrompts = [];',
    'var toolBatch = null;',
    'var modelSelect = { value: "" };',
    'var currentAgentMode = "agent";',
    'var ACTIVITY_SPINNER_SVG = "<svg class=\\"spin\\"></svg>";',
    'var ACTIVITY_CHECK_SVG = "<svg class=\\"check\\"></svg>";',
    'var ACTIVITY_X_SVG = "<svg class=\\"x\\"></svg>";',
    'var TOOL_CHEVRON_SVG = "<svg class=\\"chevron\\"></svg>";',
    varSource('AUTH_FAILURE_RE'),
    'function escapeHtml(s) { return String(s); }',
    'function scrollToBottomIfPinned() {}',
    'function clearReasoningStep() {}',
    'function openThinkingBlock() {}',
    'function setActivity(el) { activityContainer = el; }',
].join('\n');

// ---- fake DOM -----------------------------------------------------------

function FakeEl(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.attrs = {};
    this._cls = [];
    this._listeners = {};
    this._text = '';
    this._html = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    const el = this;
    this.classList = {
        add: function () {
            for (let i = 0; i < arguments.length; i++) {
                if (el._cls.indexOf(arguments[i]) < 0) el._cls.push(arguments[i]);
            }
        },
        remove: function () {
            for (let i = 0; i < arguments.length; i++) {
                const k = el._cls.indexOf(arguments[i]);
                if (k >= 0) el._cls.splice(k, 1);
            }
        },
        contains: function (c) { return el._cls.indexOf(c) >= 0; },
        toggle: function (c, force) {
            const on = force === undefined ? el._cls.indexOf(c) < 0 : !!force;
            if (on) el.classList.add(c); else el.classList.remove(c);
            return on;
        },
    };
}

Object.defineProperty(FakeEl.prototype, 'className', {
    get: function () { return this._cls.join(' '); },
    set: function (v) { this._cls = String(v == null ? '' : v).split(/\s+/).filter(Boolean); },
});

Object.defineProperty(FakeEl.prototype, 'textContent', {
    get: function () {
        return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    },
    set: function (v) { this.children = []; this._text = String(v == null ? '' : v); },
});

Object.defineProperty(FakeEl.prototype, 'innerHTML', {
    get: function () { return this._html; },
    set: function (v) {
        this.children = [];
        this._text = '';
        this._html = String(v == null ? '' : v);
        const re = /<span class="([^"]+)">([\s\S]*?)<\/span>/g;
        let m;
        while ((m = re.exec(this._html)) !== null) {
            const child = new FakeEl('span');
            child._cls = m[1].split(' ');
            child._html = m[2];
            child._text = m[2].replace(/<[^>]*>/g, '');
            this.appendChild(child);
        }
    },
});

Object.defineProperty(FakeEl.prototype, 'firstChild', {
    get: function () { return this.children[0] || null; },
});

Object.defineProperty(FakeEl.prototype, 'lastChild', {
    get: function () { return this.children[this.children.length - 1] || null; },
});

Object.defineProperty(FakeEl.prototype, 'isConnected', {
    get: function () {
        let node = this;
        while (node.parentNode) node = node.parentNode;
        return !!node.isRoot;
    },
});

FakeEl.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
FakeEl.prototype.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
FakeEl.prototype.addEventListener = function (type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
};
FakeEl.prototype.click = function () {
    const ev = { type: 'click', target: this, stopPropagation: function () {}, preventDefault: function () {} };
    (this._listeners.click || []).slice().forEach(function (fn) { fn.call(null, ev); });
};
FakeEl.prototype.detach = function (c) {
    if (c.parentNode) c.parentNode.removeChild(c);
};
FakeEl.prototype.appendChild = function (c) {
    this.detach(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
};
FakeEl.prototype.insertBefore = function (c, ref) {
    this.detach(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
};
FakeEl.prototype.removeChild = function (c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
};
FakeEl.prototype.remove = function () {
    if (this.parentNode) this.parentNode.removeChild(this);
};
FakeEl.prototype.querySelector = function (sel) {
    const hit = findFirst(this, function (n) {
        return sel.charAt(0) === '.' && n._cls.indexOf(sel.slice(1)) >= 0;
    });
    return hit;
};

function findFirst(node, pred) {
    for (let i = 0; i < node.children.length; i++) {
        const c = node.children[i];
        if (pred(c)) return c;
        const deeper = findFirst(c, pred);
        if (deeper) return deeper;
    }
    return null;
}

function findAll(node, pred, out) {
    out = out || [];
    node.children.forEach(function (c) {
        if (pred(c)) out.push(c);
        findAll(c, pred, out);
    });
    return out;
}

// ---- scenario builder ---------------------------------------------------

let pendingTasks = [];
function fakeSetTimeout(fn) { pendingTasks.push(fn); }

function scenario(opts) {
    opts = opts || {};
    const calls = [];
    const details = opts.details || {};
    const webkit = !!opts.webkit;

    const root = new FakeEl('body');
    root.isRoot = true;
    const documentElement = new FakeEl('html');
    if (webkit) documentElement.setAttribute('data-zenvi-webkit', '1');
    const document = {
        documentElement: documentElement,
        createElement: function (tag) { return new FakeEl(tag); },
    };
    const win = webkit ? {} : { qt: { webChannelTransport: {} } };
    const bridge = {
        toolDetail: webkit
            ? function (id) { calls.push(id); return JSON.stringify(details[id] || {}); }
            : function (id, cb) { calls.push(id); cb(JSON.stringify(details[id] || {})); },
    };
    const getBridge = function (cb) { cb(bridge); };

    const body = FUNCTIONS.join('\n') + '\nreturn {' +
        'addToolBlock: typeof addToolBlock === "function" ? addToolBlock : window.addToolBlock,' +
        'appendToolLog: window.appendToolLog, completeToolBlock: window.completeToolBlock,' +
        'replayToolBlock: window.replayToolBlock, appendThinkingText: window.appendThinkingText, setActivity: setActivity, toolBlocks: toolBlocks' +
        '};';
    const api = new Function('document', 'window', 'setTimeout', 'getBridge', PRELUDE + '\n' + body)(
        document, win, fakeSetTimeout, getBridge);

    const messages = new FakeEl('div');
    root.appendChild(messages);
    const activity = new FakeEl('div');
    messages.appendChild(activity);
    api.setActivity(activity);
    const runTasks = function () {
        const tasks = pendingTasks;
        pendingTasks = [];
        tasks.forEach(function (fn) { fn(); });
    };
    return { api: api, calls: calls, root: root, activity: activity, runTasks: runTasks };
}

function payload(callId, title, toolName, label) {
    return JSON.stringify({ call_id: callId, title: title, label: label || title, tool_name: toolName || '' });
}

function rowOf(s, callId) {
    return findFirst(s.root, function (n) { return n.getAttribute && n.getAttribute('data-call-id') === callId; });
}

function header(row) { return row.children[0]; }
function body(row) { return row.children[1]; }

function textHas(node, needle) {
    return !!node && node.textContent.indexOf(needle) >= 0;
}

function groupRows(s) {
    return findAll(s.root, function (n) { return n.classList.contains('chat-tool-group'); });
}

function signInRows(row) {
    return findAll(row, function (n) { return n.classList.contains('chat-signin-row'); }).length;
}

// ---- checks -------------------------------------------------------------

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

const DETAIL_C1 = {
    args: '{\n  "file_id": "/m/a.mp4"\n}',
    result: 'Added a.mp4 to track 1',
};

try {
    // A live row starts with a title and nothing else.
    let s = scenario({ details: { c1: DETAIL_C1 } });
    s.api.addToolBlock(payload('c1', 'Add clip to timeline: a.mp4 on track 1', 'add_clip_to_timeline_tool'));
    let row = rowOf(s, 'c1');
    check(!!row, 'a tool call creates a row in the activity log');
    check(!!row && row.classList.contains('has-logs'), 'a live row is expandable, so its details can load on click');
    check(s.calls.length === 0, 'no details are requested before the row is expanded');
    check(!!row && !textHas(body(row), 'ARGS'), 'no args are rendered before the row is expanded');

    // Expanding asks once and shows args and the result; collapsing does not ask again.
    s.api.completeToolBlock('c1', true, 'Added a.mp4 to track 1');
    header(row).click();
    check(s.calls.join(',') === 'c1', 'expanding asks the bridge for that call, once');
    check(!!row && row.classList.contains('expanded') && body(row).style.display === 'block', 'the row opens');
    check(!!row && textHas(body(row), 'ARGS:') && textHas(body(row), '"file_id"'), 'args are shown after expanding');
    check(!!row && textHas(body(row), 'RESULT:') && textHas(body(row), 'Added a.mp4'), 'the result is shown after expanding');
    header(row).click();
    check(!!row && body(row).style.display === 'none', 'a second click closes the row');
    check(s.calls.length === 1, 'closing a row does not ask the bridge again');

    // A finished row shows its one-line summary and does not dump the result.
    s = scenario({});
    s.api.addToolBlock(payload('c2', 'Mute clips', 'mute_clips_tool'));
    s.api.completeToolBlock('c2', true, 'Muted 2 clips');
    row = rowOf(s, 'c2');
    const cmd = header(row).querySelector('.chat-tool-cmd');
    check(!!cmd && cmd.textContent === 'Muted 2 clips', 'a finished row shows the one-line summary');
    check(!textHas(body(row), 'RESULT'), 'finishing a row adds no result text to its body');

    // Sign in is offered only when the failed call's own result says so.
    s = scenario({ details: {
        c3: { args: '{}', result: 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.' },
        c4: { args: '{}', result: 'Error: clip not found' },
    } });
    s.api.addToolBlock(payload('c3', 'Generate image', 'generate_image_tool'));
    s.api.addToolBlock(payload('c4', 'Add clip to timeline', 'add_clip_to_timeline_tool'));
    s.api.completeToolBlock('c3', false, 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.');
    s.api.completeToolBlock('c4', false, 'Error: clip not found');
    check(signInRows(rowOf(s, 'c3')) === 1, 'a failed call whose result needs sign-in gets a Sign in row');
    check(signInRows(rowOf(s, 'c4')) === 0, 'a failed call with an unrelated error gets no Sign in row');

    // Another row's log line about login never gives this failed row a Sign in button.
    s = scenario({ details: { c9: { args: '{}', result: 'Error: file not found' } } });
    s.api.addToolBlock(payload('c8', 'Export video', 'export_video_tool'));
    s.api.addToolBlock(payload('c9', 'Add clip to timeline', 'add_clip_to_timeline_tool'));
    s.api.appendToolLog('c8', 'Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.');
    s.api.completeToolBlock('c9', false, 'Error: file not found');
    check(signInRows(rowOf(s, 'c9')) === 0, 'a login line in another row does not give this failed row a Sign in button');

    // Calls of one tool that start in the same batch share one group row.
    s = scenario({});
    s.api.addToolBlock(payload('g1', 'Add clip to timeline: a.mp4 on track 1', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    s.api.addToolBlock(payload('g2', 'Add clip to timeline: b.mp4 on track 1', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    let groups = groupRows(s);
    check(groups.length === 1, 'two calls of one tool in one batch share one group row');
    const group = groups[0];
    check(!!group && textHas(group.children[0], 'Add clip to timeline ×2'), 'the group row reads "Add clip to timeline ×2"');
    check(!!group && rowOf(s, 'g1').parentNode === group.children[1] && rowOf(s, 'g2').parentNode === group.children[1],
        'both rows sit inside the group body');
    check(!!group && group.children[1].style.display === 'none', 'the group starts collapsed');
    if (group) group.children[0].click();
    check(!!group && group.children[1].style.display === 'block', 'clicking the group header opens it');
    if (group) group.children[0].click();
    check(!!group && group.children[1].style.display === 'none', 'clicking it again closes it');
    s.api.completeToolBlock('g1', true, 'Added a.mp4 to track 1');
    check(!!group && group.classList.contains('running'), 'the group spins while any call is still running');
    s.api.completeToolBlock('g2', false, 'Error: file not found');
    check(!!group && !group.classList.contains('running') && group.classList.contains('error'),
        'the group shows an error once nothing is running and one call failed');

    // A thought between two calls of one tool keeps them apart, in order.
    s = scenario({});
    s.api.addToolBlock(payload('t1', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    s.api.appendThinkingText('Now the second shot.');
    s.api.addToolBlock(payload('t2', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    check(groupRows(s).length === 0, 'calls with a thought between them are not grouped');
    check(s.activity.children.length === 3 && s.activity.children[2] === rowOf(s, 't2'),
        'the thought stays between the two calls');

    // A grouped call that needs sign-in puts Sign in on the group row, where it can be seen.
    s = scenario({ details: {
        s1: { args: '{}', result: 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.' },
        s2: { args: '{}', result: 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.' },
    } });
    s.api.addToolBlock(payload('s1', 'Generate image', 'generate_image_tool', 'Generate image'));
    s.api.addToolBlock(payload('s2', 'Generate image', 'generate_image_tool', 'Generate image'));
    s.api.completeToolBlock('s1', false, 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.');
    s.api.completeToolBlock('s2', false, 'Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.');
    groups = groupRows(s);
    check(groups.length === 1 && signInRows(groups[0]) === 1 && signInRows(groups[0].children[1]) === 0,
        'a grouped sign-in failure shows one Sign in row on the group, outside its collapsed body');

    s = scenario({});
    s.api.addToolBlock(payload('h1', 'Mute clips', 'mute_clips_tool', 'Mute clips'));
    s.api.addToolBlock(payload('h2', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    check(groupRows(s).length === 0, 'different tools in one batch are not grouped');

    s = scenario({});
    s.api.addToolBlock(payload('k1', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    s.runTasks();
    s.api.addToolBlock(payload('k2', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    check(groupRows(s).length === 0, 'calls from different batches stay separate rows');

    s = scenario({});
    s.api.replayToolBlock(JSON.stringify({ call_id: 'r1', title: 'Add clip to timeline', tool_name: 'add_clip_to_timeline_tool' }), true);
    s.api.replayToolBlock(JSON.stringify({ call_id: 'r2', title: 'Add clip to timeline', tool_name: 'add_clip_to_timeline_tool' }), true);
    check(groupRows(s).length === 0, 'restored rows from history are never grouped');

    // The same call announced twice in one batch stays one row, never a group of itself.
    s = scenario({});
    s.api.addToolBlock(payload('d1', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    s.api.addToolBlock(payload('d1', 'Add clip to timeline', 'add_clip_to_timeline_tool', 'Add clip to timeline'));
    check(groupRows(s).length === 0 && findAll(s.root, function (n) { return n.getAttribute && n.getAttribute('data-call-id') === 'd1'; }).length === 1,
        'a repeated announcement of one call keeps a single row');

    // Old WebKit pages call the bridge synchronously and get the detail back at once.
    s = scenario({ webkit: true, details: { w1: { args: '{"track": 2}', result: 'Muted 1 clip' } } });
    s.api.addToolBlock(payload('w1', 'Mute clips', 'mute_clips_tool', 'Mute clips'));
    s.api.completeToolBlock('w1', true, 'Muted 1 clip');
    header(rowOf(s, 'w1')).click();
    check(s.calls.join() === 'w1' && textHas(body(rowOf(s, 'w1')), '"track": 2') && textHas(body(rowOf(s, 'w1')), 'Muted 1 clip'),
        'WebKit pages get the args and result synchronously on expand');

    // Reasoning text streams into the open Thinking block, in order with its tool rows.
    s = scenario({});
    s.api.appendThinkingText('Checking ');
    s.api.appendThinkingText('the timeline');
    let thoughts = findAll(s.activity, function (n) { return n.classList.contains('chat-thinking-text'); });
    check(thoughts.length === 1 && thoughts[0].textContent === 'Checking the timeline', 'streamed reasoning joins into one text block');
    s.api.addToolBlock(payload('t1', 'List clips', 'list_clips_tool', 'List clips'));
    s.api.appendThinkingText('Next');
    thoughts = findAll(s.activity, function (n) { return n.classList.contains('chat-thinking-text'); });
    check(thoughts.length === 2 && thoughts[1].textContent === 'Next', 'text after a tool row starts a new block below it');
    check(s.activity.lastChild === thoughts[1], 'the new reasoning block comes after the tool row, in order');

    // Progress lines still stream into the row, and a restored row is not expandable.
    s = scenario({});
    s.api.addToolBlock(payload('c6', 'Export video', 'export_video_tool'));
    s.api.appendToolLog('c6', 'Rendering 40%');
    row = rowOf(s, 'c6');
    check(textHas(body(row), 'Rendering 40%'), 'progress lines reach the row');
    check(row.classList.contains('has-logs'), 'a row with progress is expandable');
    s.api.replayToolBlock(JSON.stringify({ call_id: 'c7', title: 'Add clip to timeline', tool_name: 'add_clip_to_timeline_tool' }), true);
    check(!rowOf(s, 'c7').classList.contains('has-logs'), 'a restored row from history is not expandable');
} catch (err) {
    failures.push('threw: ' + (err && err.stack || err));
}

process.stdout.write(JSON.stringify({ ok: failures.length === 0, failures: failures }) + '\n');
process.exit(failures.length === 0 ? 0 : 1);
