/*
 * Regression harness for zenvi-core #65.
 *
 * Extracts renderTabs() from src/chat_ui/chat.js and runs it under a fake DOM
 * that mimics old Qt WebKit: querySelectorAll() returns a NodeList-like object
 * WITHOUT `.forEach`, and elements have NO `.closest`. On the buggy code this
 * throws `TypeError: existing.forEach is not a function`; the fix must iterate
 * the NodeList with a plain loop and avoid Element.closest.
 *
 * Prints one JSON line to stdout and exits non-zero on any failure.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const CHAT_JS = path.join(__dirname, '..', '..', 'src', 'chat_ui', 'chat.js');

function extractFunction(src, name) {
    const start = src.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('could not find function ' + name);
    const open = src.indexOf('{', start);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') {
            depth--;
            if (depth === 0) return src.slice(start, i + 1);
        }
    }
    throw new Error('unbalanced braces for ' + name);
}

// ---- fake DOM -------------------------------------------------------------

function makeEl(tag) {
    const el = {
        tagName: String(tag || 'div').toUpperCase(),
        _cls: [],
        _attrs: {},
        _kids: [],
        _listeners: {},
        style: {},
        _html: '',
        parentNode: null,
        _closeChild: null,
    };
    el.classList = {
        add: function () { for (let i = 0; i < arguments.length; i++) if (el._cls.indexOf(arguments[i]) < 0) el._cls.push(arguments[i]); },
        remove: function () { for (let i = 0; i < arguments.length; i++) { const k = el._cls.indexOf(arguments[i]); if (k >= 0) el._cls.splice(k, 1); } },
        contains: function (c) { return el._cls.indexOf(c) >= 0; },
        toggle: function (c, f) { if (f === undefined) f = !el.classList.contains(c); if (f) el.classList.add(c); else el.classList.remove(c); return f; },
    };
    Object.defineProperty(el, 'className', {
        get: function () { return el._cls.join(' '); },
        set: function (v) { el._cls = String(v == null ? '' : v).split(/\s+/).filter(Boolean); },
    });
    Object.defineProperty(el, 'innerHTML', {
        get: function () { return el._html; },
        set: function (v) { el._html = String(v == null ? '' : v); },
    });
    el.setAttribute = function (k, v) { el._attrs[k] = String(v); };
    el.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(el._attrs, k) ? el._attrs[k] : null; };
    el.addEventListener = function (t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); };
    el.appendChild = function (c) { c.parentNode = el; el._kids.push(c); return c; };
    el.insertBefore = function (c, ref) {
        c.parentNode = el;
        const i = el._kids.indexOf(ref);
        if (i < 0) el._kids.push(c); else el._kids.splice(i, 0, c);
        return c;
    };
    el.removeChild = function (c) { const i = el._kids.indexOf(c); if (i >= 0) el._kids.splice(i, 1); return c; };
    el.remove = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.querySelector = function (sel) {
        if (sel === '.chat-tab-close') {
            const m = /data-close-id="([^"]*)"/.exec(el._html || '');
            const c = makeEl('button');
            c.classList.add('chat-tab-close');
            if (m) c.setAttribute('data-close-id', m[1]);
            c.parentNode = el;
            el._closeChild = c;
            return c;
        }
        return null;
    };
    el.querySelectorAll = function (sel) {
        let matches = [];
        if (sel === '.chat-tab') {
            matches = el._kids.filter(function (k) { return k.classList && k.classList.contains('chat-tab'); });
        }
        // NodeList-like: indexable + length, but deliberately NO `.forEach`
        // (the behaviour of the QtWebKit build this bug is about).
        const nl = { length: matches.length, item: function (i) { return matches[i] || null; } };
        for (let i = 0; i < matches.length; i++) nl[i] = matches[i];
        return nl;
    };
    el.dispatchEvent = function (ev) {
        const ls = (el._listeners[ev.type] || []).slice();
        for (let i = 0; i < ls.length; i++) {
            if (ev._stop) break;
            ls[i].call(el, ev);
        }
    };
    return el;
}

function clickEvent(target) {
    return { type: 'click', target: target, _stop: false, stopPropagation: function () { this._stop = true; }, preventDefault: function () {} };
}

// ---- build renderTabs in an isolated scope ------------------------------

const src = fs.readFileSync(CHAT_JS, 'utf8');
const renderTabsSrc = extractFunction(src, 'renderTabs');

const document = { createElement: makeEl, querySelector: function () { return null; }, getElementById: function () { return null; } };
const tabBarEl = makeEl('div');
const tabAddBtn = makeEl('button');
tabAddBtn.setAttribute('id', 'chat-tab-add');
tabBarEl.appendChild(tabAddBtn);

const currentTabs = [];
const unreadSessions = {};
let activeSessionId = '';
const backendSelect = null;
const escapeHtml = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
const updateCliEmptyState = function () {};
const applyBackendChrome = function () {};

const calls = [];
const bridge = {
    switchSession: function (id) { calls.push(['switch', id]); },
    closeSession: function (id) { calls.push(['close', id]); },
    createSession: function () { calls.push(['create'].concat(Array.prototype.slice.call(arguments))); },
};
const getBridge = function (cb) { cb(bridge); };

const renderTabs = new Function(
    'document', 'tabBarEl', 'tabAddBtn', 'currentTabs', 'unreadSessions',
    'activeSessionId', 'backendSelect', 'escapeHtml', 'updateCliEmptyState',
    'applyBackendChrome', 'getBridge',
    renderTabsSrc + '\nreturn renderTabs;'
)(document, tabBarEl, tabAddBtn, currentTabs, unreadSessions, activeSessionId,
  backendSelect, escapeHtml, updateCliEmptyState, applyBackendChrome, getBridge);

// ---- assertions --------------------------------------------------------

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

try {
    currentTabs.push({ id: 's1', title: 'First chat', active: false, backend: 'zenvi' });
    currentTabs.push({ id: 's2', title: 'Second chat', active: true, backend: 'zenvi', processing: true });

    renderTabs();
    renderTabs(); // second pass must clear the first pass's buttons, not stack

    const tabs = tabBarEl._kids.filter(function (k) { return k.classList.contains('chat-tab'); });
    check(tabs.length === 2, 'expected 2 rendered .chat-tab buttons, got ' + tabs.length);
    check(
        tabBarEl._kids.indexOf(tabs[0]) < tabBarEl._kids.indexOf(tabAddBtn),
        'tabs must be inserted before the + (chat-tab-add) button'
    );
    check(tabs[0].getAttribute('data-session-id') === 's1', 'first tab data-session-id');
    check(tabs[1].getAttribute('data-session-id') === 's2', 'second tab data-session-id');
    check(tabs[1].classList.contains('active'), 'active tab gets .active class');

    // click a tab body -> switchSession
    tabs[0].dispatchEvent(clickEvent(tabs[0]));
    check(calls.some(function (c) { return c[0] === 'switch' && c[1] === 's1'; }), 'clicking a tab calls bridge.switchSession(id)');

    // click the close control -> closeSession, and the tab handler must NOT switch
    const close2 = tabs[1]._closeChild || tabs[1].querySelector('.chat-tab-close');
    close2.dispatchEvent(clickEvent(close2));
    tabs[1].dispatchEvent(clickEvent(close2)); // tab handler sees a close-button target
    check(calls.some(function (c) { return c[0] === 'close' && c[1] === 's2'; }), 'clicking close calls bridge.closeSession(id)');
    check(!calls.some(function (c) { return c[0] === 'switch' && c[1] === 's2'; }), 'close click must not also switch to that session');
} catch (err) {
    failures.push('threw: ' + (err && err.stack || err));
}

process.stdout.write(JSON.stringify({ ok: failures.length === 0, failures: failures, calls: calls }) + '\n');
process.exit(failures.length === 0 ? 0 : 1);
