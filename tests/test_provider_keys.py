"""Bring-your-own-key vault + Higgsfield routing for AI video generation (#60)."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from classes import provider_keys
from classes.api_client import ZenviBackendClient

KEY = "kid-123:secret-456"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_keys, "_keyring", lambda: None)
    monkeypatch.setattr(provider_keys, "_store_path", lambda: str(tmp_path / "provider_keys.json"))
    return tmp_path / "provider_keys.json"


def test_set_get_clear_round_trip_never_stores_the_raw_key(vault):
    assert provider_keys.get_key("higgsfield") == ""
    provider_keys.set_key("higgsfield", KEY)
    assert provider_keys.get_key("higgsfield") == KEY
    raw = vault.read_bytes()
    if os.name == "nt":
        assert b"secret-456" not in raw  # DPAPI-protected on Windows
    provider_keys.clear_key("higgsfield")
    assert provider_keys.get_key("higgsfield") == ""
    assert "higgsfield" not in json.loads(vault.read_text() or "{}")


def test_unknown_provider_is_rejected(vault):
    with pytest.raises(KeyError):
        provider_keys.set_key("nope", "x")


def test_mask_shows_only_the_tail():
    assert provider_keys.mask(KEY) == "••••-456"
    assert provider_keys.mask("") == ""
    assert "secret" not in provider_keys.mask(KEY)


def _fake_session(json_body):
    s = MagicMock()
    resp = MagicMock()
    resp.json.return_value = json_body
    s.post.return_value = resp
    return s


def test_generate_video_sends_the_key_as_a_header_not_in_the_payload():
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://x/api/v1"
    c._session = _fake_session({"video_url": "u"})
    c.generate_video("ocean", duration_seconds=5, provider="higgsfield", provider_key=KEY)
    kwargs = c._session.post.call_args.kwargs
    assert kwargs["headers"] == {"X-Zenvi-Provider-Key": KEY}
    assert kwargs["json"]["provider"] == "higgsfield"
    assert "secret-456" not in json.dumps(kwargs["json"])


def test_validate_provider_key_posts_to_the_validate_route():
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://x/api/v1"
    c._session = _fake_session({"ok": True, "error": None})
    assert c.validate_provider_key("higgsfield", KEY) == {"ok": True, "error": None}
    args, kwargs = c._session.post.call_args
    assert args[0] == "http://x/api/v1/generation/providers/higgsfield/validate"
    assert kwargs["headers"] == {"X-Zenvi-Provider-Key": KEY}


def _run_t2v(stored_key):
    from classes import tool_handlers as th

    client = MagicMock()
    client.generate_video.return_value = {"video_url": "https://cdn/out.mp4"}
    check = MagicMock(return_value=(None, None, None))
    charge = MagicMock()
    with patch.object(th, "QThread", object), patch.object(th, "QEventLoop", object), \
         patch.object(th, "_get_app", return_value=MagicMock()), \
         patch.object(th, "_project_kling_o1_t2v_dims", return_value=(1920, 1080)), \
         patch.object(th, "_output_path_for_generated_video", return_value="out.mp4"), \
         patch.object(th, "_pause_auto_save", return_value=False), \
         patch.object(th, "_resume_auto_save"), \
         patch.object(th, "_download_video_url_to_path", return_value=None), \
         patch.object(th, "_import_generated_video", return_value=(None, "stop here")), \
         patch("classes.provider_keys.get_key", return_value=stored_key), \
         patch("classes.api_client.get_backend_client", return_value=client), \
         patch("classes.credits_client.check_operation", check), \
         patch("classes.credits_client.charge_operation_on_success", charge), \
         patch("classes.credits_client.credits", MagicMock()):
        out = th.generate_video_and_add_to_timeline(prompt="ocean waves")
    return out, client, check, charge


def test_t2v_with_own_higgsfield_key_skips_zenvi_credits():
    out, client, check, charge = _run_t2v(KEY)
    assert "stop here" in out
    kwargs = client.generate_video.call_args.kwargs
    assert kwargs["provider"] == "higgsfield"
    assert kwargs["provider_key"] == KEY
    check.assert_not_called()
    charge.assert_not_called()


def test_t2v_without_own_key_keeps_managed_credits_path():
    out, client, check, charge = _run_t2v("")
    assert "provider" not in client.generate_video.call_args.kwargs
    check.assert_called_once()
    charge.assert_called_once()


def test_test_and_save_only_activates_a_key_the_provider_accepts(vault):
    ok, msg = provider_keys.test_and_save(
        "higgsfield", KEY, lambda p, k: {"ok": False, "error": "Invalid credentials"},
    )
    assert ok is False and "Invalid credentials" in msg
    assert provider_keys.get_key("higgsfield") == ""

    ok, msg = provider_keys.test_and_save("higgsfield", KEY, lambda p, k: {"ok": True})
    assert ok is True and "••••-456" in msg
    assert provider_keys.get_key("higgsfield") == KEY

    ok, msg = provider_keys.test_and_save("higgsfield", "  ", lambda p, k: {"ok": True})
    assert ok is False
    assert provider_keys.get_key("higgsfield") == KEY  # blank input never wipes a working key


def test_errors_that_echo_the_key_never_reach_ui_or_tool_output(vault):
    ok, msg = provider_keys.test_and_save(
        "higgsfield", KEY, lambda p, k: {"ok": False, "error": f"nope {KEY}"},
    )
    assert ok is False and "secret-456" not in msg

    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://x/api/v1"
    c._session = MagicMock()
    c._session.post.side_effect = RuntimeError(f"boom {KEY}")
    out = c.generate_video("ocean", provider="higgsfield", provider_key=KEY)
    assert "secret-456" not in out["error"]


def test_validate_sends_a_json_body_and_reads_4xx_errors():
    import requests

    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://x/api/v1"
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"detail": "Not authenticated"}
    resp.raise_for_status.side_effect = requests.HTTPError("401", response=resp)
    c._session = MagicMock()
    c._session.post.return_value = resp
    out = c.validate_provider_key("higgsfield", KEY)
    assert c._session.post.call_args.kwargs["json"] == {}
    assert out["ok"] is False
    assert "Not authenticated" in out["error"]
