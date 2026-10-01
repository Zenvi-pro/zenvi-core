"""
Bring-your-own-key vault for third-party integrations (Higgsfield first).

Keys live per OS user in the OS keychain through ``keyring``: macOS Keychain,
Windows Credential Manager, Secret Service on Linux. Only Windows has a file
fallback (for when Credential Manager is unusable), and it is DPAPI-encrypted
to the user. Anywhere else, no usable keychain means the key is not stored at
all. Never in openshot.settings, project files, or zenvi_auth.json, and never
logged.
"""

import base64
import json
import os
import tempfile

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


class KeyUnreadable(Exception):
    """A key is stored but could not be read (keychain error, DPAPI failure)."""


class KeyStoreUnavailable(Exception):
    """Nowhere safe to keep a key: no usable keychain, and no DPAPI (not Windows)."""


def _keyring():
    """The keyring module when it has a usable backend, else None."""
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def _file_store_available() -> bool:
    """The file store exists only where DPAPI can encrypt it to the user."""
    return os.name == "nt"


def storage_available() -> bool:
    return _keyring() is not None or _file_store_available()


def _store_path():
    from classes import info
    return os.path.join(info.USER_PATH, "provider_keys.json")


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current Windows user."""
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
    """Stage the whole store in a private temp file, then swap it in.

    mkstemp creates the file 0600, so it is never readable by other users (not
    even between a write and a chmod), and a crash never leaves a half-written store.
    """
    path = _store_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".provider_keys.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _drop_file_entry(provider: str) -> None:
    """Delete any file copy (including one an earlier build wrote unencrypted)."""
    data = _read_file()
    if data.pop(provider, None) is not None:
        _write_file(data)


def _file_key(provider: str) -> str:
    if not _file_store_available():
        return ""
    blob = _read_file().get(provider)
    if not blob:
        return ""
    return _dpapi(base64.b64decode(blob), False).decode("utf-8")


def _check(provider: str) -> None:
    if provider not in PROVIDERS:
        raise KeyError(f"Unknown integration: {provider}")


def set_key(provider: str, key: str) -> None:
    """Store ``key``; raises KeyStoreUnavailable when there is nowhere safe for it."""
    _check(provider)
    key = (key or "").strip()
    kr = _keyring()
    if kr is not None:
        kr.set_password(KEYRING_SERVICE, provider, key)
        _drop_file_entry(provider)  # leave no copy behind in the file store
        return
    if not _file_store_available():
        raise KeyStoreUnavailable(provider)
    data = _read_file()
    data[provider] = base64.b64encode(_dpapi(key.encode("utf-8"), True)).decode("ascii")
    _write_file(data)


def get_key(provider: str, strict: bool = False) -> str:
    """Stored key or "". With ``strict``, a stored-but-unreadable key raises KeyUnreadable.

    The keychain wins. On Windows the file store is read too, so a key saved
    while Credential Manager was unusable is still found (and still removable).
    """
    _check(provider)
    try:
        kr = _keyring()
        if kr is not None:
            key = kr.get_password(KEYRING_SERVICE, provider) or ""
            if key:
                return key
        return _file_key(provider)
    except Exception as exc:
        log.warning("Stored %s key could not be read: %s", provider, type(exc).__name__)
        if strict:
            raise KeyUnreadable(provider) from None
        return ""


def clear_key(provider: str) -> None:
    """Remove the key from the keychain and the file store, wherever it is.

    A keychain that refuses the delete raises: the key is still there, so
    Remove must not report success.
    """
    _check(provider)
    kr = _keyring()
    if kr is not None and kr.get_password(KEYRING_SERVICE, provider) is not None:
        kr.delete_password(KEYRING_SERVICE, provider)
    _drop_file_entry(provider)


def redact(text: str, key: str) -> str:
    """Remove ``key`` and its id/secret halves from text shown in UI, logs, or tool output."""
    if not text or not key:
        return text
    kid, _, secret = key.partition(":")
    for part in sorted({key, kid, secret}, key=len, reverse=True):
        if part and len(part) >= 4:
            text = text.replace(part, "••••")
    return text


def mask(key: str) -> str:
    key = (key or "").strip()
    return f"••••{key[-4:]}" if key else ""


def test_and_save(provider: str, key: str, validate) -> tuple:
    """Validate ``key`` with ``validate(provider, key)``; store it only if accepted.

    Returns (ok, user-facing message). The raw key never appears in the message.
    Blocking (keychain + a backend round trip): call it off the GUI thread.
    """
    _check(provider)
    key = (key or "").strip()
    name = PROVIDERS[provider]["name"]
    if not key:
        return False, f"Paste your {name} key ({PROVIDERS[provider]['key_hint']})."
    if not storage_available():
        return False, (
            f"No system keychain is available, so Zenvi cannot store your {name} key safely. "
            "On Linux, install or unlock a Secret Service keyring (GNOME Keyring or KWallet)."
        )
    result = validate(provider, key) or {}
    if not result.get("ok"):
        err = redact(str(result.get("error") or "unknown error"), key)
        return False, f"{name} rejected the key: {err}"
    try:
        set_key(provider, key)
    except Exception as exc:
        log.warning("Could not store the %s key: %s", provider, type(exc).__name__)
        return False, f"{name} accepted the key, but it could not be saved ({type(exc).__name__})."
    return True, f"Connected ({mask(key)})"
