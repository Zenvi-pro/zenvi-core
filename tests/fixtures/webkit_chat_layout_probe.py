"""Render src/chat_ui in a real Qt WebKit view the way AIChatWindow embeds it and
print layout measurements as JSON. Run by tests/test_chat_webkit_layout.py.

usage: webkit_chat_layout_probe.py WIDTH HEIGHT THEME_JSON
exit 3 when Qt WebKit is not installed (the test skips).
"""
import json
import os
import sys

try:
    from PyQt5.QtCore import QFileInfo, QTimer, QUrl, Qt
    from PyQt5.QtWebKitWidgets import QWebView
    from PyQt5.QtWidgets import QApplication
except ImportError:
    sys.exit(3)

CHAT_UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "chat_ui")
INDEX = os.path.join(CHAT_UI, "index.html")
width, height, theme_json = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]

# Same content the dock gets: a tab strip, a model, a user turn and a reply
# tall enough to overflow the list in one append.
SCENARIO = """
setThemeColors(%s);
setPreamble('<span class="preamble-title">New Chat</span>');
setTabs(JSON.stringify([
    {id: 'a', title: 'Trim the intro', active: true, backend: 'zenvi'},
    {id: 'b', title: 'Colour grade pass', backend: 'zenvi'},
    {id: 'c', title: 'A third chat with a long title', backend: 'zenvi'}]));
setModels(JSON.stringify([{id: 'openai/gpt-5.4', name: 'GPT-5.4', default: true}]));
appendMessage('user', 'Cut the first 10 seconds.', false);
var reply = '';
for (var i = 0; i < 30; i++) reply += '<p>Reply line ' + i + ' with <code>code</code></p>';
appendMessage('assistant', reply + '<p id="probe-last">last line</p>', true);
document.getElementById('chat-tab-history').click();
""" % json.dumps(theme_json)

PROBE = """
(function () {
    function rect(el) {
        var b = el.getBoundingClientRect();
        return {left: b.left, top: b.top, right: b.right, bottom: b.bottom};
    }
    function css(el, prop) { return window.getComputedStyle(el)[prop]; }
    function byId(id) { return document.getElementById(id); }
    return JSON.stringify({
        viewport: {width: window.innerWidth, height: window.innerHeight},
        emptyStateDisplay: css(byId('chat-cli-empty-state'), 'display'),
        messages: rect(byId('chat-messages')),
        messagesBackground: css(byId('chat-messages'), 'backgroundColor'),
        bodyBackground: css(document.body, 'backgroundColor'),
        lastLine: rect(byId('probe-last')),
        inputRow: rect(byId('chat-input-row')),
        send: rect(byId('chat-send-btn')),
        newChatButton: rect(byId('chat-tab-add')),
        historyOverlay: rect(byId('chat-history-overlay')),
        historyPanel: rect(byId('chat-history-panel')),
        inputFont: css(byId('chat-input'), 'fontFamily'),
        codeFont: css(document.querySelector('.chat-message-body code'), 'fontFamily')
    });
})()
"""

app = QApplication(sys.argv)
view = QWebView()
view.setAttribute(Qt.WA_DontShowOnScreen, True)
view.resize(width, height)
view.show()

with open(INDEX, encoding="utf-8") as fh:
    html = fh.read()
# Mirrors AIChatWindow._load_chat_html_for_embed(webkit=True).
html = html.replace("<html ", '<html data-zenvi-webkit="1" ', 1)
html = html.replace("</head>", '\n    <link rel="stylesheet" href="chat-webkit.css">\n</head>', 1)


def measure():
    print(view.page().mainFrame().evaluateJavaScript(PROBE), flush=True)
    app.quit()


def loaded(_ok):
    view.page().mainFrame().evaluateJavaScript(SCENARIO)
    QTimer.singleShot(700, measure)


view.loadFinished.connect(loaded)
view.setHtml(html, QUrl.fromLocalFile(QFileInfo(INDEX).absoluteFilePath()))
QTimer.singleShot(20000, app.quit)
app.exec_()
