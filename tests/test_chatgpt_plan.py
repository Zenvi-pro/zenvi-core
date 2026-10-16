"""Sign in with ChatGPT (classes.chatgpt_plan): the OAuth sign-in, token refresh,
the private Codex download, and the environment the agent runs in.

No network: the token endpoint, the browser and the download are faked.
"""

import base64
import hashlib
import io
import json
import os
import tarfile
import threading
import time
import urllib.parse
import urllib.request

import pytest

from classes import chatgpt_plan, info


@pytest.fixture(autouse=True)
def _private_home(monkeypatch, tmp_path):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / ".openshot_qt"))
    monkeypatch.setattr(chatgpt_plan, "CALLBACK_PORT", 0)   # any free port


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return "%s.%s.sig" % (enc({"alg": "RS256"}), enc(claims))


def _token_endpoint(monkeypatch, responses):
    """Answer each token request with the next of *responses*; record the forms."""
    calls = []

    def post(url, fields):
        calls.append((url, dict(fields)))
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(fields) if callable(reply) else reply

    monkeypatch.setattr(chatgpt_plan, "_post_form", post)
    return calls


def _browser(callback_extra=None, state=None):
    """A browser that 'signs in' by hitting the redirect URI with a code."""
    seen = {}

    def open_browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(query)
        params = {"code": "the-code", "state": state or query["state"],
                  "client_id": "oaiapp_issued",
                  "scope": chatgpt_plan.SCOPES}
        params.update(callback_extra or {})
        target = query["redirect_uri"] + "?" + urllib.parse.urlencode(params)

        def hit():
            try:
                urllib.request.urlopen(target, timeout=5).read()
            except Exception:
                pass
        threading.Thread(target=hit, daemon=True).start()
        return True

    return open_browser, seen


def _issue(fields, scope=chatgpt_plan.SCOPES, nonce=None, browser_seen=None):
    return {
        "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": scope,
        "id_token": _jwt({"iss": "https://auth.openai.com", "aud": fields["client_id"],
                          "exp": time.time() + 3600, "email": "me@example.com",
                          "nonce": nonce if nonce is not None else browser_seen["nonce"]}),
    }


def test_sign_in_registers_with_pkce_and_saves_the_plan_tokens(monkeypatch):
    open_browser, seen = _browser()
    calls = _token_endpoint(monkeypatch, [lambda f: _issue(f, browser_seen=seen)])

    ok, message = chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)

    assert ok, message
    # First registration goes through the dynamic-client entrypoint, on 127.0.0.1, with PKCE.
    assert seen["client_id"] == "dynamic_agent_client"
    assert seen["agent_name_hint"] == "Zenvi"
    assert seen["ext_agent_host_id"].startswith("urn:uuid:")   # OpenAI rejects a bare UUID
    assert seen["scope"] == chatgpt_plan.SCOPES
    assert "chatgpt.tokens.use.direct" in seen["scope"]
    assert seen["resource"] == "https://api.openai.com/v1"
    assert seen["code_challenge_method"] == "S256"
    assert urllib.parse.urlsplit(seen["redirect_uri"]).hostname == "127.0.0.1"
    # The code is exchanged with the issued client id and the verifier behind the challenge.
    url, form = calls[0]
    assert url == "https://auth.openai.com/api/accounts/oauth/token"
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == "oaiapp_issued"
    assert form["redirect_uri"] == seen["redirect_uri"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == seen["code_challenge"]
    assert chatgpt_plan.signed_in()
    assert chatgpt_plan.access_token() == "at-1"
    assert "me@example.com" in message


def test_sign_in_refuses_a_grant_without_plan_usage(monkeypatch):
    open_browser, seen = _browser(callback_extra={"scope": "openid profile email offline_access"})
    _token_endpoint(monkeypatch, [lambda f: _issue(f, scope="openid profile email offline_access",
                                                   browser_seen=seen)])

    ok, message = chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)

    assert not ok
    assert "plan usage" in message.lower()
    assert not chatgpt_plan.signed_in()


def test_sign_in_rejects_a_forged_state_without_exchanging(monkeypatch):
    open_browser, _ = _browser(state="not-ours")
    calls = _token_endpoint(monkeypatch, [])

    ok, _message = chatgpt_plan.sign_in(open_browser=open_browser, timeout=3)

    assert not ok, "a reply for another attempt is ignored until the wait times out"
    assert calls == []
    assert not chatgpt_plan.signed_in()


def test_sign_in_rejects_an_id_token_for_another_nonce(monkeypatch):
    open_browser, seen = _browser()
    _token_endpoint(monkeypatch, [lambda f: _issue(f, nonce="replayed", browser_seen=seen)])

    ok, _message = chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)

    assert not ok
    assert not chatgpt_plan.signed_in()


def test_signing_in_again_reuses_the_issued_client_and_host(monkeypatch):
    open_browser, first = _browser()
    _token_endpoint(monkeypatch, [lambda f: _issue(f, browser_seen=first)])
    assert chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)[0]

    open_browser, again = _browser(callback_extra={"client_id": ""})
    _token_endpoint(monkeypatch, [lambda f: _issue(f, browser_seen=again)])
    assert chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)[0]

    assert again["client_id"] == "oaiapp_issued"
    assert "agent_name_hint" not in again
    assert again["id_token_hint"]
    assert again["ext_agent_host_id"] == first["ext_agent_host_id"]


def test_a_refused_code_keeps_the_issued_client_for_the_next_attempt(monkeypatch):
    """invalid_grant: drop the code, but the next sign-in reauthorizes the client it registered."""
    open_browser, first = _browser()
    _token_endpoint(monkeypatch, [chatgpt_plan.OAuthError("invalid_grant")])
    ok, _message = chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)
    assert not ok and not chatgpt_plan.signed_in()

    open_browser, again = _browser(callback_extra={"client_id": ""})
    _token_endpoint(monkeypatch, [lambda f: _issue(f, browser_seen=again)])
    assert chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)[0]
    assert again["client_id"] == "oaiapp_issued"
    assert again["ext_agent_host_id"] == first["ext_agent_host_id"]


def _signed_in(monkeypatch, expires_in):
    open_browser, seen = _browser()
    _token_endpoint(monkeypatch, [lambda f: dict(_issue(f, browser_seen=seen), expires_in=expires_in)])
    assert chatgpt_plan.sign_in(open_browser=open_browser, timeout=10)[0]


def test_an_expiring_token_is_refreshed_and_the_rotated_refresh_token_kept(monkeypatch):
    _signed_in(monkeypatch, expires_in=30)
    calls = _token_endpoint(monkeypatch, [
        {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600},
        {"access_token": "at-3", "refresh_token": "rt-3", "expires_in": 3600},
    ])

    assert chatgpt_plan.access_token() == "at-2"
    assert chatgpt_plan.access_token() == "at-2"     # fresh now: no second refresh
    form = calls[0][1]
    assert form == {"grant_type": "refresh_token", "client_id": "oaiapp_issued",
                    "refresh_token": "rt-1", "resource": "https://api.openai.com/v1"}
    assert len(calls) == 1
    assert chatgpt_plan._load()["refresh_token"] == "rt-2"


def test_a_dead_refresh_token_signs_out_but_a_network_error_does_not(monkeypatch):
    _signed_in(monkeypatch, expires_in=30)
    _token_endpoint(monkeypatch, [OSError("offline")])
    with pytest.raises(OSError):
        chatgpt_plan.access_token()
    assert chatgpt_plan.signed_in(), "a network blip must not erase the sign-in"

    _token_endpoint(monkeypatch, [chatgpt_plan.OAuthError("refresh_token_expired")])
    with pytest.raises(chatgpt_plan.SignInRequired):
        chatgpt_plan.access_token()
    assert not chatgpt_plan.signed_in()
    assert chatgpt_plan._load()["client_id"] == "oaiapp_issued", "kept for the next sign-in"


def _package(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o755
            tar.addfile(entry, io.BytesIO(data))
    return buf.getvalue()


def _serve_package(monkeypatch, blob, digest=None):
    target = ("windows", "amd64")
    monkeypatch.setattr(chatgpt_plan, "_target", lambda: target)
    monkeypatch.setitem(chatgpt_plan.CODEX_PACKAGES, target,
                        ("codex-package-test.tar.gz", digest or hashlib.sha256(blob).hexdigest()))
    urls = []

    def download(url, dest):
        urls.append(url)
        with open(dest, "wb") as fh:
            fh.write(blob)

    monkeypatch.setattr(chatgpt_plan, "_download", download)
    return urls


def test_install_unpacks_the_pinned_package_into_the_private_folder(monkeypatch):
    exe = "codex.exe" if os.name == "nt" else "codex"
    urls = _serve_package(monkeypatch, _package({"bin/" + exe: b"MZ", "codex-path/rg": b"rg"}))

    path = chatgpt_plan.install()

    assert path == chatgpt_plan.codex_path()
    assert os.path.isfile(path)
    assert path.startswith(os.path.join(info.USER_PATH, "chatgpt"))
    assert urls == ["https://github.com/openai/codex/releases/download/rust-v%s/codex-package-test.tar.gz"
                    % chatgpt_plan.CODEX_VERSION]
    assert chatgpt_plan.install() == path and len(urls) == 1, "installed once"


def test_install_refuses_a_package_whose_hash_does_not_match(monkeypatch):
    _serve_package(monkeypatch, _package({"bin/codex": b"evil"}), digest="0" * 64)

    with pytest.raises(RuntimeError, match="checksum"):
        chatgpt_plan.install()
    assert not os.path.exists(chatgpt_plan.codex_path())


def test_install_refuses_paths_that_escape_the_folder(monkeypatch):
    _serve_package(monkeypatch, _package({"../outside": b"x", "bin/codex": b"x"}))

    with pytest.raises(RuntimeError):
        chatgpt_plan.install()
    assert not os.path.exists(os.path.join(info.USER_PATH, "chatgpt", "outside"))


def test_every_supported_platform_pins_a_package_hash():
    for (system, machine), (asset, digest) in chatgpt_plan.CODEX_PACKAGES.items():
        assert asset.startswith("codex-package-") and asset.endswith(".tar.gz")
        assert len(digest) == 64 and int(digest, 16) >= 0


def test_child_env_is_isolated_from_the_users_own_codex(monkeypatch):
    _signed_in(monkeypatch, expires_in=3600)
    base = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-user", "CODEX_API_KEY": "ck",
            "CODEX_HOME": "/home/me/.codex"}

    env = chatgpt_plan.child_env(base)

    assert env["CODEX_HOME"] == chatgpt_plan.codex_home()
    assert os.path.isdir(env["CODEX_HOME"])
    assert env["ACCESS_TOKEN"] == "at-1"
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert base["CODEX_HOME"] == "/home/me/.codex", "caller's env untouched"


def test_provider_args_route_codex_to_the_responses_api_with_the_plan_token():
    args = chatgpt_plan.provider_args()
    overrides = [args[i + 1] for i, a in enumerate(args) if a == "-c"]

    assert 'model_provider="openai_chatgpt_plan"' in overrides
    assert 'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"' in overrides
    assert 'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"' in overrides
    assert 'model_providers.openai_chatgpt_plan.wire_api="responses"' in overrides
    assert 'model_providers.openai_chatgpt_plan.requires_openai_auth=false' in overrides


def test_list_models_reads_the_accounts_listed_models(monkeypatch):
    _signed_in(monkeypatch, expires_in=3600)
    monkeypatch.setattr(chatgpt_plan, "_get_json", lambda url, token: {"models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
        {"slug": "hidden-one", "display_name": "Hidden", "visibility": "hide"},
        {"slug": "gpt-6.1-mini", "display_name": "GPT-6.1 mini", "visibility": "list"},
    ]})

    rows = chatgpt_plan.list_models()

    assert [r["id"] for r in rows] == ["gpt-6.1-sol", "gpt-6.1-mini"]
    assert rows[0]["name"] == "GPT-6.1 Sol" and rows[0]["default"]


def test_list_models_is_empty_when_signed_out():
    assert chatgpt_plan.list_models() == []


@pytest.mark.parametrize("reply", [[], {"models": "nope"}, {"models": None}, "text"])
def test_list_models_ignores_a_malformed_reply(monkeypatch, reply):
    _signed_in(monkeypatch, expires_in=3600)
    monkeypatch.setattr(chatgpt_plan, "_get_json", lambda url, token: reply)
    assert chatgpt_plan.list_models() == []
