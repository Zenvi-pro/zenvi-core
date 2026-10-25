"""The chat message tells the backend which desktop features this build can run."""

import json
from unittest.mock import MagicMock, patch

from classes import api_client


def test_a_chat_message_advertises_local_motion_graphics():
    ws = MagicMock()
    ws.recv.return_value = json.dumps({"type": "done", "data": {}})
    client = api_client.ZenviBackendClient(base_url="http://127.0.0.1:8000")
    with patch("websocket.create_connection", return_value=ws):
        client.send_message_ws("make a title card", auth_token="tok")
    user_message = json.loads(ws.send.call_args.args[0])
    assert user_message["type"] == "user_message"
    assert user_message["data"]["client_features"] == ["local_motion_graphics"]
