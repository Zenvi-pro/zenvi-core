"""The chat websocket loop hands each backend frame to the handler that owns it."""

import json
import sys
import threading
import time
import types
from unittest.mock import patch

from classes import api_client


def _client():
    return api_client.ZenviBackendClient(base_url="https://backend.test")


class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        return self._frames.pop(0) if self._frames else ""

    def ping(self):
        pass

    def close(self):
        pass


def _frame(kind, **data):
    return json.dumps({"type": kind, "data": data})


def _run_ws(frames, **kwargs):
    fake = _FakeWS(frames)
    module = types.SimpleNamespace(create_connection=lambda *a, **k: fake)
    with patch.dict(sys.modules, {"websocket": module}):
        result = _client().send_message_ws("hi", auth_token="t", **kwargs)
    return fake, result


def test_thinking_frames_are_handed_to_on_thinking_in_order():
    got = []
    _run_ws(
        [_frame("thinking", text="Checking the timeline"), _frame("thinking", text=" first."), _frame("done")],
        on_thinking=got.append,
    )
    assert got == ["Checking the timeline", " first."]


def test_thinking_frames_are_ignored_when_nobody_listens():
    fake, result = _run_ws([_frame("thinking", text="just thinking"), _frame("done")])
    assert result is None
    assert fake.sent and fake.sent[0]["type"] == "user_message"


def test_tool_calls_never_run_more_than_four_at_once():
    lock = threading.Lock()
    active = peak = 0

    daemon = []

    def slow_tool(name, args, call_id):
        nonlocal active, peak
        daemon.append(threading.current_thread().daemon)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return "Done " + call_id

    frames = [
        json.dumps({"type": "tool_call", "data": {"call_id": f"c{i}", "tool_name": "slow_tool", "tool_args": {}}})
        for i in range(10)
    ]
    fake, _ = _run_ws(frames + [_frame("done")], on_tool_call=slow_tool)

    results = sorted(m["data"]["call_id"] for m in fake.sent if m.get("type") == "tool_result")
    assert peak <= 4, f"{peak} tool calls ran at once"
    assert all(daemon), "quitting the editor must not wait for a running tool"
    assert results == [f"c{i}" for i in range(10)], "every call's result is sent before the call returns"


def test_a_call_queued_before_stop_never_runs_even_after_a_new_turn():
    from classes.api_client import ZenviBackendClient

    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client._ws_lock = threading.Lock()
    client._active_wss = set()
    client._turn_cancels = set()
    cancelled = threading.Event()
    client._turn_cancels.add(cancelled)
    client.cancel_current_request()
    assert cancelled.is_set() and not client._turn_cancels
