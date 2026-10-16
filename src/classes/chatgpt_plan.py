"""Sign in with ChatGPT: run the chat agent on the user's own ChatGPT plan.

OpenAI's "Sign in with ChatGPT" plan usage for open-source, locally hosted apps
(https://developers.openai.com/siwc/token-sharing-open-source): an OAuth sign-in
whose redirect lands on 127.0.0.1, then the access token is sent to the public
Responses API by a private copy of the Codex CLI. The plan pays for the model;
no Zenvi credits are charged for it, and the token never leaves this computer.

Everything lives in ``~/.openshot_qt/chatgpt/``: the pinned Codex package
(downloaded from OpenAI's GitHub release on first use and checked against its
sha256), its own ``CODEX_HOME``, and the sign-in. No PATH change, and the
user's own Codex (``~/.codex``: config, login, sessions) is never read or written.
"""

import base64
import hashlib
import json
import os
import platform
import secrets
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from classes import info
from classes.logger import log

AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
API_URL = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PORT = 1455          # the port is the only part of the redirect allowed to change
APP_NAME = "Zenvi"

# Pinned Codex release: bump the version and every hash together
# (gh release view rust-v<version> --repo openai/codex --json assets).
CODEX_VERSION = "0.163.0"
CODEX_PACKAGES = {  # (system, machine) -> (release asset, sha256)
    ("windows", "amd64"): ("codex-package-x86_64-pc-windows-msvc.tar.gz",
                           "901028b285614a9974f431f9d9f6534b3e5ed8d62eba4ba64f4762c00f040522"),
    ("windows", "arm64"): ("codex-package-aarch64-pc-windows-msvc.tar.gz",
                           "6bbada389cd1ce26783e837fd9ba4711fb4542f432044f7955139e27e27acfde"),
    ("darwin", "arm64"): ("codex-package-aarch64-apple-darwin.tar.gz",
                          "b0344fc2e41f15e0071147e5869008047b8ff4bf2c4a8897975d3bae3f103b34"),
    ("darwin", "x86_64"): ("codex-package-x86_64-apple-darwin.tar.gz",
                           "0c182d6194da7003943678dc87866b54b3dee719edaf8d092f304affbc6b57d1"),
    ("linux", "x86_64"): ("codex-package-x86_64-unknown-linux-musl.tar.gz",
                          "582646c6bf0561eb0de540f7fc85b9121b0091a826bc6e96830b7f1722122182"),
    ("linux", "aarch64"): ("codex-package-aarch64-unknown-linux-musl.tar.gz",
                           "9d8ecf2d19d3e184877983902d6c85de48b3888dcb72c382a8a0c975845927fb"),
}
_PROVIDER = "openai_chatgpt_plan"

_lock = threading.Lock()      # one refresh / install at a time (refresh tokens rotate)


class OAuthError(Exception):
    """The token endpoint said no; ``code`` is its ``error`` value."""

    def __init__(self, code, description=""):
        super().__init__("%s: %s" % (code, description) if description else code)
        self.code = code


class SignInRequired(Exception):
    """No usable ChatGPT sign-in: the user has to sign in (again)."""


# -- files ------------------------------------------------------------------

def home():
    return os.path.join(info.USER_PATH, "chatgpt")


def codex_home():
    return os.path.join(home(), "codex-home")


def codex_path():
    exe = "codex.exe" if os.name == "nt" else "codex"
    return os.path.join(home(), "codex-%s" % CODEX_VERSION, "bin", exe)


def _account_file():
    return os.path.join(home(), "account.json")


def _load():
    try:
        with open(_account_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(account):
    os.makedirs(home(), exist_ok=True)
    path = _account_file()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(account, fh)
    os.replace(tmp, path)


def _clear_tokens(account):
    """Forget the tokens; keep the issued client id and host id for the next sign-in."""
    for key in ("access_token", "refresh_token", "expires_at", "scope"):
        account.pop(key, None)
    _save(account)


# -- network (faked in tests) ------------------------------------------------

def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        err = body.get("error")
        if isinstance(err, dict):
            raise OAuthError(err.get("code") or "http_%d" % e.code, err.get("message") or "")
        if e.code >= 500 or not err:
            raise OSError("token endpoint answered HTTP %d" % e.code)    # temporary: keep the sign-in
        raise OAuthError(err, body.get("error_description") or "")


def _get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)


# -- sign-in ----------------------------------------------------------------

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(jwt):
    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


def _check_id_token(id_token, client_id, nonce):
    # ponytail: claims only, no JWKS signature check -- the token comes straight
    # from OpenAI's token endpoint over TLS (OIDC Core 3.1.3.7). Verify the
    # signature if it is ever accepted from anywhere else.
    claims = _claims(id_token)
    aud = claims.get("aud")
    if (claims.get("iss") != ISSUER or client_id not in (aud if isinstance(aud, list) else [aud])
            or claims.get("nonce") != nonce or float(claims.get("exp") or 0) < time.time() - 60):
        raise ValueError("ChatGPT returned an ID token that is not for this sign-in.")
    return claims


def _store_tokens(account, tokens, scope=None):
    account["access_token"] = tokens["access_token"]
    account["refresh_token"] = tokens.get("refresh_token") or account.get("refresh_token")
    account["expires_at"] = time.time() + float(tokens.get("expires_in") or 3600)
    if scope or tokens.get("scope"):
        account["scope"] = scope or tokens.get("scope")
    if tokens.get("id_token"):
        account["id_token"] = tokens["id_token"]


def _wait_for_callback(server, deadline, state):
    """Serve the loopback redirect until it brings back a code or an error."""
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            if split.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = dict(urllib.parse.parse_qsl(split.query))
            if not secrets.compare_digest(query.get("state", ""), state):
                self.send_response(400)       # not our attempt: keep waiting for the real one
                self.end_headers()
                return
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p>You can close this tab and go back to Zenvi.</p>")

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    while not result and time.time() < deadline:
        server.timeout = max(0.1, min(1.0, deadline - time.time()))
        server.handle_request()
    return result


def sign_in(open_browser=webbrowser.open, timeout=300):
    """Sign in with ChatGPT in the browser; returns ``(ok, message)``. Blocking."""
    account = _load()
    account.setdefault("host_id", uuid.uuid4().urn)      # "urn:uuid:..."; a bare UUID is rejected
    saved_client = account.get("client_id") or ""
    verifier = _b64url(secrets.token_bytes(48))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), BaseHTTPRequestHandler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)   # 1455 busy (a Codex login?)
    redirect_uri = "http://127.0.0.1:%d/auth/callback" % server.server_address[1]
    params = {
        "response_type": "code",
        "client_id": saved_client or "dynamic_agent_client",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": API_URL,
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "ext_agent_host_id": account["host_id"],
    }
    if saved_client:
        if account.get("id_token"):
            params["id_token_hint"] = account["id_token"]
        if account.get("email"):
            params["login_hint"] = account["email"]
    else:
        params["agent_name_hint"] = APP_NAME
    try:
        if not open_browser(AUTH_URL + "?" + urllib.parse.urlencode(params)):
            return False, "Could not open the browser for ChatGPT sign-in."
        reply = _wait_for_callback(server, time.time() + timeout, state)
    finally:
        server.server_close()

    if not reply:
        return False, "Sign-in timed out. Finish it in the browser, then try again."
    if not secrets.compare_digest(reply.get("state", ""), state):
        return False, "Sign-in was not completed (the browser reply did not match). Try again."
    if reply.get("error"):
        return False, "ChatGPT sign-in was declined (%s)." % reply["error"]
    client_id = reply.get("client_id") or saved_client
    if not client_id or (saved_client and client_id != saved_client) or not reply.get("code"):
        return False, "ChatGPT sign-in returned an unexpected reply. Try again."
    account["client_id"] = client_id
    _save(account)       # kept even if the exchange fails: the next attempt reauthorizes this client
    try:
        tokens = _post_form(TOKEN_URL, {
            "grant_type": "authorization_code", "client_id": client_id, "code": reply["code"],
            "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": API_URL,
        })
        claims = _check_id_token(tokens.get("id_token"), client_id, nonce)
    except (OAuthError, OSError, ValueError) as e:
        log.warning("ChatGPT sign-in failed: %s", e)
        return False, "ChatGPT sign-in failed: %s" % e
    scope = tokens.get("scope") or ""      # only the token response decides plan usage
    account["email"] = claims.get("email") or account.get("email") or ""
    if PLAN_SCOPE not in scope.split():
        account["id_token"] = tokens["id_token"]
        _clear_tokens(account)
        return False, ("Signed in, but ChatGPT plan usage was not allowed for Zenvi. "
                       "Sign in again and allow it to use your plan.")
    _store_tokens(account, tokens, scope)
    _save(account)
    who = account["email"]
    return True, "Signed in with ChatGPT%s." % (" as " + who if who else "")


def signed_in():
    account = _load()
    return bool(account.get("refresh_token")) and PLAN_SCOPE in (account.get("scope") or "").split()


def access_token():
    """A live access token, refreshed when it is about to expire.

    Raises SignInRequired when the sign-in is gone; a network error propagates
    and leaves the sign-in in place.
    """
    with _lock:
        account = _load()
        if not account.get("refresh_token"):
            raise SignInRequired("Sign in with ChatGPT first.")
        if account.get("access_token") and float(account.get("expires_at") or 0) - 300 > time.time():
            return account["access_token"]
        try:
            tokens = _post_form(TOKEN_URL, {
                "grant_type": "refresh_token", "client_id": account["client_id"],
                "refresh_token": account["refresh_token"], "resource": API_URL,
            })
        except OAuthError as e:
            log.warning("ChatGPT token refresh refused (%s); signing out", e.code)
            _clear_tokens(account)
            raise SignInRequired("Your ChatGPT sign-in expired. Sign in again.") from e
        _store_tokens(account, tokens)
        _save(account)
        return account["access_token"]


def list_models():
    """Picker entries for the models the signed-in account lists; [] when unknown."""
    if not signed_in():
        return []
    try:
        data = _get_json(API_URL + "/models", access_token())
    except Exception as e:
        log.debug("ChatGPT model list failed: %s", e)
        return []
    rows = []
    models = data.get("models") if isinstance(data, dict) else None
    for model in models if isinstance(models, list) else []:
        if isinstance(model, dict) and model.get("visibility") == "list" and model.get("slug"):
            rows.append({"id": model["slug"], "name": model.get("display_name") or model["slug"],
                         "provider": "openai", "rank": len(rows), "featured": True})
    if rows:
        rows[0]["default"] = True
    return rows


# -- the private Codex --------------------------------------------------------

def _target():
    return platform.system().lower(), platform.machine().lower()


def installed():
    return os.path.isfile(codex_path())


def install():
    """Download and unpack the pinned Codex package once; returns the binary path."""
    with _lock:
        if installed():
            return codex_path()
        package = CODEX_PACKAGES.get(_target())
        if package is None:
            raise RuntimeError("ChatGPT sign-in is not available on this platform (%s %s)." % _target())
        asset, digest = package
        dest = os.path.dirname(os.path.dirname(codex_path()))
        partial = dest + ".partial"
        archive = os.path.join(home(), asset + ".download")
        os.makedirs(home(), exist_ok=True)
        shutil.rmtree(partial, ignore_errors=True)
        try:
            _download("https://github.com/openai/codex/releases/download/rust-v%s/%s"
                      % (CODEX_VERSION, asset), archive)
            sha = hashlib.sha256()
            with open(archive, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    sha.update(block)
            if sha.hexdigest() != digest:
                raise RuntimeError("The Codex download failed its checksum; not installing it.")
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                for m in members:
                    target = os.path.realpath(os.path.join(partial, m.name))
                    if not (m.isfile() or m.isdir()) or not target.startswith(
                            os.path.realpath(partial) + os.sep):
                        raise RuntimeError("Unexpected file in the Codex package: %s" % m.name)
                tar.extractall(partial, members)
            exe = os.path.join(partial, os.path.relpath(codex_path(), dest))
            if not os.path.isfile(exe):
                raise RuntimeError("The Codex package has no %s." % os.path.basename(exe))
            os.chmod(exe, 0o755)
            shutil.rmtree(dest, ignore_errors=True)     # a broken earlier install
            os.replace(partial, dest)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
            if os.path.exists(archive):
                os.remove(archive)
        return codex_path()


def child_env(env):
    """*env* for the private Codex: its own home, the plan token, and no API keys."""
    env = dict(env)
    env.pop("OPENAI_API_KEY", None)     # never fall back to pay-per-token API billing
    env.pop("CODEX_API_KEY", None)
    os.makedirs(codex_home(), exist_ok=True)
    env["CODEX_HOME"] = codex_home()
    env["ACCESS_TOKEN"] = access_token()
    return env


def provider_args():
    """``-c`` overrides that send Codex's model calls to the Responses API on the plan token."""
    key = "model_providers.%s." % _PROVIDER
    pairs = [
        ("model_provider", '"%s"' % _PROVIDER),
        (key + "name", '"ChatGPT plan"'),
        (key + "base_url", '"%s"' % API_URL),
        (key + "env_key", '"ACCESS_TOKEN"'),
        (key + "wire_api", '"responses"'),
        (key + "requires_openai_auth", "false"),
        (key + "supports_websockets", "false"),
        # The agent's shell commands must not see the plan token.
        ("shell_environment_policy.exclude", '["ACCESS_TOKEN"]'),
    ]
    args = []
    for name, value in pairs:
        args += ["-c", "%s=%s" % (name, value)]
    return args
