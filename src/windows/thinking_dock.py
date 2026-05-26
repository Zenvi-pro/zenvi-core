"""
Thinking Dock Widget

Real-time display of director thinking, analysis, debate, and voting.
Shows the collaborative decision-making process as it happens.

In the split architecture, the backend sends status updates over WebSocket
and this dock renders them in real-time.
"""

import json
import time
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QMetaObject, Q_ARG
from PyQt5.QtWidgets import QDockWidget, QLabel

from classes.logger import log
from windows.embedded_web import (
    attach_webkit_window_object,
    web_embed_backend,
    run_js,
)


class ThinkingBridge(QObject):
    """Bridge for bidirectional Python<->JavaScript communication."""

    messagePushed = pyqtSignal(str)  # JSON message
    phaseChanged = pyqtSignal(str)   # Phase name

    clearRequested = pyqtSignal()
    pauseRequested = pyqtSignal()

    @pyqtSlot()
    def clear(self):
        """User clicked clear button."""
        self.clearRequested.emit()

    @pyqtSlot()
    def pause(self):
        """User clicked pause button."""
        self.pauseRequested.emit()


class ThinkingDockWidget(QDockWidget):
    """Real-time display of director thinking and communication."""

    pause_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Director Thinking", parent)
        self.setObjectName("thinkingDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        self.web_view = None
        self.bridge = None
        self.channel = None
        self._embed_backend = web_embed_backend()

        if self._embed_backend is None:
            label = QLabel(
                "Qt WebEngine and Qt WebKit are unavailable.\n"
                "Thinking Dock cannot display."
            )
            label.setAlignment(Qt.AlignCenter)
            self.setWidget(label)
            return

        if self._embed_backend == "webengine":
            from PyQt5.QtWebEngineWidgets import QWebEngineView
            from PyQt5.QtWebChannel import QWebChannel

            self.web_view = QWebEngineView()
            self.bridge = ThinkingBridge()
            self.bridge.clearRequested.connect(self._clear)
            self.bridge.pauseRequested.connect(self.pause_requested)
            self.channel = QWebChannel()
            self.channel.registerObject("thinkingBridge", self.bridge)
            self.web_view.page().setWebChannel(self.channel)
            self._load_html_webengine()
        else:
            from PyQt5.QtWebKitWidgets import QWebView

            self.web_view = QWebView()
            self.bridge = ThinkingBridge()
            self.bridge.clearRequested.connect(self._clear)
            self.bridge.pauseRequested.connect(self.pause_requested)
            attach_webkit_window_object(self.web_view, "thinkingBridge", self.bridge)
            self._load_html_webkit()

        self.setWidget(self.web_view)
        self.setMinimumSize(350, 200)

    def _load_html_webengine(self):
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Director Thinking</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            background: #1e1e1e; color: #d4d4d4; font-size: 13px; line-height: 1.5;
        }
        .container { display: flex; flex-direction: column; height: 100vh; }
        .header {
            padding: 12px 16px; background: #252526;
            border-bottom: 1px solid #3c3c3c; flex-shrink: 0;
        }
        .phase {
            font-size: 14px; font-weight: 600; color: #4ec9b0;
            display: flex; align-items: center;
        }
        .phase-icon {
            display: inline-block; width: 8px; height: 8px; border-radius: 50%;
            background: #4ec9b0; margin-right: 8px;
            animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .messages { flex: 1; overflow-y: auto; padding: 12px; }
        .message {
            margin: 8px 0; padding: 10px 12px; background: #2d2d30;
            border-left: 3px solid #007acc; border-radius: 4px;
            animation: slideIn 0.2s ease-out;
        }
        @keyframes slideIn {
            from { opacity: 0; transform: translateX(-10px); }
            to { opacity: 1; transform: translateX(0); }
        }
        .message.analysis { border-color: #4ec9b0; background: #1a2d2d; }
        .message.debate { border-color: #dcdcaa; background: #2d2d1a; }
        .message.voting { border-color: #c586c0; background: #2d1a2d; }
        .message.decision { border-color: #4fc1ff; background: #1a2a2d; }
        .message-header {
            display: flex; justify-content: space-between;
            margin-bottom: 6px; align-items: center;
        }
        .message-role { font-weight: 600; color: #569cd6; font-size: 12px; }
        .message-time { font-size: 11px; color: #858585; }
        .message-content { font-size: 13px; line-height: 1.5; color: #cccccc; }
        .controls {
            padding: 8px 12px; border-top: 1px solid #3c3c3c;
            background: #252526; flex-shrink: 0; display: flex;
        }
        .controls button { margin-right: 8px; }
        button {
            padding: 6px 12px; background: #0e639c; color: white;
            border: none; border-radius: 3px; cursor: pointer;
            font-size: 12px; font-weight: 500; transition: background 0.2s;
        }
        button:hover { background: #1177bb; }
        button:active { background: #0d5a8c; }
        .empty-state {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; height: 100%; color: #858585;
            text-align: center; padding: 20px;
        }
        .empty-icon { font-size: 48px; margin-bottom: 12px; opacity: 0.5; }
        .empty-text { font-size: 14px; }
        .messages::-webkit-scrollbar { width: 8px; }
        .messages::-webkit-scrollbar-track { background: #1e1e1e; }
        .messages::-webkit-scrollbar-thumb { background: #424242; border-radius: 4px; }
        .messages::-webkit-scrollbar-thumb:hover { background: #4e4e4e; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="phase" id="phase">
                <span class="phase-icon"></span> Idle
            </div>
        </div>
        <div id="messages" class="messages">
            <div class="empty-state">
                <div class="empty-icon">&#128173;</div>
                <div class="empty-text">Waiting for director analysis...</div>
            </div>
        </div>
        <div class="controls">
            <button onclick="thinkingBridge.clear()">Clear</button>
            <button onclick="thinkingBridge.pause()">Pause</button>
        </div>
    </div>
    <script src="qrc:/qtwebchannel/qwebchannel.js"></script>
    <script>
        var messagesEl = document.getElementById('messages');
        var phaseEl = document.getElementById('phase');
        var messageCount = 0;
        if (window.qt && window.qt.webChannelTransport) {
            new QWebChannel(window.qt.webChannelTransport, function(channel) {
                window.thinkingBridge = channel.objects.thinkingBridge;
                thinkingBridge.messagePushed.connect(function(jsonStr) {
                    try { var msg = JSON.parse(jsonStr); addMessage(msg); }
                    catch (e) { console.error('Failed to parse message:', e); }
                });
                thinkingBridge.phaseChanged.connect(function(phase) {
                    phaseEl.innerHTML = '<span class="phase-icon"></span>' + escapeHtml(phase);
                });
            });
        }
        function addMessage(msg) {
            if (messageCount === 0) { messagesEl.innerHTML = ''; }
            messageCount++;
            var div = document.createElement('div');
            div.className = 'message ' + (msg.type || 'general');
            var timestamp = new Date(msg.timestamp * 1000).toLocaleTimeString();
            div.innerHTML =
                '<div class="message-header">' +
                '<span class="message-role">' + escapeHtml(msg.role) + '</span>' +
                '<span class="message-time">' + escapeHtml(timestamp) + '</span>' +
                '</div>' +
                '<div class="message-content">' + escapeHtml(msg.content) + '</div>';
            messagesEl.appendChild(div);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        function escapeHtml(text) {
            var div = document.createElement('div');
            div.textContent = text; return div.innerHTML;
        }
        function clearMessages() {
            messagesEl.innerHTML = '<div class="empty-state">' +
                '<div class="empty-icon">&#128173;</div>' +
                '<div class="empty-text">Cleared</div></div>';
            messageCount = 0;
        }
    </script>
</body>
</html>"""
        self.web_view.setHtml(html)

    def _load_html_webkit(self):
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Director Thinking</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            background: #1e1e1e; color: #d4d4d4; font-size: 13px; line-height: 1.5;
        }
        .container { display: -webkit-flex; display: flex; -webkit-flex-direction: column;
            flex-direction: column; height: 100vh; }
        .header {
            padding: 12px 16px; background: #252526;
            border-bottom: 1px solid #3c3c3c;
        }
        .phase {
            font-size: 14px; font-weight: 600; color: #4ec9b0;
            display: -webkit-flex; display: flex; -webkit-align-items: center; align-items: center;
        }
        .phase-icon {
            display: inline-block; width: 8px; height: 8px; border-radius: 50%;
            background: #4ec9b0; margin-right: 8px;
        }
        .messages { -webkit-flex: 1; flex: 1; overflow-y: auto; padding: 12px; }
        .message {
            margin: 8px 0; padding: 10px 12px; background: #2d2d30;
            border-left: 3px solid #007acc; border-radius: 4px;
        }
        .message.analysis { border-color: #4ec9b0; background: #1a2d2d; }
        .message.debate { border-color: #dcdcaa; background: #2d2d1a; }
        .message.voting { border-color: #c586c0; background: #2d1a2d; }
        .message.decision { border-color: #4fc1ff; background: #1a2a2d; }
        .message-header {
            display: -webkit-flex; display: flex; -webkit-justify-content: space-between;
            justify-content: space-between; margin-bottom: 6px; -webkit-align-items: center;
            align-items: center;
        }
        .message-role { font-weight: 600; color: #569cd6; font-size: 12px; }
        .message-time { font-size: 11px; color: #858585; }
        .message-content { font-size: 13px; line-height: 1.5; color: #cccccc; }
        .controls {
            padding: 8px 12px; border-top: 1px solid #3c3c3c;
            background: #252526; display: -webkit-flex; display: flex;
        }
        .controls button { margin-right: 8px; }
        button {
            padding: 6px 12px; background: #0e639c; color: white;
            border: none; border-radius: 3px; cursor: pointer;
            font-size: 12px; font-weight: 500;
        }
        .empty-state {
            display: -webkit-flex; display: flex; -webkit-flex-direction: column;
            flex-direction: column; -webkit-align-items: center; align-items: center;
            -webkit-justify-content: center; justify-content: center;
            height: 100%; color: #858585; text-align: center; padding: 20px;
        }
        .empty-icon { font-size: 48px; margin-bottom: 12px; opacity: 0.5; }
        .empty-text { font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="phase" id="phase">
                <span class="phase-icon"></span> Idle
            </div>
        </div>
        <div id="messages" class="messages">
            <div class="empty-state">
                <div class="empty-icon">&#128173;</div>
                <div class="empty-text">Waiting for director analysis...</div>
            </div>
        </div>
        <div class="controls">
            <button onclick="thinkingBridge.clear()">Clear</button>
            <button onclick="thinkingBridge.pause()">Pause</button>
        </div>
    </div>
    <script>
        var messagesEl = document.getElementById('messages');
        var phaseEl = document.getElementById('phase');
        var messageCount = 0;
        function bootThinking() {
            if (typeof thinkingBridge === 'undefined') {
                setTimeout(bootThinking, 50);
                return;
            }
            thinkingBridge.messagePushed.connect(function(jsonStr) {
                try { var msg = JSON.parse(jsonStr); addMessage(msg); }
                catch (e) { }
            });
            thinkingBridge.phaseChanged.connect(function(phase) {
                phaseEl.innerHTML = '<span class="phase-icon"></span>' + escapeHtml(phase);
            });
        }
        function addMessage(msg) {
            if (messageCount === 0) { messagesEl.innerHTML = ''; }
            messageCount++;
            var div = document.createElement('div');
            div.className = 'message ' + (msg.type || 'general');
            var timestamp = new Date(msg.timestamp * 1000).toLocaleTimeString();
            div.innerHTML =
                '<div class="message-header">' +
                '<span class="message-role">' + escapeHtml(msg.role) + '</span>' +
                '<span class="message-time">' + escapeHtml(timestamp) + '</span>' +
                '</div>' +
                '<div class="message-content">' + escapeHtml(msg.content) + '</div>';
            messagesEl.appendChild(div);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        function escapeHtml(text) {
            var div = document.createElement('div');
            div.textContent = text; return div.innerHTML;
        }
        function clearMessages() {
            messagesEl.innerHTML = '<div class="empty-state">' +
                '<div class="empty-icon">&#128173;</div>' +
                '<div class="empty-text">Cleared</div></div>';
            messageCount = 0;
        }
        bootThinking();
    </script>
</body>
</html>"""
        self.web_view.setHtml(html)

    def add_message(self, role: str, content: str, msg_type: str = "general"):
        if not self.bridge:
            return
        msg = json.dumps({
            "role": role, "content": content,
            "type": msg_type, "timestamp": time.time(),
        })
        QMetaObject.invokeMethod(
            self, "_push_message", Qt.QueuedConnection, Q_ARG(str, msg)
        )

    @pyqtSlot(str)
    def _push_message(self, msg_json: str):
        if self.bridge:
            self.bridge.messagePushed.emit(msg_json)

    def set_phase(self, phase: str):
        if not self.bridge:
            return
        QMetaObject.invokeMethod(
            self, "_set_phase", Qt.QueuedConnection, Q_ARG(str, phase)
        )

    @pyqtSlot(str)
    def _set_phase(self, phase: str):
        if self.bridge:
            self.bridge.phaseChanged.emit(phase)

    def clear(self):
        self._run_js("clearMessages();")

    def _clear(self):
        self.clear()

    def _run_js(self, code: str):
        if not self.web_view or self._embed_backend is None:
            return
        try:
            run_js(self.web_view, self._embed_backend, code)
        except Exception as exc:
            log.debug("thinking_dock run_js: %s", exc)
