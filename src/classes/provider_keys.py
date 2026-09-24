"""
Bring-your-own-key vault for third-party integrations (Higgsfield first).

Keys live per OS user: the OS keychain when ``keyring`` is installed, otherwise
a file under USER_PATH protected with DPAPI on Windows (0600 elsewhere).
Never in openshot.settings, project files, or zenvi_auth.json, and never logged.
"""

import base64
import json
import os

from classes.logger import log

KEYRING_SERVICE = "zenvi-provider-keys"

# Provider manifest: adding an integration = one entry here + a backend client.
PROVIDERS = {
    "higgsfield": {
        "name": "Higgsfield",
        "capabilities": ("video_gen",),
        "key_hint": "KEY_ID:KEY_SECRET",
        "docs_url": "https://console.higgsfield.ai",
    },
}


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _store_path():
    from classes import info
    return os.path.join(info.USER_PATH, "provider_keys.json")


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _read_file() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _check(provider: str) -> None:
    if provider not in PROVIDERS:
        raise KeyError(f"Unknown integration: {provider}")


def set_key(provider: str, key: str) -> None:
    _check(provider)
    key = (key or "").strip()
    kr = _keyring()
    if kr is not None:
        kr.set_password(KEYRING_SERVICE, provider, key)
        return
    data = _read_file()
    data[provider] = base64.b64encode(_dpapi(key.encode("utf-8"), True)).decode("ascii")
    _write_file(data)


def get_key(provider: str) -> str:
    _check(provider)
    kr = _keyring()
    if kr is not None:
        try:
            return kr.get_password(KEYRING_SERVICE, provider) or ""
        except Exception as exc:
            log.warning("Keychain read failed for %s: %s", provider, type(exc).__name__)
            return ""
    blob = _read_file().get(provider)
    if not blob:
        return ""
    try:
        return _dpapi(base64.b64decode(blob), False).decode("utf-8")
    except Exception as exc:
        log.warning("Stored %s key could not be decrypted: %s", provider, type(exc).__name__)
        return ""


def clear_key(provider: str) -> None:
    _check(provider)
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, provider)
        except Exception:
            pass
        return
    data = _read_file()
    if data.pop(provider, None) is not None:
        _write_file(data)


def mask(key: str) -> str:
    key = (key or "").strip()
    return f"••••{key[-4:]}" if key else ""


def test_and_save(provider: str, key: str, validate) -> tuple:
    """Validate ``key`` with ``validate(provider, key)``; store it only if accepted.

    Returns (ok, user-facing message). The raw key never appears in the message.
    """
    _check(provider)
    key = (key or "").strip()
    name = PROVIDERS[provider]["name"]
    if not key:
        return False, f"Paste your {name} key ({PROVIDERS[provider]['key_hint']})."
    result = validate(provider, key) or {}
    if not result.get("ok"):
        return False, f"{name} rejected the key: {result.get('error') or 'unknown error'}"
    set_key(provider, key)
    return True, f"Connected ({mask(key)})"
