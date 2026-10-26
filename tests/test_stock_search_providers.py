"""Stock search goes through the backend's cached /stock routes (Openverse music,
Pixabay video); a provider 429 reads as "busy", and Openverse UUID ids import."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.api_client import ZenviBackendClient  # noqa: E402


def _client(status=200, payload=None, headers=None):
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://api/api/v1"
    r = MagicMock(status_code=status, headers=headers or {})
    r.json.return_value = payload or {}
    if status >= 400:
        import requests
        r.raise_for_status.side_effect = requests.HTTPError(f"{status} Client Error: Too Many Requests")
    c._session = MagicMock()
    c._session.get.return_value = r
    c._session.headers = {}
    c.get_auth_token = lambda: ""
    return c


def test_music_and_footage_use_the_stock_routes():
    c = _client(payload={"sounds": []})
    c.freesound_search("ocean")
    assert c._session.get.call_args.args[0] == "http://api/api/v1/stock/music/search"
    c.pexels_search("ocean")
    assert c._session.get.call_args.args[0] == "http://api/api/v1/stock/videos/search"


def test_rate_limit_reads_as_busy_not_a_raw_429():
    c = _client(status=429, headers={"Retry-After": "30"})
    out = c.freesound_search("ocean")
    assert out["rate_limited"] is True
    assert "429" not in out["error"]
    assert "30" in out["error"]
    out = c.pexels_search("ocean")
    assert out["rate_limited"] is True and "429" not in out["error"]


def test_agent_download_accepts_an_openverse_uuid():
    from classes import tool_handlers

    backend = MagicMock()
    backend.freesound_download.return_value = {"local_path": "/tmp/a.mp3"}
    with patch("classes.api_client.get_backend_client", return_value=backend), \
         patch("classes.credits_client.check_operation", return_value=(0, 0, "")), \
         patch("classes.credits_client.charge_operation_on_success"):
        out = tool_handlers.download_freesound_music_tool(
            sound_id="3b4580e1-1454-4b4c", preview_url="https://prod-1.storage.jamendo.com/?trackid=1&format=mp32"
        )
    assert out == "Downloaded to: /tmp/a.mp3"
    assert backend.freesound_download.call_args.args[0] == "3b4580e1-1454-4b4c"
