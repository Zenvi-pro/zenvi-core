"""Frontend: agent_mode is included in WebSocket user_message payload."""

import unittest


class TestWsPayloadAgentMode(unittest.TestCase):
    def test_payload_includes_agent_mode(self):
        payload_data = {
            "message": "hello",
            "model_id": "test-model",
            "session_id": "s1",
            "auth_token": "tok",
            "agent_mode": "planning",
            "action": "chat",
        }
        envelope = {"type": "user_message", "data": payload_data}
        self.assertEqual(envelope["data"]["agent_mode"], "planning")
        self.assertEqual(envelope["data"]["action"], "chat")

    def test_execute_plan_action_payload(self):
        payload_data = {
            "message": "",
            "session_id": "s1",
            "agent_mode": "agent",
            "action": "execute_plan",
            "plan_id": "plan-123",
        }
        self.assertEqual(payload_data["action"], "execute_plan")
        self.assertEqual(payload_data["plan_id"], "plan-123")


def test_send_message_ws_sends_the_project_id_only_when_known(monkeypatch):
    import json
    import sys
    import types

    from classes import api_client

    sent = []

    class _FakeWs:
        def send(self, raw):
            sent.append(json.loads(raw))

        def recv(self):
            return json.dumps({"type": "done", "data": {}})

        def ping(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "websocket",
                        types.SimpleNamespace(create_connection=lambda *a, **k: _FakeWs()))
    client = api_client.ZenviBackendClient(base_url="https://backend.test")
    client.send_message_ws("hi", auth_token="tok", project_id="P1")
    client.send_message_ws("hi", auth_token="tok")

    with_id, without_id = [f["data"] for f in sent if f["type"] == "user_message"]
    assert with_id["project_id"] == "P1"
    assert "project_id" not in without_id


if __name__ == "__main__":
    unittest.main()
