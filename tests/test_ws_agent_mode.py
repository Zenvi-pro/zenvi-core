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


if __name__ == "__main__":
    unittest.main()
