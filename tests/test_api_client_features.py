"""The chat message tells the backend which desktop features this build can run."""

import json
from unittest.mock import MagicMock, patch

from classes import api_client


def _features(manim):
    ws = MagicMock()
    ws.recv.return_value = json.dumps({"type": "done", "data": {}})
    client = api_client.ZenviBackendClient(base_url="http://127.0.0.1:8000")
    with patch("websocket.create_connection", return_value=ws), \
         patch("classes.manim_local.manim_program", return_value=manim):
        client.send_message_ws("make a title card", auth_token="tok")
    return json.loads(ws.send.call_args.args[0])["data"]["client_features"]


def test_a_chat_message_advertises_local_motion_graphics():
    assert _features("") == ["local_motion_graphics"]


def test_a_chat_message_advertises_local_manim_only_when_it_is_installed():
    assert _features(r"C:\manim\manim.exe") == ["local_motion_graphics", "local_manim"]
