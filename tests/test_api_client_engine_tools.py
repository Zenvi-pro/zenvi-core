"""The chat message tells the backend which engine tools the desktop runs, so it is offered them."""

import json
from unittest.mock import MagicMock, patch

from classes import api_client
from classes.editor_tools import engine_bridge as bridged


def _sent(monkeypatch, bridged_tools, setting=""):
    monkeypatch.setattr(bridged, "BRIDGED", {name: {"name": name} for name in bridged_tools})
    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", setting)
    ws = MagicMock()
    ws.recv.return_value = json.dumps({"type": "done", "data": {}})
    client = api_client.ZenviBackendClient(base_url="http://127.0.0.1:8000")
    with patch("websocket.create_connection", return_value=ws):
        client.send_message_ws("make a 20 s launch video", auth_token="tok")
    return json.loads(ws.send.call_args_list[0].args[0])["data"]


def test_a_chat_message_lists_the_engine_tools_the_bridge_runs(monkeypatch):
    data = _sent(monkeypatch, ["get_style_tool", "find_style_tool", "save_brief_tool"])
    assert data["engine_tools"] == ["find_style_tool", "get_style_tool", "save_brief_tool"]


def test_without_the_engine_runtime_a_chat_message_lists_no_engine_tools(monkeypatch):
    assert "engine_tools" not in _sent(monkeypatch, [])


def test_with_the_bridge_turned_off_a_chat_message_lists_no_engine_tools(monkeypatch):
    # Turned off after start-up: the catalog read then is still in memory, but no call would run.
    assert "engine_tools" not in _sent(monkeypatch, ["find_style_tool"], setting="off")
