"""The background updater: what it downloads, what it trusts, and what it tells the UI."""

import hashlib
import json
import os
import sys
import types

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.modules.setdefault("openshot", types.ModuleType("openshot"))

from classes import auto_updater as au  # noqa: E402
from classes import update_installer as ui  # noqa: E402


@pytest.fixture
def staging(tmp_path, monkeypatch):
    stage = tmp_path / "updates"
    stage.mkdir()
    for module in (ui, au):
        monkeypatch.setattr(module, "UPDATE_STAGING_DIR", str(stage), raising=False)
    monkeypatch.setattr(ui, "UPDATE_MANIFEST", str(stage / "update_manifest.json"))
    monkeypatch.setattr(ui, "UPDATE_LOG", str(stage / "install.log"))
    monkeypatch.setattr(ui, "UPDATE_STATUS", str(stage / "update_status.json"))
    return stage


class Signals:
    """Records what the updater would tell the main window."""

    def __init__(self, monkeypatch):
        self.events = []
        monkeypatch.setattr(au.AutoUpdater, "_emit_failed_signal",
                            lambda _self, v, r: self.events.append(("failed", v, r)))
        monkeypatch.setattr(au.AutoUpdater, "_emit_update_ready_signal",
                            lambda _self, v: self.events.append(("ready", v)))
        monkeypatch.setattr(au.AutoUpdater, "_emit_version_signal",
                            lambda _self, v: self.events.append(("version", v)))
        monkeypatch.setattr(au.AutoUpdater, "_emit_progress_signal", lambda *a, **k: None)


class FakeResponse:
    def __init__(self, body=b"", text="", status=200):
        self._body = body
        self.text = text
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise au.requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


# ── Which package fits this install ──────────────────────────────────────────

@pytest.mark.parametrize("exe, appimage, expected", [
    ("/opt/zenvi/zenvi", None, ".deb"),          # the .deb's layout
    ("/usr/lib/zenvi/zenvi", None, ".deb"),
    ("/tmp/.mount_ZenviX/usr/bin/zenvi", "/home/u/Zenvi.AppImage", ".AppImage"),
    ("/home/u/zenvi/zenvi", None, ".AppImage"),
])
def test_linux_updates_with_the_package_type_it_was_installed_from(monkeypatch, exe, appimage, expected):
    monkeypatch.setattr(au.platform, "system", lambda: "Linux")
    monkeypatch.setattr(au.sys, "executable", exe)
    monkeypatch.setattr(au.os.path, "realpath", lambda p: p)
    if appimage:
        monkeypatch.setenv("APPIMAGE", appimage)
    else:
        monkeypatch.delenv("APPIMAGE", raising=False)
    assert au._platform_asset_suffix() == expected


# ── The published checksums ──────────────────────────────────────────────────

def test_the_release_checksum_list_is_read_like_sha256sum_writes_it():
    text = (
        f"{'a' * 64}  ./Zenvi-v1.2.1-x86_64.exe\n"
        f"{'B' * 64}  ./Zenvi-v1.2.1-arm64.dmg\n"
        f"{'c' * 64} *Zenvi-v1.2.1-x86_64.AppImage\n"
        "garbage line\n"
    )
    assert au.parse_checksums(text) == {
        "Zenvi-v1.2.1-x86_64.exe": "a" * 64,
        "Zenvi-v1.2.1-arm64.dmg": "b" * 64,
        "Zenvi-v1.2.1-x86_64.AppImage": "c" * 64,
    }


def _release(body, sums_text):
    return {
        "tag_name": "v9.9.9",
        "assets": [
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"},
            {"name": "Zenvi-v9.9.9-x86_64.AppImage", "browser_download_url": "https://x/app",
             "size": len(body)},
        ],
    }


def _serve(monkeypatch, body, sums_text):
    def fake_get(url, stream=False, timeout=None, headers=None):
        if url.endswith("/sums"):
            return FakeResponse(text=sums_text)
        return FakeResponse(body=body)
    monkeypatch.setattr(au.requests, "get", fake_get)


def _linux_appimage(monkeypatch):
    monkeypatch.setattr(au.platform, "system", lambda: "Linux")
    monkeypatch.setattr(au.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("APPIMAGE", "/home/u/Zenvi.AppImage")


def test_a_download_that_matches_the_published_checksum_is_staged(staging, monkeypatch):
    body = b"new zenvi"
    sums = f"{hashlib.sha256(body).hexdigest()}  ./Zenvi-v9.9.9-x86_64.AppImage\n"
    _serve(monkeypatch, body, sums)
    _linux_appimage(monkeypatch)
    signals = Signals(monkeypatch)

    au.AutoUpdater()._check_and_download(_release(body, sums), "9.9.9")

    assert ("ready", "9.9.9") in signals.events
    manifest = json.loads((staging / "update_manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(body).hexdigest()
    assert (staging / "Zenvi-v9.9.9-x86_64.AppImage").read_bytes() == body


def test_a_download_that_does_not_match_the_published_checksum_is_refused(staging, monkeypatch):
    body = b"tampered or truncated"
    sums = f"{'0' * 64}  ./Zenvi-v9.9.9-x86_64.AppImage\n"
    _serve(monkeypatch, body, sums)
    _linux_appimage(monkeypatch)
    signals = Signals(monkeypatch)

    au.AutoUpdater()._check_and_download(_release(body, sums), "9.9.9")

    assert any(e[0] == "failed" and "checksum" in e[2] for e in signals.events)
    assert not (staging / "update_manifest.json").exists()
    assert not any(p.name.startswith("Zenvi") for p in staging.iterdir())


# ── Telling the UI about an install that keeps failing ──────────────────────

def _stage(staging, version="9.9.9"):
    package = staging / f"Zenvi-v{version}-x86_64.AppImage"
    package.write_bytes(b"x")
    ui.write_manifest({"version": version, "filename": package.name, "filepath": str(package),
                       "sha256": hashlib.sha256(b"x").hexdigest(), "size": 1})


def _run_without_network(monkeypatch):
    monkeypatch.setattr(au, "INITIAL_DELAY", 0)
    monkeypatch.setattr(au.AutoUpdater, "_fetch_latest_release", lambda self: (None, ""))


def test_a_staged_update_whose_install_keeps_failing_offers_the_manual_download(staging, monkeypatch):
    _stage(staging)
    ui._write_status({"version": "9.9.9", "attempts": ui.MAX_INSTALL_ATTEMPTS, "ok": False,
                      "reason": "The installer stopped with exit code 2"})
    _run_without_network(monkeypatch)
    signals = Signals(monkeypatch)

    au.AutoUpdater()._run()

    assert signals.events == [("failed", "9.9.9", "The installer stopped with exit code 2")]


def test_a_staged_update_that_failed_once_is_still_offered_as_ready(staging, monkeypatch):
    _stage(staging)
    ui._write_status({"version": "9.9.9", "attempts": 1, "ok": False, "reason": "postponed"})
    _run_without_network(monkeypatch)
    signals = Signals(monkeypatch)

    au.AutoUpdater()._run()

    assert signals.events == [("ready", "9.9.9")]


def test_startup_tidies_stale_downloads_and_forgets_a_finished_install(staging, monkeypatch):
    (staging / "Zenvi-v1.0.120-arm64.dmg").write_bytes(b"old")
    ui._write_status({"version": au.info.VERSION, "attempts": 1, "ok": True, "reason": "installed"})
    _run_without_network(monkeypatch)
    Signals(monkeypatch)

    au.AutoUpdater()._run()

    assert not (staging / "Zenvi-v1.0.120-arm64.dmg").exists()
    assert not (staging / "update_status.json").exists()
