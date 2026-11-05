"""File → Zenvi Cloud: push, pull, hashing, Azure uploads and the GUI-thread rule.

Runs against the in-process fake Zenvi Cloud in tests/_cloud_fake.py (REST /v1
plus the Azure blob calls behind the SAS URLs).
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import random
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from _cloud_fake import (
    TOKEN,
    WEBSITE,
    FakeCloud,
    make_client,
    make_local_project,
    request_for,
    sha_of,
    socket_on_free_port,
    store_link,
    write_media,
    write_project,
)
from classes import cloud_sync
from classes.cloud_sync import (
    CancelToken,
    CloudAuthError,
    CloudCancelled,
    CloudClient,
    CloudOfflineError,
    CloudQuotaError,
    Progress,
    PushRequest,
    StaticTokenSource,
    block_id,
    pull_project,
    push_project,
)


@pytest.fixture
def cloud():
    fake = FakeCloud()
    yield fake
    fake.close()


@pytest.fixture
def local_project(tmp_path):
    return make_local_project(tmp_path)


# ── Configuration and small helpers ───────────────────────────────────────────

def test_cloud_url_precedence_env_then_setting_then_default():
    assert cloud_sync.resolve_cloud_url("", {}) == cloud_sync.DEFAULT_CLOUD_URL
    assert cloud_sync.resolve_cloud_url("https://custom.example/", {}) == "https://custom.example"
    assert cloud_sync.resolve_cloud_url("custom.example", {}) == "https://custom.example"
    assert cloud_sync.resolve_cloud_url("https://custom.example", {"ZENVI_CLOUD_URL": "http://127.0.0.1:9"}) \
        == "http://127.0.0.1:9"


def test_editor_url_follows_the_website():
    assert cloud_sync.editor_url("https://zenvi.pro/", "c_abcdefghij012345") == \
        "https://zenvi.pro/editor/p/c_abcdefghij012345"
    assert cloud_sync.resolve_website({"ZENVI_WEBSITE": "http://localhost:3000"}) == "http://localhost:3000"
    assert cloud_sync.resolve_website({}) == "https://zenvi.pro"


def test_cached_hash_is_reused_only_when_size_and_mtime_match():
    ref = {"sha256": "a" * 64, "size": 10, "mtime": 1700000000.25, "name": "x.mp4"}
    assert cloud_sync.cached_sha256(ref, 10, 1700000000.25) == "a" * 64
    assert cloud_sync.cached_sha256(ref, 11, 1700000000.25) is None
    assert cloud_sync.cached_sha256(ref, 10, 1700000001.25) is None
    assert cloud_sync.cached_sha256({"sha256": "a" * 64, "size": 10}, 10, 1.0) is None  # no mtime: rehash
    # The desktop fingerprint is a partial hash and is never a cloud key.
    assert cloud_sync.cached_sha256({"size": 10, "mtime": 1.0}, 10, 1.0) is None


def test_content_types_are_ones_the_cloud_accepts():
    assert cloud_sync.content_type_for("/x/clip.MOV") == "video/quicktime"
    assert cloud_sync.content_type_for("/x/song.mp3") == "audio/mpeg"
    assert cloud_sync.content_type_for("/x/title.svg") == "image/svg+xml"
    assert cloud_sync.content_type_for("/x/subs.srt") == "application/x-subrip"
    assert cloud_sync.content_type_for("/x/notes.docx") == "application/octet-stream"


def test_safe_names_work_on_every_platform():
    assert cloud_sync.safe_name('My: "Film"/cut?') == "My Film cut"
    assert cloud_sync.safe_name("  ..  ") == "Untitled project"
    assert cloud_sync.safe_name("CON") == "CON_"
    assert cloud_sync.safe_file_name("../../etc/pass*wd.mp4") == "pass wd.mp4"
    assert len(cloud_sync.safe_name("x" * 500)) == 80


def test_throttled_progress_keeps_phase_changes_and_completion():
    seen = []
    clock = iter([0.0, 0.01, 0.02, 0.03, 0.04, 0.05])
    report = cloud_sync.ThrottledProgress(seen.append, interval=1.0, clock=lambda: next(clock))
    report(Progress("upload", 0, 10, "a"))
    report(Progress("upload", 3, 10, "a"))      # throttled
    report(Progress("upload", 10, 10, "a"))     # completion always passes
    report(Progress("upload", 0, 10, "b"))      # new file passes
    assert [(p.detail, p.done) for p in seen] == [("a", 0), ("a", 10), ("b", 0)]


# ── Push ──────────────────────────────────────────────────────────────────────

def test_first_push_uploads_media_and_creates_the_cloud_project(cloud, local_project):
    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.created and result.project_id in cloud.projects
    assert result.editor_url == f"{WEBSITE}/editor/p/{result.project_id}"
    assert result.uploaded_files == 2 and result.already_in_cloud == 0
    assert set(cloud.media) == {sha_of(local_project.a), sha_of(local_project.b)}
    assert cloud.media[sha_of(local_project.a)]["contentType"] == "video/mp4"
    assert result.link["project_id"] == result.project_id
    assert result.link["revision"] == cloud.projects[result.project_id]["revision"]
    assert cloud.auth_on_storage == []  # the Supabase token never goes to storage

    stored = cloud.projects[result.project_id]["project"]
    refs = {f["id"]: f["cloud"] for f in stored["files"]}
    assert refs["F1"]["sha256"] == sha_of(local_project.a)
    assert refs["F1"]["size"] == 300_000 and refs["F1"]["name"] == "a.mp4"
    assert refs["F1"]["mtime"] == os.stat(local_project.a).st_mtime
    # Portable paths stay as saved; machine-local bits are stripped from the cloud copy.
    assert stored["files"][0]["path"] == "media/a.mp4"
    assert stored["effects"][0]["reader"]["path"] == "@transitions/common/fade.svg"
    assert "proxy_reader" not in stored["files"][0]
    assert "zenvi_cloud" not in stored
    assert set(result.cloud_refs) == {"F1", "F2"}


def test_push_uploads_only_the_media_the_cloud_lacks(cloud, local_project):
    cloud.add_media(local_project.a.read_bytes(), name="a.mp4")

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.uploaded_files == 1 and result.already_in_cloud == 1
    assert cloud.count("POST", "/v1/media/check") == 1
    assert cloud.count("POST", "/v1/media") == 1
    assert cloud.count("PUT", f"/blob/{sha_of(local_project.a)}") == 0
    assert cloud.count("PUT", f"/blob/{sha_of(local_project.b)}") == 1


def test_identical_files_are_uploaded_once(cloud, tmp_path):
    src = tmp_path / "src"
    a = write_media(src / "media", "a.mp4", 50_000, 7)
    copy = src / "media" / "copy.mp4"
    copy.write_bytes(a.read_bytes())
    files = [{"id": "F1", "path": "media/a.mp4"}, {"id": "F2", "path": "media/copy.mp4"}]
    project_file = write_project(src, files)

    result = push_project(make_client(cloud), PushRequest(str(project_file), {"F1": str(a), "F2": str(copy)}),
                          website=WEBSITE)

    assert result.uploaded_files == 1
    assert cloud.count("PUT", r"/blob/[0-9a-f]{64}") == 1
    assert result.cloud_refs["F1"]["sha256"] == result.cloud_refs["F2"]["sha256"]


def test_second_push_reuses_cached_hashes_and_uploads_nothing(cloud, local_project, monkeypatch):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    reads = []
    real = cloud_sync.sha256_file
    monkeypatch.setattr(cloud_sync, "sha256_file", lambda path, **kw: reads.append(path) or real(path, **kw))

    second = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert reads == []                      # size + mtime matched: no file was read
    assert second.hashed_files == 0 and second.uploaded_files == 0 and second.already_in_cloud == 2
    assert not second.created and second.project_id == first.project_id
    assert second.cloud_refs == {}          # nothing to write back locally
    put = [h for m, p, _q, h in cloud.calls if m == "PUT" and p == f"/v1/projects/{first.project_id}"]
    assert put and put[-1]["If-Match"] == f'"{first.revision}"'


def test_a_changed_file_is_hashed_again(cloud, local_project, monkeypatch):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    local_project.a.write_bytes(random.Random(99).randbytes(300_000))   # same size, new bytes
    os.utime(local_project.a, (time.time() + 5, time.time() + 5))
    reads = []
    real = cloud_sync.sha256_file
    monkeypatch.setattr(cloud_sync, "sha256_file", lambda path, **kw: reads.append(path) or real(path, **kw))

    second = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert reads == [str(local_project.a)]
    assert second.uploaded_files == 1
    assert second.cloud_refs["F1"]["sha256"] == sha_of(local_project.a)
    assert set(second.cloud_refs) == {"F1"}


def test_large_files_use_put_block_and_put_block_list(cloud, tmp_path):
    src = tmp_path / "src"
    big = write_media(src / "media", "big.mov", 100_000, 3)
    project_file = write_project(src, [{"id": "F1", "path": "media/big.mov"}])
    sha = sha_of(big)
    cloud.fail_blocks[block_id(2)] = 1      # one transient failure: retried
    sent = []

    result = push_project(make_client(cloud), PushRequest(str(project_file), {"F1": str(big)}), website=WEBSITE,
                          progress=lambda p: sent.append(p) if p.phase == "upload" else None,
                          single_put_max=64 * 1024, block_size=16 * 1024)

    assert result.uploaded_files == 1
    assert cloud.media[sha]["data"] == big.read_bytes()
    assert cloud.count("PUT", f"/blob/{sha}", comp="block") == 7 + 1          # 7 blocks, block 2 twice
    assert cloud.count("PUT", f"/blob/{sha}", comp="block", blockid=block_id(2)) == 2
    assert cloud.count("PUT", f"/blob/{sha}", comp="blocklist") == 1
    assert cloud.blob_types[sha] == "video/quicktime"                       # set by Put Block List
    assert sent[-1].done == sent[-1].total == 100_000


def test_small_files_use_one_put_blob(cloud, local_project):
    push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    sha = sha_of(local_project.b)
    puts = [(q, h) for m, p, q, h in cloud.calls if m == "PUT" and p == f"/blob/{sha}"]
    assert len(puts) == 1
    query, headers = puts[0]
    assert "comp" not in query and headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["Content-Length"] == "120000" and headers["Content-Type"] == "audio/wav"


def test_an_expired_upload_link_is_renewed(cloud, local_project):
    cloud.expire_next_upload = True

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.uploaded_files == 2
    assert cloud.count("POST", "/v1/media") == 3   # one file declared twice (fresh SAS)


def test_a_long_upload_can_renew_its_link_more_than_once(cloud, tmp_path):
    src = tmp_path / "src"
    big = write_media(src / "media", "big.mov", 100_000, 4)
    project_file = write_project(src, [{"id": "F1", "path": "media/big.mov"}])
    cloud.expire_puts = {2, 5}          # the SAS "expires" twice, with progress in between

    result = push_project(make_client(cloud), PushRequest(str(project_file), {"F1": str(big)}), website=WEBSITE,
                          single_put_max=64 * 1024, block_size=16 * 1024)

    assert result.uploaded_files == 1 and cloud.media[sha_of(big)]["data"] == big.read_bytes()
    assert cloud.count("POST", "/v1/media") == 3      # declared once, renewed twice


def test_a_file_that_vanishes_before_upload_is_left_out(cloud, local_project):
    def progress(p):
        if p.phase == "check" and local_project.b.exists():
            local_project.b.unlink()      # deleted after hashing, before its upload

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE, progress=progress)

    assert result.uploaded_files == 1
    assert any(w.startswith("b.wav: couldn't be read") for w in result.warnings)
    stored = cloud.projects[result.project_id]["project"]
    assert "cloud" not in next(f for f in stored["files"] if f["id"] == "F2")


def test_conflict_asks_and_overwrites_with_if_match_star(cloud, local_project):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    web_revision = cloud.edit_in_web(first.project_id)
    asked = []

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE,
                          confirm_overwrite=lambda conflict: asked.append(conflict.revision) or True)

    assert asked == [web_revision]
    assert result.overwritten and result.project_id == first.project_id
    matches = [h["If-Match"] for m, p, _q, h in cloud.calls if m == "PUT" and p == f"/v1/projects/{first.project_id}"]
    assert matches == [f'"{first.revision}"', "*"]
    assert "web_note" not in cloud.projects[first.project_id]["project"]


def test_conflict_cancel_leaves_the_cloud_copy_alone(cloud, local_project):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    web_revision = cloud.edit_in_web(first.project_id)

    with pytest.raises(CloudCancelled) as info:
        push_project(make_client(cloud), request_for(local_project), website=WEBSITE,
                     confirm_overwrite=lambda conflict: False)

    assert info.value.code == "conflict_cancelled"
    assert cloud.projects[first.project_id]["revision"] == web_revision
    assert cloud.projects[first.project_id]["project"]["web_note"] == "edited in the browser"


def test_a_deleted_cloud_copy_is_recreated(cloud, local_project):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    del cloud.projects[first.project_id]

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.created and result.project_id != first.project_id
    assert any("deleted" in w for w in result.warnings)


def test_missing_local_files_are_skipped_with_a_warning(cloud, local_project):
    local_project.b.unlink()

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.uploaded_files == 1
    assert any(w.startswith("b.wav: not found at") and w.endswith("left out.") for w in result.warnings)
    stored = cloud.projects[result.project_id]["project"]
    assert "cloud" not in next(f for f in stored["files"] if f["id"] == "F2")


def test_a_file_missing_locally_keeps_its_earlier_cloud_copy(cloud, local_project):
    first = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    store_link(local_project.file, first)
    local_project.b.unlink()

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert any("the copy already in Zenvi Cloud is used" in w for w in result.warnings)
    stored = cloud.projects[result.project_id]["project"]
    assert next(f for f in stored["files"] if f["id"] == "F2")["cloud"]["sha256"] == first.cloud_refs["F2"]["sha256"]


def test_image_sequences_are_left_out(cloud, tmp_path):
    src = tmp_path / "src"
    project_file = write_project(src, [{"id": "F1", "path": "frames/shot_%04d.png"}])

    result = push_project(make_client(cloud), PushRequest(str(project_file), {"F1": str(src / "frames/shot_%04d.png")}),
                          website=WEBSITE)

    assert result.uploaded_files == 0
    assert any("image sequences" in w for w in result.warnings)


def test_not_signed_in_fails_before_any_request(cloud, local_project):
    with pytest.raises(CloudAuthError):
        push_project(make_client(cloud, token=None), request_for(local_project), website=WEBSITE)
    assert cloud.calls == []


def test_a_401_refreshes_the_session_once(cloud, local_project):
    client = make_client(cloud, token="stale", refreshed=TOKEN)
    result = push_project(client, request_for(local_project), website=WEBSITE)
    assert result.created
    assert sum(1 for _m, _p, _q, h in cloud.calls if h.get("Authorization") == "Bearer stale") == 1


def test_a_rejected_session_after_refresh_asks_to_sign_in(cloud, local_project):
    with pytest.raises(CloudAuthError):
        push_project(make_client(cloud, token="stale", refreshed="also-stale"), request_for(local_project),
                     website=WEBSITE)


def test_quota_exceeded_is_reported_with_the_numbers(cloud, local_project):
    cloud.quota_bytes = 1000

    with pytest.raises(CloudQuotaError) as info:
        push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert info.value.quota_bytes == 1000 and info.value.used_bytes == 0
    assert cloud.projects == {}


def test_rate_limited_calls_are_retried_after_retry_after(cloud, local_project):
    cloud.rate_limit_next = 2

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    assert result.created and cloud.rate_limit_next == 0


def test_offline_is_a_clear_error(local_project):
    with socket_on_free_port() as port:
        client = CloudClient(f"http://127.0.0.1:{port}", StaticTokenSource(TOKEN), retry_delay=0.0)
        with pytest.raises(CloudOfflineError) as info:
            push_project(client, request_for(local_project), website=WEBSITE)
    assert "Check your internet connection" in info.value.message


def test_a_netrc_default_entry_never_replaces_the_session_token(cloud, local_project, tmp_path, monkeypatch):
    """requests would otherwise turn a ~/.netrc match into Basic auth: on the
    API (overwriting our Bearer token) and on the storage URLs (leaking it)."""
    netrc = tmp_path / "netrc"
    netrc.write_text("default login someone password hunter2\n", encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))

    push_project(make_client(cloud), request_for(local_project), website=WEBSITE)

    auth_headers = {h.get("Authorization") for _m, p, _q, h in cloud.calls}
    assert auth_headers == {f"Bearer {TOKEN}", None}
    assert cloud.auth_on_storage == []


def test_a_non_api_answer_points_at_the_url_setting(tmp_path, local_project, monkeypatch):
    response = requests.Response()
    response.status_code = 404
    response._content = b"<html>Container App stopped</html>"
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **k: response)
    client = CloudClient("https://cloud.example", StaticTokenSource(TOKEN), retry_delay=0.0)
    with pytest.raises(cloud_sync.CloudError) as info:
        client.list_projects()
    assert "Check the Zenvi Cloud URL in Preferences" in info.value.message


def test_cancel_during_upload_stops_before_the_project_is_saved(cloud, local_project):
    cancel = CancelToken()

    def progress(p):
        if p.phase == "upload" and p.done > 0:
            cancel.cancel()

    with pytest.raises(CloudCancelled):
        push_project(make_client(cloud, cancel=cancel), request_for(local_project), website=WEBSITE,
                     progress=progress)
    assert cloud.projects == {}


def test_oversized_history_is_dropped_before_the_project_is(cloud, local_project):
    data = json.loads(local_project.file.read_text(encoding="utf-8"))
    data["history"]["undo"] = [{"type": "update", "key": ["clips"], "value": "x" * 5000}] * 20
    local_project.file.write_text(json.dumps(data), encoding="utf-8")

    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE,
                          max_project_bytes=60_000)

    assert result.history_dropped
    assert cloud.projects[result.project_id]["project"]["history"] == {"undo": [], "redo": []}


# ── Pull ──────────────────────────────────────────────────────────────────────

def pushed(cloud, local_project):
    result = push_project(make_client(cloud), request_for(local_project), website=WEBSITE)
    return result.project_id


def test_pull_downloads_media_and_rewrites_paths(cloud, local_project, tmp_path):
    project_id = pushed(cloud, local_project)
    root = tmp_path / "Cloud"

    result = pull_project(make_client(cloud), project_id, dest_root=str(root))

    folder = root / "My Film"
    media = folder / "My Film_assets" / "media"
    assert result.project_file == str(folder / "My Film.zvn")
    assert (media / "a.mp4").read_bytes() == local_project.a.read_bytes()
    assert (media / "b.wav").read_bytes() == local_project.b.read_bytes()
    data = json.loads((folder / "My Film.zvn").read_text(encoding="utf-8"))
    paths = {f["id"]: f["path"] for f in data["files"]}
    assert paths == {"F1": str(media / "a.mp4"), "F2": str(media / "b.wav")}
    assert {c["file_id"]: c["reader"]["path"] for c in data["clips"]} == paths
    assert data["effects"][0]["reader"]["path"] == "@transitions/common/fade.svg"
    assert data["zenvi_cloud"]["project_id"] == project_id
    assert data["zenvi_cloud"]["revision"] == cloud.projects[project_id]["revision"]
    ref = next(f for f in data["files"] if f["id"] == "F1")["cloud"]
    assert ref["mtime"] == os.stat(media / "a.mp4").st_mtime   # a later push reuses the hash
    assert result.downloaded_files == 2 and result.reused_files == 0
    assert not [n for n in os.listdir(media) if n.endswith(".part")]


def test_pull_again_skips_media_already_present_and_backs_up(cloud, local_project, tmp_path, monkeypatch):
    project_id = pushed(cloud, local_project)
    root = tmp_path / "Cloud"
    pull_project(make_client(cloud), project_id, dest_root=str(root))
    before = cloud.count("GET", r"/blob/[0-9a-f]{64}")
    reads = []
    real = cloud_sync.sha256_file
    monkeypatch.setattr(cloud_sync, "sha256_file", lambda path, **kw: reads.append(path) or real(path, **kw))
    backups = []

    result = pull_project(make_client(cloud), project_id, dest_root=str(root), backup=backups.append)

    assert cloud.count("GET", r"/blob/[0-9a-f]{64}") == before       # nothing downloaded again
    assert reads == []                                                # cached size+mtime: no re-hash
    assert result.reused_files == 2 and result.downloaded_files == 0
    assert result.replaced_existing and backups == [result.project_file]


def test_pull_verifies_a_present_file_without_a_cache_by_hash(cloud, local_project, tmp_path):
    project_id = pushed(cloud, local_project)
    media = tmp_path / "Cloud" / "My Film" / "My Film_assets" / "media"
    media.mkdir(parents=True)
    (media / "a.mp4").write_bytes(local_project.a.read_bytes())       # same bytes, no project file yet
    (media / "b.wav").write_bytes(b"something else entirely")          # same name, other bytes

    result = pull_project(make_client(cloud), project_id, dest_root=str(tmp_path / "Cloud"))

    assert result.reused_files == 1 and result.downloaded_files == 1
    data = json.loads(open(result.project_file, encoding="utf-8").read())
    assert {f["id"]: os.path.basename(f["path"]) for f in data["files"]} == {"F1": "a.mp4", "F2": "b (2).wav"}


def test_pull_gives_another_project_with_the_same_name_its_own_folder(cloud, local_project, tmp_path):
    first_id = pushed(cloud, local_project)
    # The link was not stored locally, so this push creates a second cloud project named "My Film".
    second_id = pushed(cloud, local_project)
    assert second_id != first_id
    root = tmp_path / "Cloud"

    first = pull_project(make_client(cloud), first_id, dest_root=str(root))
    second = pull_project(make_client(cloud), second_id, dest_root=str(root))

    assert os.path.dirname(first.project_file) == str(root / "My Film")
    assert os.path.dirname(second.project_file) == str(root / "My Film (2)")


def test_pull_reports_media_that_never_reached_the_cloud(cloud, local_project, tmp_path):
    local_project.b.unlink()
    project_id = pushed(cloud, local_project)

    result = pull_project(make_client(cloud), project_id, dest_root=str(tmp_path / "Cloud"))

    assert any(w.startswith("b.wav: not in Zenvi Cloud") for w in result.warnings)
    data = json.loads(open(result.project_file, encoding="utf-8").read())
    assert next(f for f in data["files"] if f["id"] == "F2")["path"] == "media/b.wav"  # left for relinking


def test_pull_then_push_needs_no_hashing_or_uploads(cloud, local_project, tmp_path, monkeypatch):
    project_id = pushed(cloud, local_project)
    pulled = pull_project(make_client(cloud), project_id, dest_root=str(tmp_path / "Cloud"))
    data = json.loads(open(pulled.project_file, encoding="utf-8").read())
    reads = []
    real = cloud_sync.sha256_file
    monkeypatch.setattr(cloud_sync, "sha256_file", lambda path, **kw: reads.append(path) or real(path, **kw))

    result = push_project(make_client(cloud),
                          PushRequest(pulled.project_file, {f["id"]: f["path"] for f in data["files"]}),
                          website=WEBSITE)

    assert reads == [] and result.uploaded_files == 0 and not result.created
    assert result.project_id == project_id and not result.overwritten


def test_a_disk_problem_during_pull_is_a_clear_error(cloud, local_project, tmp_path):
    project_id = pushed(cloud, local_project)
    not_a_folder = tmp_path / "Cloud"
    not_a_folder.write_text("a file where the folder should be", encoding="utf-8")

    with pytest.raises(cloud_sync.CloudError) as info:
        pull_project(make_client(cloud), project_id, dest_root=str(not_a_folder))

    assert info.value.code == "local_io" and "Couldn't save the project on this computer" in info.value.message


def test_cancelled_pull_leaves_no_project_file_or_partial_media(cloud, local_project, tmp_path):
    project_id = pushed(cloud, local_project)
    cancel = CancelToken()

    def progress(p):
        if p.phase == "download":   # the first report, before any byte is written
            cancel.cancel()

    with pytest.raises(CloudCancelled):
        pull_project(make_client(cloud, cancel=cancel), project_id, dest_root=str(tmp_path / "Cloud"),
                     progress=progress)
    folder = tmp_path / "Cloud" / "My Film"
    assert not (folder / "My Film.zvn").exists()
    leftovers = [n for n in os.listdir(folder / "My Film_assets" / "media")]
    assert all(not n.endswith(".part") for n in leftovers) and "a.mp4" not in leftovers


# ── GUI thread: the menu actions only snapshot state; the worker does the work ─

class _FakeProject:
    def __init__(self, path, files, dirty=False):
        self.current_filepath = path
        self._data = {"files": files, "zenvi_cloud": {}}
        self.has_unsaved_changes = dirty

    def needs_save(self):
        return self.has_unsaved_changes


class _FakeUpdates:
    def __init__(self):
        self.untracked = []
        self.tracked = []
        self.data_version = 1

    def update_untracked(self, key, values):
        self.untracked.append((key, values))

    def update(self, key, values):
        self.tracked.append((key, values))

    def insert(self, key, values):
        self.tracked.append((key, values))


class _FakeApp:
    def __init__(self, project, cloud_url):
        self.project = project
        self.updates = _FakeUpdates()
        self._settings = {"zenvi-cloud-url": cloud_url}
        self._tr = lambda text: text

    def get_settings(self):
        return SimpleNamespace(get=self._settings.get)


@pytest.fixture
def ui(monkeypatch):
    # These drive the controller with a MagicMock window, which only the headless
    # Qt stub accepts; tests/test_cloud_sync_qt.py covers the same paths on real Qt.
    if os.environ.get("ZENVI_REAL_QT") == "1":
        pytest.skip("uses the headless Qt stub; see test_cloud_sync_qt.py")
    from windows import cloud_sync_ui

    monkeypatch.delenv("ZENVI_CLOUD_URL", raising=False)
    return cloud_sync_ui


def _controller(ui, monkeypatch, cloud, project):
    app = _FakeApp(project, cloud.base_url)
    monkeypatch.setattr(ui, "get_app", lambda: app)
    # Deterministic message-box buttons (the Qt stub hands out a new mock per access).
    for name, value in (("Yes", 1), ("No", 2), ("Cancel", 4), ("Save", 8)):
        monkeypatch.setattr(ui.QMessageBox, name, value, raising=False)
    window = MagicMock()
    jobs = []
    controller = ui.CloudSyncController(window, start_job=jobs.append, tokens=StaticTokenSource(TOKEN))
    return controller, app, window, jobs


def test_push_action_does_no_io_on_the_gui_thread(ui, cloud, local_project, monkeypatch):
    files = [{"id": "F1", "path": str(local_project.a)}, {"id": "F2", "path": str(local_project.b)}]
    project = _FakeProject(str(local_project.file), files)
    controller, app, window, jobs = _controller(ui, monkeypatch, cloud, project)
    gui = threading.current_thread()
    seen = []   # (what, thread)

    def watch(name, real):
        def wrapper(*args, **kwargs):
            seen.append((name, threading.current_thread()))
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, "open", watch("open", builtins.open))
    monkeypatch.setattr(os, "stat", watch("os.stat", os.stat))
    monkeypatch.setattr(requests.Session, "request", watch("http", requests.Session.request))
    monkeypatch.setattr(hashlib, "sha256", watch("sha256", hashlib.sha256))

    controller.push()

    on_gui = [name for name, thread in seen if thread is gui]
    assert on_gui == [], f"the menu action did I/O on the GUI thread: {on_gui}"
    assert len(jobs) == 1 and controller.busy
    window.actionSave_trigger.assert_not_called()   # clean project: nothing to save

    results = []
    worker = threading.Thread(target=lambda: results.append(jobs[0](lambda _p: None)))
    worker.start()
    worker.join(timeout=30)
    monkeypatch.undo()

    assert not worker.is_alive() and results and results[0].project_id in cloud.projects
    on_worker = {name for name, thread in seen if thread is worker}
    assert {"open", "os.stat", "http", "sha256"} <= on_worker
    assert [name for name, thread in seen if thread is gui] == []

    # Back on the GUI thread: the result is applied without adding undo steps.
    monkeypatch.setattr(ui, "get_app", lambda: app)
    monkeypatch.setattr(ui, "PushResultDialog", MagicMock())
    controller._job_succeeded(results[0])
    assert not controller.busy
    assert app.updates.tracked == []
    assert (["zenvi_cloud"], results[0].link) in app.updates.untracked
    assert (["files", {"id": "F1"}], {"cloud": results[0].cloud_refs["F1"]}) in app.updates.untracked
    deadline = time.time() + 5
    while not window.save_project.called and time.time() < deadline:
        time.sleep(0.01)
    window.save_project.assert_called_once_with(str(local_project.file))


def test_push_saves_a_dirty_project_first(ui, cloud, local_project, monkeypatch):
    project = _FakeProject(str(local_project.file), [], dirty=True)
    controller, _app, window, jobs = _controller(ui, monkeypatch, cloud, project)
    window.actionSave_trigger.side_effect = lambda: setattr(project, "has_unsaved_changes", False)

    controller.push()

    window.actionSave_trigger.assert_called_once_with()
    assert len(jobs) == 1


def test_push_stops_when_the_save_fails(ui, cloud, local_project, monkeypatch):
    project = _FakeProject(str(local_project.file), [], dirty=True)
    controller, _app, window, jobs = _controller(ui, monkeypatch, cloud, project)

    controller.push()   # actionSave_trigger (a mock) leaves the project dirty

    assert jobs == [] and not controller.busy


def test_push_of_an_untitled_project_asks_to_save_first(ui, cloud, monkeypatch):
    project = _FakeProject(None, [])
    controller, _app, window, jobs = _controller(ui, monkeypatch, cloud, project)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *a, **k: ui.QMessageBox.Cancel)

    controller.push()

    window.actionSave_trigger.assert_not_called()
    assert jobs == []


def test_open_from_cloud_respects_cancel_at_the_unsaved_changes_prompt(ui, cloud, monkeypatch):
    project = _FakeProject("/tmp/x.zvn", [], dirty=True)
    controller, _app, window, jobs = _controller(ui, monkeypatch, cloud, project)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *a, **k: ui.QMessageBox.Cancel)
    dialogs = []
    monkeypatch.setattr(ui, "CloudProjectsDialog", lambda *a, **k: dialogs.append(a) or MagicMock())

    controller.open_from_cloud()

    assert dialogs == [] and jobs == []


def test_pull_does_not_ask_twice_about_changes_already_discarded(ui, cloud, monkeypatch, tmp_path):
    project = _FakeProject("/tmp/current.zvn", [], dirty=True)
    controller, app, window, _jobs = _controller(ui, monkeypatch, cloud, project)
    target = str(tmp_path / "Pulled.zvn")
    window.open_project.side_effect = lambda path: setattr(project, "current_filepath", path)
    result = cloud_sync.PullResult(project_file=target, project_id="c_" + "a" * 16, revision="r", name="Pulled")

    controller._pull_finished(result, discard_version=app.updates.data_version)
    assert project.has_unsaved_changes is False
    window.open_project.assert_called_once_with(target)

    # New edits since the "No" answer: open_project must ask again.
    project.has_unsaved_changes = True
    project.current_filepath = "/tmp/current.zvn"
    stale = app.updates.data_version
    app.updates.data_version += 1
    controller._pull_finished(result, discard_version=stale)
    assert project.has_unsaved_changes is True


def test_conflict_question_is_answered_on_the_gui_side(ui, cloud, monkeypatch):
    controller, _app, _window, _jobs = _controller(ui, monkeypatch, cloud, _FakeProject("/tmp/x.zvn", []))
    monkeypatch.setattr(controller, "_ask_overwrite", lambda conflict: True)
    conflict = cloud_sync.CloudConflictError("changed", revision="r2")
    assert controller._confirm_overwrite(conflict, CancelToken()) is True


def test_sign_in_is_offered_when_the_session_is_gone(ui, cloud, monkeypatch):
    controller, _app, _window, _jobs = _controller(ui, monkeypatch, cloud, _FakeProject("/tmp/x.zvn", []))
    offered = []
    monkeypatch.setattr(controller, "_offer_sign_in", lambda message, retry: offered.append(message))
    warnings = []
    monkeypatch.setattr(ui.QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    controller._show_failure(CloudAuthError("Sign in to your Zenvi account to use Zenvi Cloud."))
    controller._show_failure(CloudCancelled("Cancelled."))

    assert offered == ["Sign in to your Zenvi account to use Zenvi Cloud."] and warnings == []


def test_progress_labels_and_summaries(ui):
    label, fraction = ui.describe_progress(Progress("upload", 512 * 1024, 1024 * 1024, "a.mp4"))
    assert label == "Uploading a.mp4 — 512.0 KB of 1.0 MB" and fraction == 0.5
    assert ui.describe_progress(Progress("check"))[1] is None
    result = cloud_sync.PushResult(project_id="c_" + "a" * 16, revision="r", editor_url="u", name="N",
                                   created=True, overwritten=False, link={}, cloud_refs={},
                                   uploaded_files=2, uploaded_bytes=2048, already_in_cloud=3)
    assert ui.push_summary(result) == ("Uploaded 2 media file(s) (2.0 KB). 3 were already in Zenvi Cloud. "
                                       "Created a new cloud project.")
    assert ui.format_duration(3725.2) == "1:02:05" and ui.format_duration(42) == "0:42"
    assert ui.format_duration(None) == ""
    assert ui.format_updated("not a date") == "not a date"


# ── Project store integration ─────────────────────────────────────────────────

def test_zenvi_cloud_updates_never_reach_libopenshot():
    import threading as _threading

    from classes.timeline import TimelineSync
    from classes.updates import UpdateAction

    sync = TimelineSync.__new__(TimelineSync)
    sync.timeline = MagicMock()
    sync.timeline_lock = _threading.RLock()
    sync.window = MagicMock()
    TimelineSync.changed(sync, UpdateAction("update", ["zenvi_cloud"], {"project_id": "c_" + "a" * 16}))
    sync.timeline.ApplyJsonDiff.assert_not_called()


def test_every_project_has_a_zenvi_cloud_key(monkeypatch, tmp_path):
    from classes import project_data as pd

    def make_store(user_default):
        store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
        store._data = {}
        store.default_project_filepath = os.path.join(os.path.dirname(pd.__file__), "..", "settings",
                                                      "_default.project")
        store.read_from_file = lambda path, path_mode="ignore": json.loads(open(path, encoding="utf-8").read())
        store.get_profile = lambda **_kw: None
        store.apply_default_audio_settings = lambda: None
        monkeypatch.setattr(pd.info, "USER_DEFAULT_PROJECT", str(user_default))
        monkeypatch.setattr(pd.info, "reset_userdirs", lambda: None)
        monkeypatch.setattr(pd, "get_app", lambda: SimpleNamespace(
            get_settings=lambda: SimpleNamespace(get=lambda key: "HD 720p 30 fps", set=lambda *a: None)))
        store.new()
        return store

    assert make_store(tmp_path / "absent.zvn")._data["zenvi_cloud"] == {}
    custom = tmp_path / "default.zvn"
    custom.write_text(json.dumps({"files": [], "clips": []}), encoding="utf-8")
    assert make_store(custom)._data["zenvi_cloud"] == {}

    # update_untracked(["zenvi_cloud"], link) merges into it.
    store = make_store(tmp_path / "absent.zvn")
    old = store._set(["zenvi_cloud"], {"project_id": "c_" + "b" * 16, "revision": "r1"})
    assert old == {} and store._data["zenvi_cloud"]["revision"] == "r1"
