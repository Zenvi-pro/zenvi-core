"""Bring-your-own-key vault + Higgsfield routing for AI video generation (#60)."""

import base64
import json
import logging
import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from classes import provider_keys
from classes.api_client import ZenviBackendClient

KEY = "kid-123:secret-456"
KEY_PARTS = (KEY, "kid-123", "secret-456")


class _FakeKeychain:
    """In-memory stand-in for the ``keyring`` module (never the real OS keychain)."""

    def __init__(self):
        self.items = {}
        self.fail_get = None
        self.fail_delete = None

    def set_password(self, service, user, secret):
        self.items[(service, user)] = secret

    def get_password(self, service, user):
        if self.fail_get:
            raise self.fail_get
        return self.items.get((service, user))

    def delete_password(self, service, user):
        if self.fail_delete:
            raise self.fail_delete
        del self.items[(service, user)]


def _fake_dpapi(data, protect):
    if protect:
        return b"DPAPI:" + data[::-1]
    if not data.startswith(b"DPAPI:"):
        raise OSError("DPAPI call failed")
    return data[len(b"DPAPI:"):][::-1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "provider_keys.json"
    monkeypatch.setattr(provider_keys, "_store_path", lambda: str(path))
    return path


@pytest.fixture
def keychain(store, monkeypatch):
    kc = _FakeKeychain()
    monkeypatch.setattr(provider_keys, "_keyring", lambda: kc)
    return kc


@pytest.fixture
def dpapi_vault(store, monkeypatch):
    """Windows' file fallback (Credential Manager unusable), DPAPI faked so it runs anywhere."""
    monkeypatch.setattr(provider_keys, "_keyring", lambda: None)
    monkeypatch.setattr(provider_keys, "_file_store_available", lambda: True)
    monkeypatch.setattr(provider_keys, "_dpapi", _fake_dpapi)
    return store


def test_keychain_round_trip_writes_no_file(keychain, store):
    assert provider_keys.get_key("higgsfield") == ""
    provider_keys.set_key("higgsfield", KEY)
    assert keychain.items == {(provider_keys.KEYRING_SERVICE, "higgsfield"): KEY}
    assert provider_keys.get_key("higgsfield") == KEY
    assert not store.exists()
    provider_keys.clear_key("higgsfield")
    assert provider_keys.get_key("higgsfield") == ""
    assert keychain.items == {}


def test_windows_file_store_is_encrypted_private_and_swapped_in_whole(dpapi_vault):
    provider_keys.set_key("higgsfield", KEY)
    blob = json.loads(dpapi_vault.read_text())["higgsfield"]
    assert base64.b64decode(blob) == _fake_dpapi(KEY.encode(), True)  # went through DPAPI
    assert provider_keys.get_key("higgsfield") == KEY
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(dpapi_vault).st_mode) == 0o600
    assert [p.name for p in dpapi_vault.parent.iterdir()] == ["provider_keys.json"]  # no temp left

    # A failed write leaves the previous store intact, not half-written.
    with patch("classes.provider_keys.json.dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            provider_keys.set_key("higgsfield", "other-kid:other-secret")
    assert provider_keys.get_key("higgsfield") == KEY
    assert [p.name for p in dpapi_vault.parent.iterdir()] == ["provider_keys.json"]

    provider_keys.clear_key("higgsfield")
    assert provider_keys.get_key("higgsfield") == ""
    assert "higgsfield" not in json.loads(dpapi_vault.read_text())


def test_off_windows_without_a_keychain_the_key_is_refused_not_written(store, monkeypatch):
    monkeypatch.setattr(provider_keys, "_keyring", lambda: None)
    monkeypatch.setattr(provider_keys, "_file_store_available", lambda: False)
    with pytest.raises(provider_keys.KeyStoreUnavailable):
        provider_keys.set_key("higgsfield", KEY)
    validate = MagicMock(return_value={"ok": True})
    ok, msg = provider_keys.test_and_save("higgsfield", KEY, validate)
    assert ok is False and "No system keychain" in msg
    validate.assert_not_called()  # no point testing a key that cannot be kept
    assert not store.exists()


def test_off_windows_an_old_unencrypted_file_copy_is_never_read(store, monkeypatch):
    store.write_text(json.dumps({"higgsfield": base64.b64encode(KEY.encode()).decode()}))
    monkeypatch.setattr(provider_keys, "_keyring", lambda: None)
    monkeypatch.setattr(provider_keys, "_file_store_available", lambda: False)
    assert provider_keys.get_key("higgsfield", strict=True) == ""


def test_saving_to_the_keychain_drops_a_leftover_file_copy(keychain, store):
    store.write_text(json.dumps({"higgsfield": base64.b64encode(KEY.encode()).decode()}))
    provider_keys.set_key("higgsfield", "new-kid:new-secret")
    assert "higgsfield" not in json.loads(store.read_text())
    assert provider_keys.get_key("higgsfield") == "new-kid:new-secret"


def test_remove_clears_both_stores_and_a_refused_delete_is_not_success(keychain, store):
    store.write_text(json.dumps({"higgsfield": base64.b64encode(KEY.encode()).decode()}))
    provider_keys.set_key("higgsfield", KEY)
    store.write_text(json.dumps({"higgsfield": "left-over"}))
    provider_keys.clear_key("higgsfield")
    assert keychain.items == {}
    assert "higgsfield" not in json.loads(store.read_text())

    provider_keys.set_key("higgsfield", KEY)
    keychain.fail_delete = RuntimeError("keychain locked")
    with pytest.raises(RuntimeError):
        provider_keys.clear_key("higgsfield")
    assert keychain.items  # still stored, and Remove said so


def test_unknown_provider_is_rejected(keychain):
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


def _client(session, token="jwt-1"):
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://x/api/v1"
    c._session = session
    c._auth_token = lambda: token
    return c


def test_generate_video_sends_the_key_as_a_header_not_in_the_payload():
    c = _client(_fake_session({"video_url": "u"}))
    c.generate_video("ocean", duration_seconds=5, provider="higgsfield", provider_key=KEY)
    kwargs = c._session.post.call_args.kwargs
    # The backend gates /generation on the signed-in user, so BYOK carries the JWT.
    assert kwargs["headers"] == {"X-Zenvi-Provider-Key": KEY, "Authorization": "Bearer jwt-1"}
    assert kwargs["json"]["provider"] == "higgsfield"
    assert "secret-456" not in json.dumps(kwargs["json"])


def test_managed_generate_video_sends_no_provider_key():
    c = _client(_fake_session({"video_url": "u"}))
    c.generate_video("ocean", duration_seconds=5, mode="t2v")
    kwargs = c._session.post.call_args.kwargs
    assert "provider" not in kwargs["json"]
    assert "X-Zenvi-Provider-Key" not in (kwargs["headers"] or {})


def test_validate_provider_key_posts_to_the_validate_route():
    c = _client(_fake_session({"ok": True, "error": None}))
    assert c.validate_provider_key("higgsfield", KEY) == {"ok": True, "error": None}
    args, kwargs = c._session.post.call_args
    assert args[0] == "http://x/api/v1/generation/providers/higgsfield/validate"
    assert kwargs["headers"] == {"X-Zenvi-Provider-Key": KEY, "Authorization": "Bearer jwt-1"}


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


def test_test_and_save_only_activates_a_key_the_provider_accepts(keychain):
    ok, msg = provider_keys.test_and_save(
        "higgsfield", KEY, lambda p, k: {"ok": False, "error": "Invalid credentials"},
    )
    assert ok is False and msg == "Higgsfield rejected the key: Invalid credentials"
    assert provider_keys.get_key("higgsfield") == ""

    ok, msg = provider_keys.test_and_save("higgsfield", KEY, lambda p, k: {"ok": True})
    assert ok is True and "••••-456" in msg
    assert provider_keys.get_key("higgsfield") == KEY

    ok, msg = provider_keys.test_and_save("higgsfield", "  ", lambda p, k: {"ok": True})
    assert ok is False
    assert provider_keys.get_key("higgsfield") == KEY  # blank input never wipes a working key


def test_backend_or_network_failure_is_not_called_a_rejection(keychain):
    ok, msg = provider_keys.test_and_save(
        "higgsfield", KEY,
        lambda p, k: {"ok": False, "unverified": True, "error": "Zenvi backend returned 404: Not Found"},
    )
    assert ok is False
    assert msg == "Could not test the Higgsfield key: Zenvi backend returned 404: Not Found"
    assert provider_keys.get_key("higgsfield") == ""


def test_a_failed_store_after_an_accepted_key_is_reported(keychain):
    keychain.set_password = MagicMock(side_effect=RuntimeError("keychain locked"))
    ok, msg = provider_keys.test_and_save("higgsfield", KEY, lambda p, k: {"ok": True})
    assert ok is False
    assert msg == "Higgsfield accepted the key, but it could not be saved (RuntimeError)."
    assert provider_keys.get_key("higgsfield") == ""


def test_errors_that_echo_the_key_never_reach_ui_or_tool_output(keychain):
    ok, msg = provider_keys.test_and_save(
        "higgsfield", KEY, lambda p, k: {"ok": False, "error": f"nope {KEY}"},
    )
    assert ok is False and "secret-456" not in msg

    c = _client(MagicMock())
    c._session.post.side_effect = RuntimeError(f"boom {KEY}")
    out = c.generate_video("ocean", provider="higgsfield", provider_key=KEY)
    assert "secret-456" not in out["error"]


def _http_error(status, body):
    import requests

    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status.side_effect = requests.HTTPError(str(status), response=resp)
    return resp


def test_validate_sends_a_json_body_and_reads_4xx_errors():
    c = _client(MagicMock())
    c._session.post.return_value = _http_error(404, {"detail": "Not Found"})
    out = c.validate_provider_key("higgsfield", KEY)
    assert c._session.post.call_args.kwargs["json"] == {}
    assert out == {"ok": False, "unverified": True, "error": "Zenvi backend returned 404: Not Found"}


def test_validate_needs_zenvi_sign_in_and_says_so():
    c = _client(MagicMock(), token=None)
    c._session.post.return_value = _http_error(401, {"detail": "auth required"})
    out = c.validate_provider_key("higgsfield", KEY)
    assert "Authorization" not in c._session.post.call_args.kwargs["headers"]
    assert out == {"ok": False, "unverified": True, "error": "sign in to Zenvi first"}


def test_error_on_a_200_body_is_redacted_and_non_dict_json_is_handled():
    c = _client(_fake_session({"error": f"echo {KEY}"}))
    out = c.generate_video("ocean", provider="higgsfield", provider_key=KEY)
    assert "secret-456" not in out["error"]

    c._session = _fake_session(["not", "a", "dict"])
    assert "error" in c.generate_video("ocean")
    assert c.validate_provider_key("higgsfield", KEY)["ok"] is False


def test_an_unreadable_stored_key_fails_instead_of_billing_zenvi(dpapi_vault, keychain):
    # Windows file store: an entry DPAPI cannot decrypt.
    dpapi_vault.write_text(json.dumps({"higgsfield": "////"}))
    with patch.object(provider_keys, "_keyring", lambda: None):
        with pytest.raises(provider_keys.KeyUnreadable):
            provider_keys.get_key("higgsfield", strict=True)
        assert provider_keys.get_key("higgsfield") == ""  # UI path stays forgiving

        from classes import tool_handlers as th
        kwargs, err = th._byok_generation_kwargs()
    assert kwargs == {} and "could not be read" in err

    # Keychain: a read that errors (locked, denied) is also never "no key".
    keychain.fail_get = RuntimeError("keychain locked")
    with pytest.raises(provider_keys.KeyUnreadable):
        provider_keys.get_key("higgsfield", strict=True)


def test_an_unusable_keyring_backend_counts_as_no_keychain(monkeypatch):
    import sys
    import types

    class FailKeyring:
        pass

    fail_mod = types.ModuleType("keyring.backends.fail")
    fail_mod.Keyring = FailKeyring
    fake = types.ModuleType("keyring")
    fake.get_keyring = lambda: FailKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setitem(sys.modules, "keyring.backends", types.ModuleType("keyring.backends"))
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_mod)
    assert provider_keys._keyring() is None


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        self.lines.append(text)


def test_the_key_never_reaches_logs_tool_results_or_chat(keychain):
    """#60 security list, end to end through execute_tool (the path chat and MCP use).

    The tool transcript (_ToolLogCapture) is fed from the OpenShot logger and the
    tool result is what chat shows and sends back as context, so neither may carry
    the key, its id, or its secret, whatever the backend or keychain echoes.
    """
    from classes import tool_handlers as th
    from classes.logger import log as zenvi_log

    provider_keys.set_key("higgsfield", KEY)
    records = _Records()
    old_level = zenvi_log.level
    zenvi_log.addHandler(records)
    logging.getLogger().addHandler(records)
    zenvi_log.setLevel(logging.DEBUG)

    gui = object()

    class _Thread:
        @staticmethod
        def currentThread():
            return gui

    app = MagicMock()
    app.thread.return_value = gui
    outputs = []
    try:
        with patch.object(th, "QThread", _Thread), patch.object(th, "QEventLoop", object), \
             patch.object(th, "_get_app", return_value=app), \
             patch.object(th, "_atomic", lambda _app, fn: fn), \
             patch.object(th, "_project_kling_o1_t2v_dims", return_value=(1920, 1080)), \
             patch.object(th, "_output_path_for_generated_video", return_value="out.mp4"), \
             patch.object(th, "_pause_auto_save", return_value=False), \
             patch.object(th, "_resume_auto_save"), \
             patch("classes.credits_client.check_operation", MagicMock()), \
             patch("classes.credits_client.credits", MagicMock()):
            for session in (
                _fake_session({"error": f"Higgsfield API error 401: bad key {KEY}"}),
                MagicMock(post=MagicMock(side_effect=RuntimeError(f"connection reset {KEY}"))),
            ):
                client = _client(session)
                with patch("classes.api_client.get_backend_client", return_value=client):
                    outputs.append(th.execute_tool(
                        "generate_video_and_add_to_timeline_tool", {"prompt": "ocean waves"},
                    ))
                # The request did carry the key, as a header only.
                assert session.post.call_args.kwargs["headers"]["X-Zenvi-Provider-Key"] == KEY

        # Integrations dialog path: rejected and unreachable validations.
        for session in (
            _fake_session({"ok": False, "error": f"Invalid credentials for {KEY}"}),
            MagicMock(post=MagicMock(side_effect=RuntimeError(f"tls {KEY}"))),
        ):
            outputs.append(provider_keys.test_and_save("higgsfield", KEY, _client(session).validate_provider_key)[1])
        # A keychain error that quotes the secret.
        keychain.fail_get = RuntimeError(f"keychain says {KEY}")
        assert provider_keys.get_key("higgsfield") == ""
    finally:
        zenvi_log.removeHandler(records)
        logging.getLogger().removeHandler(records)
        zenvi_log.setLevel(old_level)

    assert outputs[0].startswith("Error: Higgsfield API error 401")
    assert outputs[1].startswith("Error: connection reset")
    assert records.lines, "the calls above must have logged something to check"
    for text in outputs + records.lines:
        for part in KEY_PARTS:
            assert part not in text, f"key material in: {text!r}"
