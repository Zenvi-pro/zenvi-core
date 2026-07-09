/**
 * Zenvi Assistant chat – CEP/WebEngine front-end.
 * Communicates with Python via QWebChannel (window.zenviChatBridge).
 */

(function () {
    'use strict';

    const bridgeName = 'zenviChatBridge';
    const qwebchannelUrl = 'qwebchannel.js';

    function getBridge(cb) {
        // Qt WebKit: Python exposes the bridge via addToJavaScriptWindowObject (no qt.webChannelTransport).
        if (window[bridgeName]) {
            cb(window[bridgeName]);
            return;
        }
        if (window.QWebChannel && window.qt && window.qt.webChannelTransport) {
            new window.QWebChannel(window.qt.webChannelTransport, function (ch) {
                window[bridgeName] = ch.objects[bridgeName];
                cb(window[bridgeName] || null);
            });
            return;
        }
        setTimeout(function () { getBridge(cb); }, 50);
    }

    const preambleEl = document.getElementById('chat-preamble-label');
    const preambleStatus = document.getElementById('chat-preamble-status');
    const modelSelect = document.getElementById('chat-model-select');
    const modelTrigger = document.getElementById('chat-model-trigger');
    const modelLabel = document.getElementById('chat-model-label');
    const modelMenu = document.getElementById('chat-model-menu');
    // Move menu to <body> so it escapes any CSS transform on ancestor elements
    // (transform creates a new containing block that breaks position:fixed)
    document.body.appendChild(modelMenu);
    const messagesEl = document.getElementById('chat-messages');
    const inputEl = document.getElementById('chat-input');
    const inputRow = document.getElementById('chat-input-row');
    const glowWrap = document.getElementById('chat-input-glow-wrap');
    const inputOverlay = document.getElementById('chat-input-overlay');
    const sendBtn = document.getElementById('chat-send-btn');
    const cancelBtn = document.getElementById('chat-cancel-btn');
    const clearBtn = document.getElementById('chat-clear-btn');
    const modePlanBtn = document.getElementById('chat-mode-plan');
    const modeAgentBtn = document.getElementById('chat-mode-agent');
    var currentAgentMode = 'agent';
    const inputRowEl = document.getElementById('chat-input-row');
    const chatContainer = document.querySelector('.chat-container');

    // ── Command palette state ("/" commands) ─────────────────────────────
    var commandPaletteEl = null;
    var commandPaletteOpen = false;
    var commandActiveIndex = -1;
    var commandQuery = '';
    var COMMANDS = [
        { prefix: '/add-track', label: 'Add track', description: 'Add a new track to the timeline' },
        { prefix: '/generate', label: 'Generate video', description: 'Generate a new AI video clip from a text prompt (Kling O1 Pro, default 5s)' },
        { prefix: '/split', label: 'Split clip', description: 'Split a timeline clip at the playhead (name the clip in chat or scrub to it first)' },
        { prefix: '/export', label: 'Export', description: 'Export the current project (choose preset)' },
        { prefix: '/caption', label: 'Generate captions', description: 'Generate captions for a timeline clip (describe which clip)' },
        { prefix: '/transition', label: 'Transition', description: 'Generate a transition between two clips (describe both clips)' }
    ];

    function ensureCommandPaletteEl() {
        if (commandPaletteEl) return commandPaletteEl;
        // Mount inside the input glow inner card so it's clipped correctly and
        // positioned relative to the input area (no fixed positioning issues).
        var inner = document.querySelector('.chat-input-glow-inner');
        if (!inner) return null;
        var el = document.createElement('div');
        el.id = 'chat-command-palette';
        el.className = 'chat-command-palette';
        el.setAttribute('role', 'listbox');
        el.setAttribute('aria-label', 'Command suggestions');
        el.style.display = 'none';
        inner.appendChild(el);
        commandPaletteEl = el;
        return el;
    }

    function getCommandMatches(query) {
        var q = (query || '').toLowerCase();
        if (!q) return COMMANDS.slice();
        return COMMANDS.filter(function (cmd) {
            return (cmd.prefix || '').toLowerCase().indexOf(q) === 0
                || (cmd.label || '').toLowerCase().indexOf(q) !== -1;
        });
    }

    function openCommandPalette(query) {
        var el = ensureCommandPaletteEl();
        if (!el) return;
        commandPaletteOpen = true;
        commandQuery = query || '';
        renderCommandPalette();
        el.style.display = 'block';
    }

    function closeCommandPalette() {
        if (!commandPaletteEl) return;
        commandPaletteOpen = false;
        commandActiveIndex = -1;
        commandQuery = '';
        commandPaletteEl.style.display = 'none';
        commandPaletteEl.innerHTML = '';
    }

    function renderCommandPalette() {
        var el = ensureCommandPaletteEl();
        if (!el) return;

        var matches = getCommandMatches(commandQuery);
        if (!matches.length) {
            // Keep palette visible but show a small empty state.
            el.innerHTML = '<div class="chat-command-empty">No commands</div>';
            commandActiveIndex = -1;
            return;
        }

        if (commandActiveIndex < 0 || commandActiveIndex >= matches.length) {
            // Try to preselect best match (exact prefix start)
            var exact = matches.findIndex(function (cmd) { return cmd.prefix === commandQuery; });
            commandActiveIndex = exact >= 0 ? exact : 0;
        }

        var html = '';
        for (var i = 0; i < matches.length; i++) {
            var cmd = matches[i];
            var active = i === commandActiveIndex;
            html += '<button type="button" class="chat-command-item' + (active ? ' active' : '') + '"'
                + ' role="option" aria-selected="' + (active ? 'true' : 'false') + '"'
                + ' data-index="' + i + '">'
                +   '<span class="chat-command-prefix">' + escapeHtml(cmd.prefix) + '</span>'
                +   '<span class="chat-command-label">' + escapeHtml(cmd.label) + '</span>'
                +   '<span class="chat-command-desc">' + escapeHtml(cmd.description) + '</span>'
                + '</button>';
        }
        el.innerHTML = html;

        // Click handler (delegated)
        el.onclick = function (ev) {
            var target = ev.target;
            var btn = target && target.closest ? target.closest('.chat-command-item') : null;
            if (!btn) return;
            var idx = parseInt(btn.getAttribute('data-index') || '-1', 10);
            if (!isNaN(idx)) {
                selectCommandIndex(idx);
            }
        };
    }

    function selectCommandIndex(idx) {
        var matches = getCommandMatches(commandQuery);
        if (!matches.length) return;
        var safeIdx = Math.max(0, Math.min(idx, matches.length - 1));
        var cmd = matches[safeIdx];
        if (!cmd) return;
        inputEl.value = cmd.prefix + ' ';
        closeCommandPalette();
        // Trigger input side-effects (e.g. hide overlay)
        try {
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        } catch (e) {}
        adjustTextareaHeight();
        inputEl.focus();
    }

    function maybeUpdateCommandPaletteFromValue(val) {
        var v = (val || '');
        // Only show when starts with "/" and no space yet
        if (v.charAt(0) === '/' && v.indexOf(' ') === -1) {
            openCommandPalette(v.trim());
            return;
        }
        closeCommandPalette();
    }

    function adjustTextareaHeight() {
        if (!inputEl) return;
        try {
            // reset to measure scrollHeight accurately
            inputEl.style.height = '0px';
            var minPx = 60;
            var maxPx = 200;
            var next = Math.max(minPx, Math.min(inputEl.scrollHeight, maxPx));
            inputEl.style.height = next + 'px';
        } catch (e) {}
    }

    var processingStartTime = null;
    var lastRunTimestamp = null;
    var lastThoughtSec = null;
    var statusInterval = null;

    var activityContainer = null;
    var activitySteps = [];
    var toolBlocks = {}; // call_id -> { el, body, header, lines: [] }
    var currentReasoningStep = null; // legacy; kept for compat
    var enterStagger = 0;   // index within the current entrance burst
    var lastEnterAt = 0;    // timestamp of the last staggered tool-block entrance

    // Cursor-style collapsible thinking block (tool activity lives inside)
    var thinkingBlockEl = null;
    var thinkingBlockBody = null;
    var thinkingBlockHeader = null;
    var thinkingBlockCollapsed = false;
    var firstAnswerTokenReceived = false;

    var ACTIVITY_SPINNER_SVG = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none">' +
        '<circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.2" stroke-dasharray="16 16" stroke-linecap="round"/></svg>';

    var ACTIVITY_CHECK_SVG = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none">' +
        '<path d="M3.5 7.5l2.5 2L10.5 4.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    var ACTIVITY_X_SVG = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none">' +
        '<path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';

    var TOOL_CHEVRON_SVG = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none">' +
        '<path d="M3.5 2L6.5 5l-3 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const SUGGESTED_PROMPTS = 'List my files · Add a track · Export video · Undo';
    let typingInterval = null;
    let typingIndex = 0;
    let overlayVisible = true;

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    function removePlaceholder() {
        const ph = messagesEl.querySelector('.chat-placeholder');
        if (ph) ph.remove();
    }

    function isPinnedToBottom(el, threshold) {
        threshold = threshold || 80;
        return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    }

    function scrollToBottomIfPinned() {
        if (messagesEl && isPinnedToBottom(messagesEl)) {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    }

    function openThinkingBlock() {
        if (thinkingBlockEl) return;
        thinkingBlockCollapsed = false;
        firstAnswerTokenReceived = false;
        thinkingBlockEl = document.createElement('div');
        thinkingBlockEl.className = 'chat-thinking-block expanded';
        thinkingBlockHeader = document.createElement('button');
        thinkingBlockHeader.type = 'button';
        thinkingBlockHeader.className = 'chat-thinking-header';
        thinkingBlockHeader.innerHTML =
            '<span class="chat-thinking-chevron">' + TOOL_CHEVRON_SVG + '</span>' +
            '<span class="chat-thinking-title">Thinking…</span>';
        thinkingBlockHeader.addEventListener('click', function () {
            if (!thinkingBlockEl) return;
            var expanded = thinkingBlockEl.classList.toggle('expanded');
            if (thinkingBlockBody) {
                thinkingBlockBody.style.display = expanded ? 'block' : 'none';
            }
        });
        thinkingBlockBody = document.createElement('div');
        thinkingBlockBody.className = 'chat-thinking-body';
        thinkingBlockBody.style.display = 'block';
        thinkingBlockEl.appendChild(thinkingBlockHeader);
        thinkingBlockEl.appendChild(thinkingBlockBody);
        messagesEl.appendChild(thinkingBlockEl);
        activityContainer = document.createElement('div');
        activityContainer.className = 'chat-activity-log';
        activityContainer.setAttribute('aria-live', 'polite');
        thinkingBlockBody.appendChild(activityContainer);
        activitySteps = [];
        currentReasoningStep = null;
        scrollToBottomIfPinned();
    }

    window.openThinkingBlock = openThinkingBlock;

    function collapseThinkingBlock(elapsedMs) {
        if (!thinkingBlockEl || thinkingBlockCollapsed) return;
        thinkingBlockCollapsed = true;
        var sec = Math.round((elapsedMs || 0) / 1000);
        var title = thinkingBlockHeader && thinkingBlockHeader.querySelector('.chat-thinking-title');
        if (title) {
            title.textContent = 'Thought for ' + (sec < 1 ? '<1' : sec) + 's';
        }
        thinkingBlockEl.classList.remove('expanded');
        if (thinkingBlockBody) thinkingBlockBody.style.display = 'none';
        clearReasoningStep();
        lastThoughtSec = sec;
        scrollToBottomIfPinned();
    }

    window.collapseThinkingBlock = collapseThinkingBlock;

    function setInputIdle(idle) {
        const container = document.querySelector('.chat-container');
        if (!container) return;
        if (idle) container.classList.add('chat-input-idle');
        else container.classList.remove('chat-input-idle');
    }

    function syncTextareaMaskForOverlay() {
        if (!inputEl) return;
        inputEl.style.color = '';
        inputEl.style.caretColor = '';
        try { inputEl.style.removeProperty('-webkit-text-fill-color'); } catch (e) {}
        inputEl.removeAttribute('readonly');
        inputEl.style.pointerEvents = '';
        if (chatContainer) chatContainer.classList.remove('chat-input-overlay-active');
    }

    function hideOverlay() {
        if (inputOverlay) inputOverlay.classList.add('hidden');
        syncTextareaMaskForOverlay();
        if (inputEl) inputEl.focus();
    }

    function exitIdle() {
        setInputIdle(false);
    }

    window.appendMessage = function (role, bodyHtml, isAssistant) {
        removePlaceholder();
        // If a streaming bubble for this turn exists and the caller is now
        // delivering the finalised assistant message, replace its body with
        // the proper markdown HTML instead of appending a duplicate.
        if (isAssistant && streamingMessageEl) {
            var body = streamingMessageEl.querySelector('.chat-message-body');
            if (body) body.innerHTML = bodyHtml;
            streamingMessageEl.classList.remove('chat-message-streaming');
            streamingMessageEl = null;
            streamingBuffer = '';
            streamMdEl = null;
            scrollToBottomIfPinned();
            return;
        }
        const div = document.createElement('div');
        var cls = 'chat-message chat-message-enter ';
        if (role === 'user') cls += 'chat-message-user ';
        if (role === 'system') cls += 'chat-message-system ';
        div.className = cls;
        if (role === 'system') {
            div.innerHTML = '<div class="chat-message-body">' + '<p>' + bodyHtml + '</p>' + '</div>';
        } else {
            div.innerHTML = '<div class="chat-message-body">' + (isAssistant ? bodyHtml : '<p>' + bodyHtml + '</p>') + '</div>';
        }
        messagesEl.appendChild(div);
        scrollToBottomIfPinned();
    };

    // ── Streaming-token rendering ──────────────────────────────────────────
    var streamingMessageEl = null;
    var streamingBuffer = '';
    var streamFlushScheduled = false;
    var streamMdEl = null;

    function escapeHtmlForStream(s) {
        return s.replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
    }

    function lightMarkdown(text) {
        var s = escapeHtmlForStream(text);
        s = s.replace(/```([\s\S]*?)```/g, function (_, code) {
            return '<pre><code>' + code + '</code></pre>';
        });
        s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/\n/g, '<br/>');
        return s;
    }

    function flushStreamingBuffer() {
        streamFlushScheduled = false;
        if (!streamingMessageEl) return;
        var body = streamingMessageEl.querySelector('.chat-message-body');
        if (!body) return;
        if (!streamMdEl) {
            body.innerHTML = '<div class="stream-md"></div>';
            streamMdEl = body.querySelector('.stream-md');
        }
        if (streamMdEl) streamMdEl.innerHTML = lightMarkdown(streamingBuffer);
        scrollToBottomIfPinned();
    }

    window.resetStreamingMessage = function () {
        if (streamingMessageEl && streamingMessageEl.parentNode) {
            streamingMessageEl.remove();
        }
        streamingMessageEl = null;
        streamingBuffer = '';
        streamMdEl = null;
        streamFlushScheduled = false;
    };

    window.appendOrUpdateStreamingMessage = function (text) {
        if (!text) return;
        removePlaceholder();
        if (!firstAnswerTokenReceived) {
            firstAnswerTokenReceived = true;
            clearReasoningStep();
            var elapsed = processingStartTime ? (Date.now() - processingStartTime) : 0;
            collapseThinkingBlock(elapsed);
        }
        if (!streamingMessageEl) {
            streamingMessageEl = document.createElement('div');
            streamingMessageEl.className = 'chat-message chat-message-enter chat-message-streaming';
            streamingMessageEl.innerHTML = '<div class="chat-message-body"></div>';
            messagesEl.appendChild(streamingMessageEl);
            streamingBuffer = '';
            streamMdEl = null;
        }
        streamingBuffer += text;
        if (!streamFlushScheduled) {
            streamFlushScheduled = true;
            requestAnimationFrame(flushStreamingBuffer);
        }
    };

    window.finalizeStreamingMessage = function () {
        if (streamFlushScheduled) {
            flushStreamingBuffer();
        }
        if (streamingMessageEl) {
            streamingMessageEl.classList.remove('chat-message-streaming');
        }
    };

    /* ── Activity log helpers (tool step display during processing) ── */

    // Number of tool blocks still spinning. The DOM is the single source of
    // truth so dedupe / unknown-id handling can never desync a counter.
    function runningToolCount() {
        var n = 0;
        for (var k in toolBlocks) {
            if (toolBlocks.hasOwnProperty(k) && toolBlocks[k] &&
                toolBlocks[k].el && toolBlocks[k].el.classList.contains('running')) {
                n++;
            }
        }
        return n;
    }

    // Idempotent: reasoning spinner retired — thinking block replaces it.
    function ensureReasoningStep() {
        return;
    }

    // Remove the live reasoning placeholder (used when a tool starts — the
    // agent is no longer "just thinking").
    function clearReasoningStep() {
        if (!currentReasoningStep) return;
        var idx = activitySteps.indexOf(currentReasoningStep);
        if (idx !== -1) activitySteps.splice(idx, 1);
        if (currentReasoningStep.parentNode) currentReasoningStep.remove();
        currentReasoningStep = null;
    }

    // Back-compat alias for the older activity API / any external callers.
    function addReasoningStep() { ensureReasoningStep(); }

    function completeActivityStep(step) {
        if (!step) return;
        step.classList.remove('running');
        step.classList.add('done');
        var icon = step.querySelector('.activity-icon');
        if (icon) icon.innerHTML = ACTIVITY_CHECK_SVG;
        // Remove animated dots class from reasoning labels when completed
        var label = step.querySelector('.activity-reasoning');
        if (label) label.classList.remove('activity-reasoning');
    }

    window.addActivityStep = function (label, detail) {
        if (!activityContainer) openThinkingBlock();
        if (!activityContainer) return;
        // A tool is starting — dismiss the live reasoning placeholder.
        clearReasoningStep();
        // Complete current step (reasoning or previous tool)
        if (activitySteps.length > 0) {
            completeActivityStep(activitySteps[activitySteps.length - 1]);
        }
        // Add new tool step
        var step = document.createElement('div');
        step.className = 'chat-activity-step running';
        step.setAttribute('data-type', 'tool');
        var h = '<span class="activity-icon">' + ACTIVITY_SPINNER_SVG + '</span>' +
                '<span class="activity-label">' + escapeHtml(label) + '</span>';
        if (detail) {
            h += '<span class="activity-detail">' + escapeHtml(detail) + '</span>';
        }
        step.innerHTML = h;
        activityContainer.appendChild(step);
        activitySteps.push(step);
        scrollToBottomIfPinned();
    };

    window.completeLastActivityStep = function () {
        if (!activityContainer || activitySteps.length === 0) return;
        var last = activitySteps[activitySteps.length - 1];
        if (last === currentReasoningStep) {
            clearReasoningStep();
        } else {
            completeActivityStep(last);
        }
        scrollToBottomIfPinned();
    };

    /* ── Cursor-style collapsible tool terminal blocks ───────────────── */

    // Stagger entrance animations so a burst of tool blocks pops in one-by-one
    // rather than all at once. Blocks appearing >400ms apart start a fresh burst.
    function staggerEntrance(el) {
        var now = Date.now();
        if (now - lastEnterAt > 400) {
            enterStagger = 0;
        } else {
            enterStagger = Math.min(enterStagger + 1, 8);
        }
        lastEnterAt = now;
        if (enterStagger > 0) {
            el.style.animationDelay = (enterStagger * 80) + 'ms';
        }
    }

    function setToolBlockExpanded(block, expanded) {
        if (!block || !block.el) return;
        if (expanded) {
            block.el.classList.add('expanded');
            block.body.style.display = 'block';
        } else {
            block.el.classList.remove('expanded');
            block.body.style.display = 'none';
        }
    }

    window.addToolBlock = function (payloadJson) {
        if (!activityContainer) openThinkingBlock();
        if (!activityContainer) return;
        var data;
        try {
            data = typeof payloadJson === 'string' ? JSON.parse(payloadJson) : payloadJson;
        } catch (e) { return; }
        var callId = data.call_id || ('tool_' + Date.now());
        var title = data.title || 'Running tool';
        var cmd = data.cmd || '';

        // A tool is starting — dismiss the live reasoning placeholder.
        clearReasoningStep();

        // Dedupe: the same call_id can be announced twice (local on_tool_call
        // and ws on_tool_progress both reach here). Reuse the existing block so
        // we never orphan a still-spinning DOM node that completeToolBlock can't
        // reach. Preserve any logs already streamed into its body.
        var existing = toolBlocks[callId];
        if (existing && existing.el && existing.el.parentNode) {
            existing.el.classList.remove('done', 'error');
            existing.el.classList.add('running');
            var exIcon = existing.header.querySelector('.chat-tool-icon');
            if (exIcon) exIcon.innerHTML = ACTIVITY_SPINNER_SVG;
            if (title) {
                var exTitle = existing.header.querySelector('.chat-tool-title');
                if (exTitle) exTitle.textContent = title;
            }
            if (cmd) {
                var exCmd = existing.header.querySelector('.chat-tool-cmd');
                if (exCmd) exCmd.textContent = cmd;
            }
            scrollToBottomIfPinned();
            return;
        }

        var el = document.createElement('div');
        el.className = 'chat-tool-block running expanded chat-message-enter';
        el.setAttribute('data-call-id', callId);
        staggerEntrance(el);

        var header = document.createElement('button');
        header.type = 'button';
        header.className = 'chat-tool-header';
        header.innerHTML =
            '<span class="chat-tool-chevron">' + TOOL_CHEVRON_SVG + '</span>' +
            '<span class="chat-tool-icon">' + ACTIVITY_SPINNER_SVG + '</span>' +
            '<span class="chat-tool-title">' + escapeHtml(title) + '</span>' +
            '<span class="chat-tool-cmd">' + escapeHtml(cmd) + '</span>';

        var body = document.createElement('div');
        body.className = 'chat-tool-body';

        header.addEventListener('click', function () {
            var block = toolBlocks[callId];
            // Only blocks that streamed log lines are expandable; the rest are
            // just a tick + heading and have nothing to reveal.
            if (!block || block.lines.length === 0) return;
            var nowExpanded = !el.classList.contains('expanded');
            setToolBlockExpanded(block, nowExpanded);
        });

        el.appendChild(header);
        el.appendChild(body);
        activityContainer.appendChild(el);

        toolBlocks[callId] = { el: el, header: header, body: body, lines: [] };
        scrollToBottomIfPinned();
    };

    window.appendToolLog = function (callId, line) {
        if (!callId || !line) return;
        if (!toolBlocks[callId]) {
            window.addToolBlock(JSON.stringify({
                call_id: callId,
                title: 'Rendering',
                cmd: 'product demo'
            }));
        }
        var block = toolBlocks[callId];
        if (!block) return;
        var row = document.createElement('div');
        row.className = 'chat-tool-line';
        row.textContent = line;
        block.body.appendChild(row);
        block.lines.push(line);
        // Reveal the chevron now that there's something to expand.
        block.el.classList.add('has-logs');
        block.body.scrollTop = block.body.scrollHeight;
        scrollToBottomIfPinned();
    };

    window.completeToolBlock = function (callId, ok, summary) {
        var block = toolBlocks[callId];
        if (block && block.el) {
            if (!ok) {
                // Failed tool calls are transient noise — the agent retries and
                // usually succeeds. Drop them so only successful steps remain.
                if (block.el.parentNode) block.el.remove();
                delete toolBlocks[callId];
            } else {
                block.el.classList.remove('running');
                block.el.classList.add('done');

                var iconEl = block.header.querySelector('.chat-tool-icon');
                if (iconEl) iconEl.innerHTML = ACTIVITY_CHECK_SVG;

                // Header is just the tick + heading; the summary/detail and the
                // chevron (unless logs streamed) are hidden via CSS.
                setToolBlockExpanded(block, false);
            }
        }
        // Unknown call_id: nothing to stop — fall through.

        scrollToBottomIfPinned();
    };

    /* ── Processing state ── */

    let typingEl = null;
    window.setProcessing = function (processing) {
        sendBtn.disabled = processing;
        cancelBtn.style.display = processing ? 'flex' : 'none';
        if (processing) {
            if (thinkingBlockEl && !thinkingBlockCollapsed) return;
            if (thinkingBlockEl && thinkingBlockCollapsed) {
                thinkingBlockEl = null;
                thinkingBlockBody = null;
                thinkingBlockHeader = null;
                thinkingBlockCollapsed = false;
                activityContainer = null;
            }
            processingStartTime = Date.now();
            if (glowWrap) glowWrap.classList.add('glow-active');
            removePlaceholder();
            openThinkingBlock();
            scrollToBottomIfPinned();
        } else {
            if (glowWrap) glowWrap.classList.remove('glow-active');
            clearReasoningStep();
            for (var i = 0; i < activitySteps.length; i++) {
                if (activitySteps[i].classList.contains('running')) {
                    completeActivityStep(activitySteps[i]);
                }
            }
            Object.keys(toolBlocks).forEach(function (cid) {
                var block = toolBlocks[cid];
                if (block && block.el && block.el.classList.contains('running')) {
                    block.el.classList.remove('running');
                    block.el.classList.add('done');
                    var iconEl = block.header.querySelector('.chat-tool-icon');
                    if (iconEl) iconEl.innerHTML = ACTIVITY_CHECK_SVG;
                    setToolBlockExpanded(block, false);
                }
            });
            if (!firstAnswerTokenReceived && thinkingBlockEl && processingStartTime) {
                collapseThinkingBlock(Date.now() - processingStartTime);
            }
            window.resetStreamingMessage();
            if (thinkingBlockEl && thinkingBlockBody) {
                var hasTools = thinkingBlockBody.querySelector('.chat-tool-block');
                var hasSteps = activitySteps.length > 0;
                if (!hasTools && !hasSteps && !thinkingBlockCollapsed) {
                    thinkingBlockEl.remove();
                    thinkingBlockEl = null;
                    thinkingBlockBody = null;
                    thinkingBlockHeader = null;
                }
            }
            activityContainer = null;
            activitySteps = [];
            toolBlocks = {};
            currentReasoningStep = null;
            if (processingStartTime) {
                lastRunTimestamp = Date.now();
                processingStartTime = null;
                updatePreambleStatus();
            }
            if (inputEl) inputEl.focus();
        }
    };

    function formatTimeAgo(ts) {
        if (!ts) return '';
        var sec = Math.round((Date.now() - ts) / 1000);
        if (sec < 5) return 'just now';
        if (sec < 60) return sec + 's ago';
        var min = Math.round(sec / 60);
        if (min < 60) return min + 'm ago';
        var hr = Math.round(min / 60);
        return hr + 'h ago';
    }

    function updatePreambleStatus() {
        if (!preambleStatus) return;
        if (!lastRunTimestamp) {
            preambleStatus.classList.remove('visible');
            return;
        }
        var parts = [];
        parts.push(formatTimeAgo(lastRunTimestamp));
        if (lastThoughtSec !== null) {
            parts.push('Thought ' + (lastThoughtSec < 1 ? '<1' : lastThoughtSec) + ' sec');
        }
        var modelName = modelLabel ? modelLabel.textContent : '';
        if (modelName && modelName !== 'Model') parts.push(modelName);
        preambleStatus.innerHTML = parts.join('<span class="status-sep">&middot;</span>');
        preambleStatus.classList.add('visible');
    }

    // Refresh the "Xm ago" text periodically
    statusInterval = setInterval(function () {
        if (lastRunTimestamp) updatePreambleStatus();
    }, 10000);

    var modelItems = [];
    var selectedModelId = '';
    var menuOpen = false;

    // Provider SVG logos (16x16) keyed by provider slug
    var PROVIDER_ICONS = {
        openai:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<path d="M22.282 9.821a5.985 5.985 0 00-.516-4.91 6.046 6.046 0 00-6.51-2.9A6.065 6.065 0 0011.684.18a6.038 6.038 0 00-5.77 4.22 5.99 5.99 0 00-3.997 2.9 6.05 6.05 0 00.743 7.097 5.98 5.98 0 00.51 4.911 6.05 6.05 0 006.515 2.9A5.999 5.999 0 0014.297 23.8a6.04 6.04 0 005.772-4.206 5.98 5.98 0 003.997-2.9 6.056 6.056 0 00-.784-6.873zM14.297 22.27a4.49 4.49 0 01-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 00.392-.681v-6.737l2.02 1.166a.071.071 0 01.038.052v5.583a4.504 4.504 0 01-4.494 4.496zM3.958 18.14a4.477 4.477 0 01-.537-3.018l.142.085 4.783 2.759a.771.771 0 00.78 0l5.843-3.369v2.332a.08.08 0 01-.033.062L9.74 19.77a4.506 4.506 0 01-5.782-1.63zM2.468 7.87a4.485 4.485 0 012.344-1.974V11.6a.766.766 0 00.388.676l5.815 3.355-2.02 1.168a.076.076 0 01-.071.005l-4.83-2.786A4.504 4.504 0 012.468 7.87zm16.597 3.855L13.22 8.37l2.02-1.166a.076.076 0 01.071-.006l4.83 2.787a4.494 4.494 0 01-.676 8.105v-5.818a.79.79 0 00-.4-.687zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 00-.785 0L9.534 9.203V6.87a.08.08 0 01.033-.062l4.83-2.787a4.5 4.5 0 016.678 4.681zM8.392 12.497l-2.02-1.164a.076.076 0 01-.038-.057V5.694a4.504 4.504 0 017.37-3.455l-.14.079-4.78 2.758a.795.795 0 00-.392.681zm1.097-2.365L12 8.612l2.511 1.45v2.906l-2.511 1.45-2.511-1.45z" fill="currentColor"/></svg>',
        anthropic:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<path d="M17.304 3.541h-3.672l6.696 16.918h3.672L17.304 3.541zm-10.608 0L0 20.459h3.744l1.38-3.588h7.104l1.38 3.588h3.744L10.656 3.541H6.696zm.456 10.2l2.544-6.612 2.544 6.612H7.152z" fill="currentColor"/></svg>',
        ollama:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm0 2a8 8 0 110 16 8 8 0 010-16zm-2.5 5a1.5 1.5 0 100 3 1.5 1.5 0 000-3zm5 0a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM8.5 13.5s1 2 3.5 2 3.5-2 3.5-2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        google:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="currentColor" opacity=".7"/>' +
            '<path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="currentColor" opacity=".8"/>' +
            '<path d="M5.84 14.1a6.84 6.84 0 010-4.24V7.02H2.18A11.96 11.96 0 001 12c0 1.94.46 3.77 1.18 5.02l3.66-2.92z" fill="currentColor" opacity=".6"/>' +
            '<path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.02l3.66 2.84c.87-2.6 3.3-4.48 6.16-4.48z" fill="currentColor" opacity=".9"/></svg>',
        meta:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<path d="M6.915 4.03c-1.968 0-3.042 1.566-3.042 4.158 0 1.86.756 4.08 2.028 5.946.834 1.224 2.31 2.844 3.93 2.844.87 0 1.494-.432 2.16-1.266.708-.894 1.2-2.064 1.2-2.064s.498 1.17 1.2 2.064c.666.834 1.29 1.266 2.16 1.266 1.62 0 3.096-1.62 3.93-2.844 1.272-1.866 2.028-4.086 2.028-5.946 0-2.592-1.074-4.158-3.042-4.158-1.59 0-3.06 1.386-4.278 3.498-.456.798-.822 1.578-1.158 2.352-.336-.774-.702-1.554-1.158-2.352C11.976 5.416 10.506 4.03 8.915 4.03z" fill="currentColor" opacity=".85"/></svg>',
        mistral:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<rect x="1" y="3" width="5" height="5" fill="currentColor"/><rect x="18" y="3" width="5" height="5" fill="currentColor"/>' +
            '<rect x="1" y="9.5" width="5" height="5" fill="currentColor"/><rect x="9.5" y="9.5" width="5" height="5" fill="currentColor"/><rect x="18" y="9.5" width="5" height="5" fill="currentColor"/>' +
            '<rect x="1" y="16" width="5" height="5" fill="currentColor"/><rect x="5.25" y="16" width="5" height="5" fill="currentColor" opacity=".5"/><rect x="9.5" y="16" width="5" height="5" fill="currentColor"/><rect x="13.75" y="16" width="5" height="5" fill="currentColor" opacity=".5"/><rect x="18" y="16" width="5" height="5" fill="currentColor"/></svg>',
        cohere:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">' +
            '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6" fill="none"/>' +
            '<circle cx="12" cy="12" r="4" fill="currentColor"/></svg>',
        default:
            '<svg class="chat-model-option-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M8 1a3 3 0 00-3 3v1H4a2 2 0 00-2 2v6a2 2 0 002 2h8a2 2 0 002-2V7a2 2 0 00-2-2h-1V4a3 3 0 00-3-3zm0 1.5A1.5 1.5 0 019.5 4v1h-3V4A1.5 1.5 0 018 2.5zM6 9a1 1 0 112 0 1 1 0 01-2 0zm4 0a1 1 0 112 0 1 1 0 01-2 0z" fill="currentColor"/></svg>'
    };

    function detectProvider(modelId) {
        var id = (modelId || '').toLowerCase();
        if (id.indexOf('openai') === 0 || id.indexOf('gpt') !== -1 || id.indexOf('o1') !== -1 || id.indexOf('o3') !== -1) return 'openai';
        if (id.indexOf('anthropic') !== -1 || id.indexOf('claude') !== -1) return 'anthropic';
        if (id.indexOf('ollama') !== -1 || id.indexOf('llama') !== -1 || id.indexOf('local') !== -1) return 'ollama';
        if (id.indexOf('gemini') !== -1 || id.indexOf('google') !== -1) return 'google';
        if (id.indexOf('meta') !== -1) return 'meta';
        if (id.indexOf('mistral') !== -1 || id.indexOf('mixtral') !== -1) return 'mistral';
        if (id.indexOf('cohere') !== -1 || id.indexOf('command') !== -1) return 'cohere';
        return 'default';
    }

    function getModelIcon(modelId) {
        var provider = detectProvider(modelId);
        return PROVIDER_ICONS[provider] || PROVIDER_ICONS['default'];
    }

    function getCheckIcon() {
        return '<svg class="chat-model-option-check" width="14" height="14" viewBox="0 0 14 14" fill="none">' +
            '<path d="M3 7.5l2.5 2.5L11 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    }

    function renderMenu() {
        modelMenu.innerHTML = '';
        modelItems.forEach(function (item) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-model-option' + (item.id === selectedModelId ? ' selected' : '');
            btn.setAttribute('role', 'option');
            btn.setAttribute('aria-selected', item.id === selectedModelId ? 'true' : 'false');
            btn.innerHTML = getModelIcon(item.id) +
                '<span class="chat-model-option-name">' + escapeHtml(item.name) + '</span>' +
                getCheckIcon();
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                selectModel(item.id, item.name);
                closeMenu();
            });
            modelMenu.appendChild(btn);
        });
    }

    function updateTriggerIcon(modelId) {
        var iconEl = modelTrigger.querySelector('.chat-model-icon');
        if (iconEl) {
            var tmp = document.createElement('span');
            tmp.innerHTML = getModelIcon(modelId);
            var newIcon = tmp.firstChild;
            if (newIcon) {
                newIcon.classList.add('chat-model-icon');
                newIcon.classList.remove('chat-model-option-icon');
                newIcon.setAttribute('width', '14');
                newIcon.setAttribute('height', '14');
                iconEl.parentNode.replaceChild(newIcon, iconEl);
            }
        }
    }

    function selectModel(id, name) {
        selectedModelId = id;
        modelSelect.value = id;
        if (modelLabel) modelLabel.textContent = name || id || 'Model';
        updateTriggerIcon(id);
        renderMenu();
    }

    function openMenu() {
        if (menuOpen) return;
        menuOpen = true;
        renderMenu();
        /* Fixed menu lives under document.body. Qt WebKit often reports unreliable innerHeight;
           use client metrics and clamp horizontal position. */
        var rect = modelTrigger.getBoundingClientRect();
        var vh = Math.max(
            document.documentElement ? document.documentElement.clientHeight : 0,
            window.innerHeight || 0,
            1
        );
        var vw = Math.max(
            document.documentElement ? document.documentElement.clientWidth : 0,
            window.innerWidth || 0,
            1
        );
        var gap = 6;
        var menuMax = 280;
        var left = rect.left;
        var menuW = 260;
        if (left + menuW > vw - 4) {
            left = Math.max(4, vw - menuW - 4);
        }
        modelMenu.style.position = 'fixed';
        modelMenu.style.left = Math.round(left) + 'px';
        modelMenu.style.right = 'auto';
        modelMenu.style.visibility = 'visible';
        modelMenu.style.zIndex = '2147483647';
        var spaceBelow = Math.max(0, vh - rect.bottom - 8);
        var spaceAbove = Math.max(0, rect.top - 8);
        modelMenu.style.top = '';
        modelMenu.style.bottom = '';
        if (spaceBelow >= 120 || spaceBelow >= spaceAbove) {
            modelMenu.style.top = Math.round(rect.bottom + gap) + 'px';
            modelMenu.style.bottom = 'auto';
            modelMenu.style.maxHeight = Math.min(menuMax, spaceBelow) + 'px';
        } else {
            modelMenu.style.bottom = Math.round(vh - rect.top + gap) + 'px';
            modelMenu.style.top = 'auto';
            modelMenu.style.maxHeight = Math.min(menuMax, spaceAbove) + 'px';
        }
        modelMenu.style.display = 'block';
        modelTrigger.classList.add('active');
    }

    function closeMenu() {
        if (!menuOpen) return;
        menuOpen = false;
        modelMenu.style.display = 'none';
        modelMenu.style.top = '';
        modelMenu.style.bottom = '';
        modelMenu.style.maxHeight = '';
        modelTrigger.classList.remove('active');
    }

    function toggleMenu(e) {
        e.stopPropagation();
        if (menuOpen) closeMenu();
        else openMenu();
    }

    modelTrigger.addEventListener('click', toggleMenu);
    document.addEventListener('click', function (e) {
        if (menuOpen && !modelMenu.contains(e.target) && !modelTrigger.contains(e.target)) {
            closeMenu();
        }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && menuOpen) closeMenu();
    });

    window.setModels = function (modelListJson) {
        var list = [];
        try {
            list = JSON.parse(modelListJson);
        } catch (e) {
            list = [];
        }
        modelItems = list.map(function (item) {
            return { id: item.id || item.name || '', name: item.name || item.id || '', isDefault: !!item.default };
        });
        // Keep hidden select in sync
        var currentValue = modelSelect.value;
        modelSelect.innerHTML = '';
        modelItems.forEach(function (item) {
            var opt = document.createElement('option');
            opt.value = item.id;
            opt.textContent = item.name;
            if (item.isDefault) opt.selected = true;
            modelSelect.appendChild(opt);
        });
        // Determine selected
        var picked = modelItems.find(function (i) { return i.id === currentValue; });
        if (!picked) picked = modelItems.find(function (i) { return i.isDefault; });
        if (!picked && modelItems.length) picked = modelItems[0];
        if (picked) selectModel(picked.id, picked.name);
        else if (modelLabel) modelLabel.textContent = 'Model';
    };

    window.setPreamble = function (html) {
        if (preambleEl) preambleEl.innerHTML = html;
    };

    window.updateCreditsBalance = function (balance) {
        var badge = document.getElementById('chat-credits-badge');
        if (!badge) return;
        if (balance === null || balance === undefined) {
            badge.style.display = 'none';
            return;
        }
        badge.style.display = 'inline-flex';
        if (balance < 0) {
            badge.textContent = '…';
            badge.style.background = 'rgba(124,111,247,0.08)';
            badge.style.color = 'rgba(124,111,247,0.65)';
            badge.style.borderColor = 'rgba(124,111,247,0.15)';
            return;
        }
        badge.textContent = balance + ' credits';
        if (balance === 0) {
            badge.style.background = 'rgba(239,68,68,0.12)';
            badge.style.color = 'rgba(239,68,68,0.9)';
            badge.style.borderColor = 'rgba(239,68,68,0.25)';
        } else if (balance < 50) {
            badge.style.background = 'rgba(245,158,11,0.12)';
            badge.style.color = 'rgba(245,158,11,0.9)';
            badge.style.borderColor = 'rgba(245,158,11,0.25)';
        } else {
            badge.style.background = 'rgba(124,111,247,0.12)';
            badge.style.color = 'rgba(124,111,247,0.9)';
            badge.style.borderColor = 'rgba(124,111,247,0.2)';
        }
    };

    window.setThemeColors = function (cssVarsJson) {
        try {
            const vars = JSON.parse(cssVarsJson);
            const root = document.documentElement;
            Object.keys(vars).forEach(function (key) {
                root.style.setProperty('--' + key, vars[key]);
            });
            // Qt WebKit: many builds lack reliable var() / modern CSS. Apply critical surfaces inline.
            if (document.documentElement.getAttribute('data-zenvi-webkit') === '1') {
                applyZenviWebKitInlineTheme(vars);
            }
        } catch (e) {}
    };

    function applyZenviWebKitInlineTheme(vars) {
        try {
            const bg = vars['chat-bg'] || '#0d0d0d';
            const tx = vars['chat-text'] || '#d4d4d4';
            const br = (vars['chat-border'] && vars['chat-border'] !== 'transparent')
                ? vars['chat-border'] : 'rgba(255,255,255,0.07)';
            const inp = vars['chat-input-bg'] || '#171717';
            const surf = vars['chat-surface'] || vars['chat-preamble-bg'] || bg;
            const muted = vars['chat-muted'] || vars['chat-placeholder'] || '#6b7280';
            const acc = vars['chat-accent'] || '#4d9cf6';
            const codeBg = vars['chat-code-bg'] || '#252525';

            document.body.style.background = bg;
            document.body.style.color = tx;

            const msgs = document.getElementById('chat-messages');
            if (msgs) {
                msgs.style.background = surf;
                msgs.style.color = tx;
            }
            const tabBar = document.getElementById('chat-tab-bar');
            if (tabBar) {
                tabBar.style.background = bg;
                tabBar.style.borderBottom = '1px solid ' + br;
            }
            const preamble = document.getElementById('chat-preamble-label');
            const preambleRow = preamble ? preamble.parentElement : null;
            if (preambleRow) {
                preambleRow.style.background = surf;
                preambleRow.style.color = tx;
            }
            const glowInner = document.querySelector('.chat-input-glow-inner');
            if (glowInner) {
                glowInner.style.background = inp;
            }
            const inputRow = document.getElementById('chat-input-row');
            if (inputRow) {
                inputRow.style.color = tx;
            }
            const modelTrig = document.getElementById('chat-model-trigger');
            if (modelTrig) {
                modelTrig.style.borderColor = br;
                modelTrig.style.background = surf;
                modelTrig.style.color = tx;
            }
            const ta = document.getElementById('chat-input');
            if (ta) {
                ta.style.background = 'transparent';
            }
            syncTextareaMaskForOverlay();
            /* Expose for glow CSS colour tweaks */
            document.documentElement.style.setProperty('--chat-accent', acc);
            document.documentElement.style.setProperty('--chat-muted', muted);
            document.documentElement.style.setProperty('--chat-code-bg', codeBg);
        } catch (e) {}
    }

    window.clearMessages = function () {
        typingEl = null;
        messagesEl.innerHTML = '';
    };

    function sendMessage() {
        const text = (inputEl.value || '').trim();
        if (!text) return;
        exitIdle();
        closeCommandPalette();
        getBridge(function (bridge) {
            if (!bridge) return;
            bridge.sendMessage(text, modelSelect.value || '', currentAgentMode);
            inputEl.value = '';
            adjustTextareaHeight();
        });
    }

    function setAgentModeUI(mode) {
        currentAgentMode = mode === 'planning' ? 'planning' : 'agent';
        if (modePlanBtn) modePlanBtn.classList.toggle('active', currentAgentMode === 'planning');
        if (modeAgentBtn) modeAgentBtn.classList.toggle('active', currentAgentMode === 'agent');
        if (inputEl) {
            inputEl.placeholder = currentAgentMode === 'planning'
                ? 'Describe the edit; I will draft a plan without changing the timeline…'
                : 'Ask or edit directly…';
        }
        var wrap = document.getElementById('chat-input-glow-wrap');
        if (wrap) {
            wrap.classList.toggle('planning-mode', currentAgentMode === 'planning');
        }
    }

    window.setAgentModeUI = setAgentModeUI;

    function onModeButtonClick(mode) {
        setAgentModeUI(mode);
        getBridge(function (bridge) {
            if (bridge && bridge.setAgentMode) {
                bridge.setAgentMode(mode);
            }
        });
    }

    if (modePlanBtn) modePlanBtn.addEventListener('click', function () { onModeButtonClick('planning'); });
    if (modeAgentBtn) modeAgentBtn.addEventListener('click', function () { onModeButtonClick('agent'); });

    window.setPlanReadyBanner = function (show) {
        if (!show) window.setPlanChip(null);
    };

    var currentPlanData = null;

    function escapeAttr(s) {
        if (!s) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;');
    }

    window.setPlanChip = function (planJson) {
        var root = document.getElementById('chat-plan-chip');
        if (!planJson) {
            currentPlanData = null;
            if (root && root.parentNode) root.remove();
            return;
        }
        var plan = typeof planJson === 'string' ? JSON.parse(planJson) : planJson;
        currentPlanData = plan;
        if (!root) {
            root = document.createElement('div');
            root.id = 'chat-plan-chip';
            root.className = 'chat-plan-chip';
            var messages = document.getElementById('chat-messages');
            if (messages) messages.appendChild(root);
        }
        var status = (plan.status || 'draft').toUpperCase();
        var steps = plan.steps || [];
        var done = 0;
        var failed = 0;
        for (var i = 0; i < steps.length; i++) {
            var st = (steps[i].status || '').toLowerCase();
            if (st === 'completed') done++;
            if (st === 'failed' || st === 'blocked') failed++;
        }
        var progress = steps.length ? (done + '/' + steps.length + ' done') : '';
        if (failed > 0) progress += ' (' + failed + ' failed)';
        var html = '<div class="chat-plan-chip-inner">' +
            '<span class="chat-plan-chip-title">' + escapeHtml(plan.title || 'Edit plan') + '</span>' +
            '<span class="chat-plan-chip-badge chat-plan-chip-badge-' + escapeHtml(status.toLowerCase()) + '">' + escapeHtml(status) + '</span>' +
            (progress ? '<span class="chat-plan-chip-progress" id="chat-plan-chip-progress">' + escapeHtml(progress) + '</span>' : '') +
            '<div class="chat-plan-chip-actions">' +
            '<button type="button" class="chat-plan-chip-open" id="chat-plan-chip-open">Open Plan</button>';
        if (status === 'READY' || status === 'BLOCKED') {
            var execLabel = status === 'BLOCKED' ? 'Retry execution' : 'Execute';
            html += '<button type="button" class="chat-plan-chip-exec" id="chat-plan-chip-exec">' + execLabel + '</button>';
        }
        if (status === 'BLOCKED') {
            html += '<button type="button" class="chat-plan-chip-edit" id="chat-plan-chip-edit">Edit in Plan mode</button>';
        }
        html += '</div></div>';
        root.innerHTML = html;
        var openBtn = document.getElementById('chat-plan-chip-open');
        if (openBtn) {
            openBtn.onclick = function () {
                getBridge(function (bridge) {
                    if (bridge && bridge.openPlanDock) bridge.openPlanDock();
                });
            };
        }
        var execBtn = document.getElementById('chat-plan-chip-exec');
        if (execBtn) {
            execBtn.onclick = function () {
                getBridge(function (bridge) {
                    if (bridge && bridge.executePlanNoArgs) bridge.executePlanNoArgs();
                    else if (bridge && bridge.executePlan) bridge.executePlan('', '');
                });
            };
        }
        var editBtn = document.getElementById('chat-plan-chip-edit');
        if (editBtn) {
            editBtn.onclick = function () {
                getBridge(function (bridge) {
                    if (bridge && bridge.editPlanInPlanningMode) bridge.editPlanInPlanningMode();
                });
            };
        }
    };

    window.setPlanData = window.setPlanChip;

    window.updatePlanChipProgress = function (stepId, status, error) {
        if (!currentPlanData || !currentPlanData.steps) return;
        for (var i = 0; i < currentPlanData.steps.length; i++) {
            if (currentPlanData.steps[i].step_id === stepId) {
                currentPlanData.steps[i].status = status;
                if (error) currentPlanData.steps[i].last_error = error;
                break;
            }
        }
        var el = document.getElementById('chat-plan-chip-progress');
        if (!el) return;
        var done = 0;
        var failed = 0;
        var steps = currentPlanData.steps;
        for (var j = 0; j < steps.length; j++) {
            var st = (steps[j].status || '').toLowerCase();
            if (st === 'completed') done++;
            if (st === 'failed' || st === 'blocked') failed++;
        }
        var text = done + '/' + steps.length + ' done';
        if (failed > 0) text += ' (' + failed + ' failed)';
        el.textContent = text;
    };

    window.updatePlanStep = function () { /* chip uses updatePlanChipProgress */ };

    window.clearPlanQuestions = function () {
        var el = document.getElementById('chat-plan-questions');
        if (el && el.parentNode) el.remove();
    };

    window.setPlanQuestions = function (questionsJson) {
        var questions = typeof questionsJson === 'string' ? JSON.parse(questionsJson) : questionsJson;
        if (!questions || !questions.length) {
            window.clearPlanQuestions();
            return;
        }
        window.clearPlanQuestions();
        var root = document.createElement('div');
        root.id = 'chat-plan-questions';
        root.className = 'chat-plan-questions';
        var requiredIds = [];
        var html = '<div class="chat-plan-questions-header">A few questions before I finalize the plan</div>';
        for (var i = 0; i < questions.length; i++) {
            var q = questions[i];
            var qid = String(q.id || ('q' + (i + 1)));
            requiredIds.push(qid);
            html += '<div class="chat-plan-question" data-qid="' + escapeAttr(qid) + '">';
            html += '<label class="chat-plan-question-prompt">' + escapeHtml(q.prompt || '') + '</label>';
            if (q.options && q.options.length) {
                html += '<div class="chat-plan-question-options">';
                for (var j = 0; j < q.options.length; j++) {
                    var opt = q.options[j];
                    html += '<button type="button" class="chat-plan-option-btn" data-qid="' + escapeAttr(qid) + '" data-value="' + escapeAttr(opt) + '">' + escapeHtml(opt) + '</button>';
                }
                html += '</div>';
            }
            html += '<input type="text" class="chat-plan-question-input" data-qid="' + escapeAttr(qid) + '" placeholder="Your answer…" />';
            html += '</div>';
        }
        root.dataset.requiredIds = JSON.stringify(requiredIds);
        html += '<textarea class="chat-plan-questions-notes" placeholder="Anything else? (optional)" rows="2"></textarea>';
        html += '<div class="chat-plan-questions-actions">';
        html += '<button type="button" class="chat-plan-questions-submit" id="chat-plan-questions-submit" disabled>Submit answers</button>';
        html += '<button type="button" class="chat-plan-questions-skip" id="chat-plan-questions-skip">Skip — use your judgment</button>';
        html += '</div>';
        root.innerHTML = html;
        var messages = document.getElementById('chat-messages');
        if (messages) messages.appendChild(root);

        function allAnswered() {
            var ids;
            try {
                ids = JSON.parse(root.dataset.requiredIds || '[]');
            } catch (e) {
                ids = requiredIds;
            }
            for (var k = 0; k < ids.length; k++) {
                var input = root.querySelector('.chat-plan-question-input[data-qid="' + ids[k] + '"]');
                if (!input || !input.value.trim()) return false;
            }
            return ids.length > 0;
        }

        function updateSubmitState() {
            var submitBtn = document.getElementById('chat-plan-questions-submit');
            if (submitBtn) submitBtn.disabled = !allAnswered();
        }

        root.querySelectorAll('.chat-plan-option-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var qid = btn.getAttribute('data-qid');
                var input = root.querySelector('.chat-plan-question-input[data-qid="' + qid + '"]');
                if (input) input.value = btn.getAttribute('data-value') || '';
                root.querySelectorAll('.chat-plan-option-btn[data-qid="' + qid + '"]').forEach(function (b) {
                    b.classList.toggle('selected', b === btn);
                });
                updateSubmitState();
            });
        });

        root.querySelectorAll('.chat-plan-question-input').forEach(function (input) {
            input.addEventListener('input', updateSubmitState);
        });

        var submitBtn = document.getElementById('chat-plan-questions-submit');
        if (submitBtn) {
            submitBtn.onclick = function () {
                if (!allAnswered()) return;
                var answers = {};
                root.querySelectorAll('.chat-plan-question-input').forEach(function (input) {
                    var qid = input.getAttribute('data-qid');
                    if (qid && input.value.trim()) answers[qid] = input.value.trim();
                });
                var notesEl = root.querySelector('.chat-plan-questions-notes');
                var payload = { answers: answers, notes: notesEl ? notesEl.value.trim() : '' };
                getBridge(function (bridge) {
                    if (bridge && bridge.submitPlanAnswers) {
                        bridge.submitPlanAnswers(JSON.stringify(payload));
                    } else {
                        console.warn('submitPlanAnswers bridge method missing');
                    }
                });
                window.clearPlanQuestions();
            };
        }
        var skipBtn = document.getElementById('chat-plan-questions-skip');
        if (skipBtn) {
            skipBtn.onclick = function () {
                getBridge(function (bridge) {
                    if (bridge && bridge.submitPlanAnswers) {
                        bridge.submitPlanAnswers(JSON.stringify({ skip: true }));
                    } else {
                        console.warn('submitPlanAnswers bridge method missing');
                    }
                });
                window.clearPlanQuestions();
            };
        }
        scrollToBottomIfPinned();
        root.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    };

    window.setChatInput = function (text) {
        if (!inputEl) return;
        inputEl.value = text || '';
        try {
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        } catch (e) {}
        inputEl.focus();
    };

    setAgentModeUI('agent');

    function insertAtCursor(text) {
        try {
            if (!inputEl) return;
            const start = inputEl.selectionStart || 0;
            const end = inputEl.selectionEnd || 0;
            const before = inputEl.value.slice(0, start);
            const after = inputEl.value.slice(end);
            inputEl.value = before + text + after;
            const pos = start + text.length;
            inputEl.selectionStart = inputEl.selectionEnd = pos;
        } catch (e) {}
    }

    function cancelRequest() {
        getBridge(function (bridge) {
            if (bridge) bridge.cancelRequest();
            window.setProcessing(false);
        });
    }

    function clearChat() {
        getBridge(function (bridge) {
            if (bridge && bridge.clearChat) {
                bridge.clearChat();
            } else {
                window.clearMessages();
            }
        });
    }

    sendBtn.addEventListener('click', sendMessage);

    cancelBtn.addEventListener('click', cancelRequest);
    clearBtn.addEventListener('click', clearChat);

    inputEl.addEventListener('keydown', function (e) {
        if (commandPaletteOpen) {
            var matches = getCommandMatches(commandQuery);
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (matches.length) {
                    commandActiveIndex = (commandActiveIndex + 1) % matches.length;
                    renderCommandPalette();
                }
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (matches.length) {
                    commandActiveIndex = (commandActiveIndex - 1 + matches.length) % matches.length;
                    renderCommandPalette();
                }
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                if (matches.length && commandActiveIndex >= 0) {
                    selectCommandIndex(commandActiveIndex);
                }
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                closeCommandPalette();
                return;
            }
        }

        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    inputEl.addEventListener('focus', function () {
        hideOverlay();
        if (glowWrap) glowWrap.classList.add('glow-active');
    });
    inputEl.addEventListener('blur', function () {
        if (glowWrap) glowWrap.classList.remove('glow-active');
    });
    inputEl.addEventListener('input', function () {
        var val = inputEl.value || '';
        adjustTextareaHeight();
        maybeUpdateCommandPaletteFromValue(val);
        if (val.trim().length > 0) hideOverlay();
    });

    // Close command palette on outside click (but keep model menu behavior intact)
    document.addEventListener('mousedown', function (e) {
        if (!commandPaletteOpen || !commandPaletteEl) return;
        var t = e.target;
        if (commandPaletteEl.contains(t) || inputEl.contains(t)) return;
        closeCommandPalette();
    });

    // Initial textarea sizing
    adjustTextareaHeight();

    if (inputOverlay) {
        inputOverlay.addEventListener('click', function () { hideOverlay(); });
    }

    function updateIdleState() {
        setInputIdle(false);
        syncTextareaMaskForOverlay();
    }

    setInputIdle(false);
    if (inputOverlay) inputOverlay.classList.add('hidden');
    syncTextareaMaskForOverlay();

    getBridge(function (bridge) {
        if (bridge && bridge.ready) bridge.ready();
    });

    window.clearMessages = (function (orig) {
        return function () {
            if (orig) orig();
            typingEl = null;
            activityContainer = null;
            activitySteps = [];
            toolBlocks = {};
            currentReasoningStep = null;
            thinkingBlockEl = null;
            thinkingBlockBody = null;
            thinkingBlockHeader = null;
            thinkingBlockCollapsed = false;
            firstAnswerTokenReceived = false;
            window.resetStreamingMessage();
            overlayVisible = true;
            if (inputOverlay) inputOverlay.classList.remove('hidden');
            typingIndex = 0;
            lastRunTimestamp = null;
            lastThoughtSec = null;
            processingStartTime = null;
            if (preambleStatus) {
                preambleStatus.classList.remove('visible');
                preambleStatus.innerHTML = '';
            }
            updateIdleState();
        };
    })(window.clearMessages);

    window.appendMessage = (function (orig) {
        return function (role, bodyHtml, isAssistant) {
            if (orig) orig(role, bodyHtml, isAssistant);
            updateIdleState();
        };
    })(window.appendMessage);

    // ==================================================================
    // Multi-chat tab bar
    // ==================================================================
    var tabBarEl = document.getElementById('chat-tab-bar');
    var tabAddBtn = document.getElementById('chat-tab-add');
    var currentTabs = [];
    var unreadSessions = {};  // sessionId -> true if has unread messages

    window.setTabs = function (tabsJson) {
        var list = [];
        try { list = JSON.parse(tabsJson); } catch (e) { list = []; }
        currentTabs = list;
        renderTabs();
    };

    function renderTabs() {
        // Remove all existing tab buttons (keep the "+" button)
        var existing = tabBarEl.querySelectorAll('.chat-tab');
        existing.forEach(function (el) { el.remove(); });

        // Always show the tab bar so the "+" new chat button is visible (Cursor-style).
        tabBarEl.style.display = 'flex';

        currentTabs.forEach(function (tab) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-tab'
                + (tab.active ? ' active' : '')
                + (unreadSessions[tab.id] ? ' has-unread' : '')
                + (tab.processing ? ' is-processing' : '');
            btn.setAttribute('role', 'tab');
            btn.setAttribute('data-session-id', tab.id);
            btn.setAttribute('aria-selected', tab.active ? 'true' : 'false');

            var titleSpan = '<span class="chat-tab-title">' + escapeHtml(tab.title || 'New Chat') + '</span>';
            var badge = '<span class="chat-tab-badge"></span>';
            var processingDot = tab.processing ? '<span class="chat-tab-processing" title="Processing"></span>' : '';
            var closeBtn = '<button type="button" class="chat-tab-close" data-close-id="' + escapeHtml(tab.id) + '" title="Close">&times;</button>';
            btn.innerHTML = processingDot + badge + titleSpan + closeBtn;

            btn.addEventListener('click', function (e) {
                if (e.target.classList.contains('chat-tab-close') || e.target.closest('.chat-tab-close')) {
                    return; // handled by close button
                }
                unreadSessions[tab.id] = false;
                getBridge(function (bridge) {
                    if (bridge && bridge.switchSession) bridge.switchSession(tab.id);
                });
            });

            // Close button handler
            var closeBtnEl = btn.querySelector('.chat-tab-close');
            if (closeBtnEl) {
                closeBtnEl.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var closeId = this.getAttribute('data-close-id');
                    getBridge(function (bridge) {
                        if (bridge && bridge.closeSession) bridge.closeSession(closeId);
                    });
                });
            }

            tabBarEl.insertBefore(btn, tabAddBtn);
        });
    }

    tabAddBtn.addEventListener('click', function () {
        getBridge(function (bridge) {
            if (bridge && bridge.createSession) bridge.createSession(modelSelect.value || '');
        });
    });

    // Handle background responses (marks tab as unread)
    window.onBackgroundResponse = function (sessionId, bodyHtml) {
        unreadSessions[sessionId] = true;
        renderTabs();
    };

    // ==================================================================
    // Version Cards for Parallel Execution
    // ==================================================================
    var versionCards = {}; // version_id -> card element

    window.addVersionCard = function (versionData) {
        removePlaceholder();
        var data;
        try {
            data = typeof versionData === 'string' ? JSON.parse(versionData) : versionData;
        } catch (e) {
            console.error('Failed to parse version data:', e);
            return;
        }

        var versionId = data.version_id;
        var title = data.title || 'Untitled';
        var status = data.status || 'pending';
        var progress = data.progress || 0;

        // Create version card
        var card = document.createElement('div');
        card.className = 'chat-version-card chat-message-enter';
        card.setAttribute('data-version-id', versionId);

        var statusBadge = getStatusBadge(status);
        var progressBarHtml = status === 'running' || status === 'pending'
            ? '<div class="version-progress-bar"><div class="version-progress-fill" style="width: ' + (progress * 100) + '%"></div></div>'
            : '';

        card.innerHTML =
            '<div class="version-header">' +
                '<span class="version-title">' + escapeHtml(title) + '</span>' +
                statusBadge +
            '</div>' +
            progressBarHtml +
            '<div class="version-activity-log" data-version-id="' + versionId + '"></div>' +
            '<button class="version-switch-btn" data-version-id="' + versionId + '" ' +
                (status === 'completed' ? '' : 'disabled') + '>' +
                'Switch to this version' +
            '</button>';

        messagesEl.appendChild(card);
        versionCards[versionId] = card;

        // Add click handler for switch button
        var switchBtn = card.querySelector('.version-switch-btn');
        if (switchBtn) {
            switchBtn.addEventListener('click', function () {
                var vid = this.getAttribute('data-version-id');
                getBridge(function (bridge) {
                    if (bridge && bridge.switchToVersion) {
                        bridge.switchToVersion(vid);
                    }
                });
            });
        }

        messagesEl.scrollTop = messagesEl.scrollHeight;
    };

    window.updateVersionProgress = function (versionId, progress, status) {
        var card = versionCards[versionId];
        if (!card) return;

        // Update progress bar
        var progressFill = card.querySelector('.version-progress-fill');
        if (progressFill) {
            progressFill.style.width = (progress * 100) + '%';
        }

        // Update status badge if provided
        if (status) {
            var oldBadge = card.querySelector('.version-status-badge');
            if (oldBadge) {
                var newBadge = getStatusBadge(status);
                oldBadge.outerHTML = newBadge;
            }

            // Enable/disable switch button based on status
            var switchBtn = card.querySelector('.version-switch-btn');
            if (switchBtn) {
                switchBtn.disabled = status !== 'completed';
            }

            // Remove progress bar if completed or failed
            if (status === 'completed' || status === 'failed') {
                var progressBar = card.querySelector('.version-progress-bar');
                if (progressBar) progressBar.remove();
            }
        }
    };

    window.addVersionActivityStep = function (versionId, label, detail) {
        var card = versionCards[versionId];
        if (!card) return;

        var activityLog = card.querySelector('.version-activity-log[data-version-id="' + versionId + '"]');
        if (!activityLog) return;

        var step = document.createElement('div');
        step.className = 'version-activity-step running';
        step.innerHTML =
            '<span class="activity-icon">' + ACTIVITY_SPINNER_SVG + '</span>' +
            '<span class="activity-label">' + escapeHtml(label) + '</span>' +
            (detail ? '<span class="activity-detail">' + escapeHtml(detail) + '</span>' : '');

        activityLog.appendChild(step);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    };

    window.completeVersionActivityStep = function (versionId) {
        var card = versionCards[versionId];
        if (!card) return;

        var activityLog = card.querySelector('.version-activity-log[data-version-id="' + versionId + '"]');
        if (!activityLog) return;

        var steps = activityLog.querySelectorAll('.version-activity-step.running');
        if (steps.length > 0) {
            var lastStep = steps[steps.length - 1];
            lastStep.classList.remove('running');
            lastStep.classList.add('done');
            var icon = lastStep.querySelector('.activity-icon');
            if (icon) icon.innerHTML = ACTIVITY_CHECK_SVG;
        }
    };

    function getStatusBadge(status) {
        var badges = {
            'pending': '<span class="version-status-badge status-pending">Pending</span>',
            'running': '<span class="version-status-badge status-running">Running</span>',
            'completed': '<span class="version-status-badge status-completed">✓ Completed</span>',
            'failed': '<span class="version-status-badge status-failed">✗ Failed</span>'
        };
        return badges[status] || badges['pending'];
    }

    // ==================================================================
    // Context progress ring + popover
    // ==================================================================
    var contextRingWrap = document.getElementById('chat-context-ring-wrap');
    var contextRingFg = document.getElementById('chat-context-ring-fg');
    var carryForwardBtn = document.getElementById('chat-carry-forward-btn');
    var popoverPct = document.getElementById('chat-context-popover-pct');
    var popoverTokens = document.getElementById('chat-context-popover-tokens');
    var popoverBarFill = document.getElementById('chat-context-popover-bar-fill');
    var RING_CIRCUMFERENCE = 2 * Math.PI * 8; // r=8 -> ~50.265

    window.updateContextUsage = function (usageJson) {
        var usage;
        try { usage = JSON.parse(usageJson); } catch (e) { return; }
        var fraction = usage.fraction || 0;
        var used = usage.used || 0;
        var total = usage.total || 1;
        var pctText = (fraction * 100).toFixed(1) + '%';
        var colorClass = fraction >= 0.85 ? 'danger' : (fraction >= 0.70 ? 'warn' : '');

        // Update ring stroke-dashoffset
        var offset = RING_CIRCUMFERENCE * (1 - fraction);
        if (contextRingFg) {
            contextRingFg.style.strokeDashoffset = offset;
            contextRingFg.classList.remove('warn', 'danger');
            if (colorClass) contextRingFg.classList.add(colorClass);
        }

        // Update popover contents
        if (popoverPct) {
            popoverPct.textContent = pctText;
            popoverPct.classList.remove('warn', 'danger');
            if (colorClass) popoverPct.classList.add(colorClass);
        }
        if (popoverTokens) {
            popoverTokens.textContent = numberWithCommas(used) + ' / ' + numberWithCommas(total);
        }
        if (popoverBarFill) {
            popoverBarFill.style.width = (fraction * 100).toFixed(2) + '%';
            popoverBarFill.classList.remove('warn', 'danger');
            if (colorClass) popoverBarFill.classList.add(colorClass);
        }

        // Show/hide carry-forward button
        if (carryForwardBtn) {
            carryForwardBtn.style.display = fraction >= 0.85 ? 'flex' : 'none';
        }
    };

    function numberWithCommas(x) {
        return x.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    // Carry-forward button handler
    if (carryForwardBtn) {
        carryForwardBtn.addEventListener('click', function () {
            getBridge(function (bridge) {
                if (bridge && bridge.carryForward) {
                    // Pass empty string to mean "active session"
                    bridge.carryForward('');
                }
            });
        });
    }
})();
