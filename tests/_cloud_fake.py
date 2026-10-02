"""Fake Zenvi Cloud for the cloud sync tests (not collected: leading underscore).

Runs in-process on a random port (threaded ``http.server``). Implements the
REST /v1 routes the desktop calls (zenvi-web packages/cloud/src/routes/v1.ts)
and the Azure blob calls behind the SAS URLs (Put Blob, Put Block, Put Block
List, GET), strictly enough to catch framing mistakes: Content-Length,
If-Match, block ids, server-side hash checks, and no Authorization header on
storage URLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from classes.cloud_sync import CloudClient, PushRequest, StaticTokenSource

TOKEN = "good-token"
WEBSITE = "https://zenvi.test"


# ── Fake Zenvi Cloud ──────────────────────────────────────────────────────────

class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that cancels mid-upload closes the socket; that is expected here.
        return None


class FakeCloud:
    def __init__(self):
        self.lock = threading.RLock()
        self.valid_tokens = {TOKEN}
        self.media = {}          # sha -> {"data", "name", "contentType"} (completed)
        self.pending = {}        # sha -> declared {"size", "name", "contentType"}
        self.blobs = {}          # sha -> committed bytes not yet completed
        self.blocks = {}         # sha -> {block id: bytes}
        self.blob_types = {}     # sha -> content type the upload set
        self.sas = {}            # sha -> signature the current upload URL carries
        self.projects = {}       # id -> {"name", "project", "revision", "createdAt", "updatedAt"}
        self.calls = []          # (method, path, query, headers)
        self.auth_on_storage = []
        self.fail_blocks = {}    # block id -> failures left (answer 500)
        self.expire_next_upload = False
        self.expire_puts = set()     # 1-based storage PUT numbers answered 403 (SAS expired)
        self.storage_puts = 0
        self.quota_bytes = None
        self.rate_limit_next = 0     # answer this many API calls with 429 + Retry-After
        self._counter = 0
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Headers and body go out in separate writes; without TCP_NODELAY every
            # response waits for a delayed ACK (tens of ms per request).
            disable_nagle_algorithm = True

            def log_message(self, *_args):
                return None

            def do_GET(self):
                cloud.dispatch(self, "GET")

            def do_POST(self):
                cloud.dispatch(self, "POST")

            def do_PUT(self):
                cloud.dispatch(self, "PUT")

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- helpers for tests

    def next_id(self):
        self._counter += 1
        return self._counter

    def new_revision(self):
        return f"0x8DE{self.next_id():012X}"

    def used(self):
        return sum(len(m["data"]) for m in self.media.values())

    def count(self, method, path_pattern, **query):
        pattern = re.compile(path_pattern)
        return sum(1 for m, p, q, _h in self.calls if m == method and pattern.fullmatch(p)
                   and all(q.get(k) == v for k, v in query.items()))

    def edit_in_web(self, project_id):
        """Someone saved the project in the web editor: new revision."""
        with self.lock:
            record = self.projects[project_id]
            record["project"]["web_note"] = "edited in the browser"
            record["revision"] = self.new_revision()
            return record["revision"]

    def add_media(self, data, name="clip.mp4", content_type="video/mp4"):
        sha = hashlib.sha256(data).hexdigest()
        self.media[sha] = {"data": data, "name": name, "contentType": content_type}
        return sha

    # -- HTTP

    def dispatch(self, handler, method):
        parts = urlsplit(handler.path)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        with self.lock:
            self.calls.append((method, parts.path, query, dict(handler.headers)))
            try:
                if parts.path.startswith("/blob/"):
                    status, payload, headers = self.storage(method, parts.path[6:], query, handler.headers, body)
                else:
                    status, payload, headers = self.api(method, parts.path, handler.headers, body)
            except Exception as exc:  # a bug in the fake itself: make it loud
                status, payload, headers = 500, {"error": {"code": "fake_bug", "message": repr(exc)}}, {}
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
            content_type = headers.pop("Content-Type", "application/octet-stream")
        else:
            data = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def error(status, code, message, **extra):
        return status, {"error": {"code": code, "message": message}, **extra}, {}

    def summary(self, project_id):
        record = self.projects[project_id]
        project = record["project"]
        return {
            "id": project_id, "name": record["name"], "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"], "revision": record["revision"],
            "stats": {"clips": len(project["clips"]), "files": len(project["files"]), "duration": 5.0,
                      "width": project["width"], "height": project["height"], "fps": 30},
            "editorUrl": f"https://zenvi.test/editor/p/{project_id}",
        }

    @staticmethod
    def valid_project(value):
        return (isinstance(value, dict) and isinstance(value.get("fps"), dict)
                and isinstance(value.get("width"), int) and isinstance(value.get("height"), int)
                and isinstance(value.get("clips"), list) and isinstance(value.get("files"), list)
                and all(isinstance(x, dict) and x.get("id") for x in value["clips"] + value["files"]))

    def api(self, method, path, headers, body):
        auth = headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:] in self.valid_tokens):
            return self.error(401, "unauthorized", "The access token is invalid, expired or revoked.")
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return 429, {"error": {"code": "rate_limited", "message": "Too many requests; slow down."}}, \
                {"Retry-After": "0"}
        data = json.loads(body) if body else {}
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        if method == "GET" and path == "/v1/me":
            return 200, {"user": {"id": "user-1", "email": None}, "auth": "supabase",
                         "usage": {"bytes": self.used(), "quotaBytes": self.quota_bytes}}, {}
        if method == "GET" and path == "/v1/projects":
            items = sorted((self.summary(pid) for pid in self.projects), key=lambda s: s["updatedAt"], reverse=True)
            return 200, {"projects": items}, {}
        if method == "POST" and path == "/v1/projects":
            if not self.valid_project(data.get("project")):
                return self.error(422, "invalid_project", "Not a valid Zenvi project")
            project_id = "c_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
            revision = self.new_revision()
            self.projects[project_id] = {"name": data.get("name") or "Untitled project", "project": data["project"],
                                         "revision": revision, "createdAt": now, "updatedAt": now}
            return 201, self.summary(project_id), {"ETag": f'"{revision}"'}
        match = re.fullmatch(r"/v1/projects/(c_[a-z0-9]{16})", path)
        if match:
            project_id = match.group(1)
            record = self.projects.get(project_id)
            if record is None:
                return self.error(404, "not_found", "Project not found")
            if method == "GET":
                media, missing = [], []
                for entry in record["project"]["files"]:
                    ref = entry.get("cloud") if isinstance(entry.get("cloud"), dict) else {}
                    sha = ref.get("sha256")
                    name = entry.get("name") or os.path.basename(str(entry.get("path") or "")) or entry["id"]
                    if not sha:
                        missing.append({"fileId": entry["id"], "name": name, "reason": "not_in_cloud"})
                    elif sha in self.media:
                        info = self.media[sha]
                        media.append({"fileId": entry["id"], "sha256": sha, "name": info["name"],
                                      "contentType": info["contentType"], "size": len(info["data"]),
                                      "url": f"{self.base_url}/blob/{sha}?sp=r&sig=read",
                                      "expiresAt": now})
                    else:
                        missing.append({"fileId": entry["id"], "name": name, "sha256": sha, "reason": "not_uploaded"})
                return 200, {**self.summary(project_id), "project": record["project"], "media": media,
                             "missingMedia": missing}, {"ETag": f'"{record["revision"]}"'}
            if method == "PUT":
                if_match = headers.get("If-Match")
                if not if_match:
                    return self.error(428, "precondition_required", "Send If-Match")
                expected = if_match.strip().strip('"')
                if expected != "*" and expected != record["revision"]:
                    return self.error(409, "revision_conflict", "The project changed since you loaded it.",
                                      revision=record["revision"])
                if not self.valid_project(data):
                    return self.error(422, "invalid_project", "Not a valid Zenvi project")
                record.update(project=data, revision=self.new_revision(), updatedAt=now)
                return 200, {"id": project_id, "revision": record["revision"], "updatedAt": now,
                             "stats": self.summary(project_id)["stats"]}, {"ETag": f'"{record["revision"]}"'}
        if method == "POST" and path == "/v1/media/check":
            hashes = [h.lower() for h in data["sha256"]]
            assert len(hashes) <= 1000
            return 200, {"existing": [h for h in hashes if h in self.media],
                         "missing": [h for h in hashes if h not in self.media]}, {}
        if method == "POST" and path == "/v1/media":
            sha = data["sha256"].lower()
            if sha in self.media:
                return 200, {"sha256": sha, "blobPath": f"u/user-1/media/{sha}", "exists": True}, {}
            if self.quota_bytes is not None and self.used() + data["size"] > self.quota_bytes:
                return self.error(413, "quota_exceeded", "This upload would exceed your Zenvi Cloud storage.",
                                  usedBytes=self.used(), quotaBytes=self.quota_bytes)
            self.pending[sha] = {"size": data["size"], "name": data["name"], "contentType": data["contentType"]}
            self.sas[sha] = f"w{self.next_id()}"
            return 201, {
                "sha256": sha, "blobPath": f"u/user-1/media/{sha}", "exists": False,
                "uploadUrl": f"{self.base_url}/blob/{sha}?sv=2025-05-05&sp=cw&sig={self.sas[sha]}",
                "uploadExpiresAt": now,
                "uploadHeaders": {"x-ms-blob-type": "BlockBlob", "Content-Type": data["contentType"]},
            }, {}
        match = re.fullmatch(r"/v1/media/([0-9a-f]{64})/complete", path)
        if match and method == "POST":
            sha = match.group(1)
            blob = self.blobs.get(sha)
            if blob is None:
                return self.error(404, "not_found", "Uploaded media not found")
            pending = self.pending.get(sha) or {}
            if pending.get("size") != len(blob):
                self.blobs.pop(sha)
                return self.error(422, "size_mismatch", "Size mismatch. Upload the file again.")
            if hashlib.sha256(blob).hexdigest() != sha:
                self.blobs.pop(sha)
                return self.error(422, "hash_mismatch", "The uploaded bytes do not match. Upload again.")
            self.media[sha] = {"data": self.blobs.pop(sha), "name": pending["name"],
                               "contentType": pending["contentType"]}
            self.pending.pop(sha, None)
            return 200, {"sha256": sha, "size": len(blob), "name": pending["name"],
                         "contentType": pending["contentType"], "verified": True}, {}
        match = re.fullmatch(r"/v1/media/([0-9a-f]{64})/url", path)
        if match and method == "GET":
            return 200, {"url": f"{self.base_url}/blob/{match.group(1)}?sp=r&sig=read", "expiresAt": now}, {}
        return self.error(404, "not_found", "No such endpoint.")

    def storage(self, method, sha, query, headers, body):
        if headers.get("Authorization"):
            self.auth_on_storage.append((method, sha))
        if method == "GET":
            if query.get("sig") != "read" or sha not in self.media:
                return 404, b"", {"x-ms-error-code": "BlobNotFound"}
            return 200, self.media[sha]["data"], {"Content-Type": self.media[sha]["contentType"]}
        self.storage_puts += 1
        if query.get("sig") != self.sas.get(sha) or self.expire_next_upload or self.storage_puts in self.expire_puts:
            self.expire_next_upload = False
            return 403, b"", {"x-ms-error-code": "AuthenticationFailed"}
        comp = query.get("comp")
        if comp == "block":
            bid = query["blockid"]
            if self.fail_blocks.get(bid):
                self.fail_blocks[bid] -= 1
                return 500, b"", {"x-ms-error-code": "InternalError"}
            if len(body) != int(headers.get("Content-Length") or -1):
                return 400, b"", {"x-ms-error-code": "InvalidHeaderValue"}
            self.blocks.setdefault(sha, {})[bid] = body
            return 201, b"", {}
        if comp == "blocklist":
            ids = [element.text for element in ET.fromstring(body).findall("Latest")]
            stored = self.blocks.get(sha, {})
            if not ids or any(i not in stored for i in ids) or len({len(i) for i in ids}) != 1:
                return 400, b"", {"x-ms-error-code": "InvalidBlockList"}
            self.blobs[sha] = b"".join(stored[i] for i in ids)
            self.blob_types[sha] = headers.get("x-ms-blob-content-type")
            self.blocks.pop(sha, None)
            return 201, b"", {}
        if comp is None:
            if headers.get("x-ms-blob-type") != "BlockBlob":
                return 400, b"", {"x-ms-error-code": "MissingRequiredHeader"}
            self.blobs[sha] = body
            self.blob_types[sha] = headers.get("Content-Type")
            return 201, b"", {}
        return 400, b"", {"x-ms-error-code": "InvalidQueryParameterValue"}


def make_client(cloud, token=TOKEN, cancel=None, refreshed=None):
    return CloudClient(cloud.base_url, StaticTokenSource(token, refreshed), cancel=cancel, retry_delay=0.0)


# ── Local project fixture ─────────────────────────────────────────────────────

def write_media(folder, name, size, seed):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(random.Random(seed).randbytes(size))
    return path


def write_project(folder, files, name="My Film", link=None):
    """A saved .zvn in the desktop's portable form: media paths relative to the
    project folder, bundled assets as @transitions/… markers."""
    project = {
        "id": "T0ABCDEFGH",
        "fps": {"num": 30, "den": 1}, "display_ratio": {"num": 16, "den": 9}, "pixel_ratio": {"num": 1, "den": 1},
        "width": 1280, "height": 720, "sample_rate": 48000, "channels": 2, "channel_layout": 3,
        "settings": {},
        "files": files,
        "clips": [{"id": f"C{i}", "file_id": f["id"], "layer": 1000000, "position": i * 5.0, "start": 0, "end": 5,
                   "reader": {"path": f["path"], "has_video": True}} for i, f in enumerate(files)],
        "effects": [{"id": "TR1", "type": "Mask", "reader": {"path": "@transitions/common/fade.svg"}}],
        "layers": [{"id": "L1", "number": 1000000, "label": "", "y": 0, "lock": False}],
        "markers": [], "history": {"undo": [{"type": "insert", "key": ["clips"], "value": {}}], "redo": []},
        "zenvi_cloud": link or {},
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.zvn"
    path.write_text(json.dumps(project, indent=1), encoding="utf-8")
    return path


def make_local_project(tmp_path):
    """Two media files and a saved project that uses them (portable paths)."""
    src = tmp_path / "src"
    a = write_media(src / "media", "a.mp4", 300_000, 1)
    b = write_media(src / "media", "b.wav", 120_000, 2)
    files = [
        {"id": "F1", "path": "media/a.mp4", "media_type": "video", "fingerprint": {"size": 1, "sha256": "0" * 64},
         "proxy_reader": {"path": "@assets/optimized/a.mp4"}},
        {"id": "F2", "path": "media/b.wav", "media_type": "audio"},
    ]
    project_file = write_project(src, files)
    local_paths = {"F1": str(a), "F2": str(b)}
    return SimpleNamespace(folder=src, file=project_file, paths=local_paths, a=a, b=b)


def request_for(local):
    return PushRequest(project_file=str(local.file), local_paths=dict(local.paths))


def store_link(project_file, result):
    """What the GUI does after a push (update_untracked + save), on the file."""
    data = json.loads(project_file.read_text(encoding="utf-8"))
    for entry in data["files"]:
        if entry["id"] in result.cloud_refs:
            entry["cloud"] = result.cloud_refs[entry["id"]]
    data["zenvi_cloud"] = result.link
    project_file.write_text(json.dumps(data, indent=1), encoding="utf-8")


def sha_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class socket_on_free_port:
    """A port nothing listens on (bound, then released)."""

    def __enter__(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        return self.port

    def __exit__(self, *exc):
        return False
