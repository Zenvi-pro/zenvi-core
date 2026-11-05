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


def test_retry_ignored_while_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    updater = AutoUpdater()
    gate = threading.Event()

    def hang(initial_delay=15):
        gate.wait(2.0)

    updater._thread = threading.Thread(target=hang, daemon=True)
    updater._thread.start()
    updater._delay_over = True  # past the launch delay: downloading
    try:
        assert updater.retry() is False
        assert not updater._user_waiting.is_set()
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


# ---------------------------------------------------------------------------
# A retry the user waits on always ends in ready or failed (review of #365)
# ---------------------------------------------------------------------------

class _Signal:
    def __init__(self, emits, name):
        self._emits, self._name = emits, name

    def emit(self, *args):
        self._emits.append((self._name,) + args)


class _Window:
    """What the updater reaches through ``_main_window()``: four queued signals."""

    def __init__(self):
        self.emits = []
        for attr, name in (("FoundVersionSignal", "found"), ("UpdateReadySignal", "ready"),
                           ("UpdateProgressSignal", "progress"), ("UpdateFailedSignal", "failed")):
            setattr(self, attr, _Signal(self.emits, name))

    def named(self, name):
        return [e for e in self.emits if e[0] == name]


def _updater(tmp_path, monkeypatch, releases):
    """An AutoUpdater whose GitHub answers come from *releases* (None = unreachable)."""
    monkeypatch.setattr(auto_updater_mod, "UPDATE_STAGING_DIR", str(tmp_path))
    monkeypatch.setattr(auto_updater_mod, "update_channel", lambda: "stable")
    monkeypatch.setattr(auto_updater_mod, "has_pending_update", lambda: False)
    monkeypatch.setattr(auto_updater_mod, "is_version_newer",
                        lambda remote, local: remote == "9.9.9")
    window = _Window()
    monkeypatch.setattr(AutoUpdater, "_main_window", staticmethod(lambda: window))
    updater = AutoUpdater()
    monkeypatch.setattr(updater, "_tidy_staging", lambda: None)
    monkeypatch.setattr(updater, "_get_json", lambda url, headers: releases)
    return updater, window


def _finish(updater):
    updater._thread.join(timeout=5.0)
    assert not updater._thread.is_alive()


def test_retry_that_cannot_reach_github_reports_a_failure(tmp_path, monkeypatch):
    # CodeRabbit (major): the pill was left on "Downloading update…" with its
    # buttons disabled when GitHub could not be reached.
    updater, window = _updater(tmp_path, monkeypatch, None)
    assert updater.retry() is True
    _finish(updater)
    failed = window.named("failed")
    assert len(failed) == 1
    assert "GitHub" in failed[0][2]


def test_retry_with_no_installer_for_this_platform_reports_a_failure(tmp_path, monkeypatch):
    updater, window = _updater(tmp_path, monkeypatch, {"tag_name": "v9.9.9", "assets": []})
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_suffix", lambda: None)
    assert updater.retry() is True
    _finish(updater)
    assert [e[1:] for e in window.named("failed")] == [
        ("9.9.9", "Updates can't be downloaded on this platform")]


def test_retry_that_finds_no_newer_release_reports_why(tmp_path, monkeypatch):
    updater, window = _updater(tmp_path, monkeypatch, {"tag_name": "v1.0.0", "assets": []})
    assert updater.retry() is True
    _finish(updater)
    assert [e[1:] for e in window.named("failed")] == [
        ("1.0.0", "GitHub no longer lists a newer release")]


def test_retry_that_downloads_reports_ready_only(tmp_path, monkeypatch):
    release = {"tag_name": "v9.9.9", "assets": [{
        "name": "Zenvi-v9.9.9-x86_64.exe", "browser_download_url": "https://example.test/z.exe", "size": 3}]}
    updater, window = _updater(tmp_path, monkeypatch, release)
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_suffix", lambda: ".exe")
    monkeypatch.setattr(auto_updater_mod, "_platform_asset_arch_hint", lambda: "x86_64")
    monkeypatch.setattr(updater, "_published_checksum", lambda *a, **k: "")
    monkeypatch.setattr(updater, "_download", lambda *a, **k: True)
    assert updater.retry() is True
    _finish(updater)
    assert window.named("ready") == [("ready", "9.9.9")]
    assert window.named("failed") == []


def test_startup_check_that_cannot_reach_github_stays_quiet(tmp_path, monkeypatch):
    # Nobody clicked anything: an offline launch must not flash "Update Failed".
    updater, window = _updater(tmp_path, monkeypatch, None)
    updater._run(initial_delay=0)
    assert window.emits == []


def test_retry_during_the_launch_delay_starts_the_download_now(tmp_path, monkeypatch):
    # CodeRabbit: the startup check emits "available" before its 15 s delay. A
    # Download click during that delay used to be ignored, leaving the pill on
    # "Downloading" until the delay ran out. It now ends the delay, on the same thread.
    release = {"tag_name": "v9.9.9", "assets": []}
    updater, window = _updater(tmp_path, monkeypatch, release)
    downloads = []
    started = threading.Event()
    monkeypatch.setattr(updater, "_check_and_download",
                        lambda rel, ver: (downloads.append(threading.current_thread().name),
                                          updater._emit_update_ready_signal(ver)))
    monkeypatch.setattr(auto_updater_mod, "INITIAL_DELAY", 15)
    real_fetch = updater._fetch_latest_release

    def fetch():
        result = real_fetch()
        started.set()
        return result

    monkeypatch.setattr(updater, "_fetch_latest_release", fetch)
    updater.start()
    assert started.wait(5.0)
    assert window.named("found") == [("found", "9.9.9")]   # the pill says "Update Available"
    first = updater._thread
    assert updater.retry() is True
    first.join(timeout=5.0)
    assert not first.is_alive(), "the retry did not end the 15 s launch delay"
    assert updater._thread is first, "a second check thread was started"
    assert downloads == ["AutoUpdater"]
    assert window.named("ready") == [("ready", "9.9.9")]
    assert window.named("failed") == []


# ---------------------------------------------------------------------------
# A failed download says why in a few words (e2e of #365: the panel showed a
# nested IncompleteRead repr full of byte counts, cut off top and bottom)
# ---------------------------------------------------------------------------

import requests  # noqa: E402

from classes.auto_updater import download_failure_reason  # noqa: E402


def test_failure_reasons_are_short_and_human():
    dropped = requests.exceptions.ChunkedEncodingError(
        "('Connection broken: IncompleteRead(115343360 bytes read, 492591641 more expected)')")
    assert download_failure_reason(dropped, 115_343_360, 607_935_001) == \
        "The connection dropped after 115 of 607 MB"
    assert download_failure_reason(dropped, 115_343_360, 0) == "The connection dropped after 115 MB"
    assert download_failure_reason(requests.ConnectionError("refused"), 0, 607_935_001) == \
        "Couldn't connect to the download server"
    assert download_failure_reason(requests.Timeout("slow"), 5, 10) == "The download timed out"
    not_found = requests.HTTPError("404")
    not_found.response = type("R", (), {"status_code": 404})()
    assert download_failure_reason(not_found, 0, 0) == "The download server answered HTTP 404"
    assert download_failure_reason(OSError("disk full"), 0, 0) == "disk full"


def test_a_connection_dropped_mid_download_reports_how_far_it_got(tmp_path, monkeypatch):
    updater, window = _updater(tmp_path, monkeypatch, None)

    class _Response:
        headers = {}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"x" * 3_000_000
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")

    monkeypatch.setattr(auto_updater_mod.requests, "get", lambda *a, **k: _Response())
    assert updater._download("https://example.test/z.dmg", "z.dmg", "9.9.9", 10_000_000) is False
    assert window.named("failed") == [("failed", "9.9.9", "The connection dropped after 3 of 10 MB")]
    assert not (tmp_path / "z.dmg.part").exists()
