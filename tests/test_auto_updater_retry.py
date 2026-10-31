"""Regression: update pill must retry in-app, not dump users on the website.

A failed download used to emit FoundVersionSignal after UpdateFailedSignal,
which rewrote the pill back to "Update Available" whose only action opened
zenvi.pro/download (often the signed-in dashboard).
"""

from __future__ import annotations

import threading
import classes.auto_updater as auto_updater_mod
from classes.auto_updater import AutoUpdater


def test_retry_starts_when_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    updater = AutoUpdater()
    started = []

    def fake_run(initial_delay=15):
        started.append(initial_delay)

    monkeypatch.setattr(updater, "_run", fake_run)
    assert updater.retry() is True
    # Give the daemon thread a moment to enter fake_run
    for _ in range(50):
        if started:
            break
        threading.Event().wait(0.01)
    assert started == [0]


def test_retry_ignored_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    updater = AutoUpdater()
    gate = threading.Event()

    def hang(initial_delay=15):
        gate.wait(2.0)

    updater._thread = threading.Thread(target=hang, daemon=True)
    updater._thread.start()
    try:
        assert updater.retry() is False
    finally:
        gate.set()
        updater._thread.join(timeout=2.0)


def test_failed_download_does_not_emit_version_signal(tmp_path, monkeypatch):
    """After a failed _download, do not re-emit FoundVersionSignal."""
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    updater = AutoUpdater()
    version_emits = []
    failed_emits = []
    monkeypatch.setattr(updater, "_emit_version_signal", lambda v: version_emits.append(v))
    monkeypatch.setattr(updater, "_emit_failed_signal", lambda v, r: failed_emits.append((v, r)))
    monkeypatch.setattr(updater, "_emit_update_ready_signal", lambda v: None)

    def fake_download(url, filename, version, expected_size, expected_sha256=""):
        failed_emits.append((version, "boom"))
        return False

    monkeypatch.setattr(updater, "_download", fake_download)
    monkeypatch.setattr(updater, "_published_checksum", lambda *a, **k: "")
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_suffix", lambda: ".exe")
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_arch_hint", lambda: "x86_64")
    monkeypatch.setattr(auto_updater_mod, "is_version_newer", lambda remote, local: True)

    release = {
        "assets": [{
            "name": "Zenvi-v9.9.9-x86_64.exe",
            "browser_download_url": "https://example.test/Zenvi.exe",
            "size": 10,
        }],
    }
    updater._check_and_download(release, "9.9.9")
    assert failed_emits  # from the fake _download
    assert version_emits == []


def test_missing_asset_emits_failed_not_available(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    updater = AutoUpdater()
    version_emits = []
    failed_emits = []
    monkeypatch.setattr(updater, "_emit_version_signal", lambda v: version_emits.append(v))
    monkeypatch.setattr(updater, "_emit_failed_signal", lambda v, r: failed_emits.append((v, r)))
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_suffix", lambda: ".exe")
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_arch_hint", lambda: "x86_64")
    monkeypatch.setattr(auto_updater_mod, "is_version_newer", lambda remote, local: True)

    updater._check_and_download({"assets": []}, "9.9.9")
    assert version_emits == []
    assert len(failed_emits) == 1
    assert failed_emits[0][0] == "9.9.9"
