"""Local chat-history store for the Zenvi chat dock.

The dock lets a user switch backends (Zenvi Assistant, Claude Code, Codex)
while editing a project, but until now nothing kept the *visible* transcript:
the local session store held metadata only, the Zenvi backend's copy lived in
a process-local dict, and the CLI agents' transcripts were only in their own
JSONL dirs.  So reopening a project showed an empty pane even though the
model still had the context.

This module is the durable, offline, per-project source of truth for those
transcripts.  It is written from the one place every backend converges on --
the chat window's message sink -- so a single hook captures all three.

Design notes:

* Messages are stored as RAW text/markdown, never rendered HTML.  HTML is a UI
  artifact that would rot on any restyle and cannot be re-rendered or synced.
* ``messages`` is append-only and immutable, keyed ``(session_id, seq)``.  That
  is what would make a future server-side sync a last-write-wins merge with no
  conflict resolution.
* ``tool_events`` records only which tool ran and how it ended.  Full argument
  and result payloads stay in the CLI's own transcript, reachable through the
  stored ``cli_session_id``.
* Every public call is wrapped so a database failure can never break a chat
  turn -- matching the existing "chat can still run without local
  persistence" contract in the session store.
"""

from __future__ import annotations

import functools
import hashlib
import os
import sqlite3
import threading
import time
import uuid

from classes import info
from classes.logger import log

# Module-level so tests can point it at a tmp path (cf. agent_gap_log.GAP_LOG_PATH).
CHAT_DB_PATH = os.path.join(info.USER_PATH, "chat_history.db")

SCHEMA_VERSION = 1

_lock = threading.RLock()
_conn = None
_conn_path = None

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    project_key    TEXT NOT NULL,
    project_path   TEXT,
    title          TEXT,
    backend        TEXT,
    agent_mode     TEXT,
    cli_session_id TEXT,
    -- Nullable on purpose: an omitted field is written as NULL so the upsert's
    -- COALESCE can tell "leave alone" from "set to false".  Read it as a bool.
    cli_started    INTEGER DEFAULT 0,
    cli_cwd        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions (project_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_path
    ON sessions (project_path);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    after_seq  INTEGER NOT NULL,
    call_id    TEXT,
    tool_name  TEXT,
    title      TEXT,
    status     TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_events_session
    ON tool_events (session_id, after_seq, id);
"""


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha1(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _migrate(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        version = int(row["value"]) if row else 0
    except (TypeError, ValueError):
        version = 0

    # Migration ladder: each step upgrades one version and falls through.
    if version < 1:
        conn.executescript(_SCHEMA_V1)
        version = 1

    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )
    conn.commit()


def _connect():
    """Return the shared connection, (re)opening and migrating as needed.

    Caller must hold ``_lock``.  Reopens when ``CHAT_DB_PATH`` changes so tests
    can repoint the module constant.
    """
    global _conn, _conn_path
    if _conn is not None and _conn_path == CHAT_DB_PATH:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
        _conn_path = None

    parent = os.path.dirname(CHAT_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(CHAT_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)

    _conn = conn
    _conn_path = CHAT_DB_PATH
    return conn


def _store(default=None, factory=None):
    """Wrap a store call: serialize it, inject the connection, never raise.

    The wrapped function receives the live connection as its first argument.
    """
    def deco(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                with _lock:
                    return fn(_connect(), *args, **kwargs)
            except Exception as ex:
                log.debug("chat_history.%s failed: %s", fn.__name__, ex)
                return factory() if factory is not None else default
        return inner
    return deco


def close() -> None:
    """Close the shared connection (app shutdown, and between tests)."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _conn_path = None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_SESSION_FIELDS = (
    "project_key", "project_path", "title", "backend",
    "agent_mode", "cli_session_id", "cli_started", "cli_cwd",
)


def new_draft_key() -> str:
    """Bucket for an untitled project: stable for one window, never reused.

    An unsaved project regenerates its project id on every ``new()``, so the
    id is worthless as a key -- but we still want the chat to survive until the
    user saves, at which point the bucket is rekeyed onto the real project.
    """
    return "draft:" + uuid.uuid4().hex


def _path_key(project_path: str) -> str:
    """Fallback bucket for a project whose file carries no usable id."""
    return "path:" + _sha1(os.path.abspath(project_path))[:16]


def _ensure_session(conn, session_id: str, project_key: str = "") -> None:
    """Insert a stub row so a message can never be orphaned by a missing parent."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, project_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, project_key or "", now, now),
    )


@_store()
def upsert_session(conn, session_id: str, project_key: str, **fields) -> None:
    """Create or refresh a session row.  Only non-None *fields* are written."""
    now = _now()
    values = {k: fields.get(k) for k in _SESSION_FIELDS}
    values["project_key"] = project_key
    if values.get("cli_started") is not None:
        values["cli_started"] = 1 if values["cli_started"] else 0

    cols = ", ".join(_SESSION_FIELDS)
    marks = ", ".join("?" for _ in _SESSION_FIELDS)
    # In DO UPDATE, a bare column name is the existing row's value, so COALESCE
    # leaves anything the caller passed as None untouched.
    updates = ", ".join(
        "%s = COALESCE(excluded.%s, %s)" % (c, c, c)
        for c in _SESSION_FIELDS if c != "project_key"
    )
    conn.execute(
        "INSERT INTO sessions (session_id, %s, created_at, updated_at) "
        "VALUES (?, %s, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  project_key = excluded.project_key, %s, updated_at = excluded.updated_at"
        % (cols, marks, updates),
        (session_id, *[values[c] for c in _SESSION_FIELDS], now, now),
    )
    conn.commit()


@_store()
def update_session(conn, session_id: str, **fields) -> None:
    """Patch an existing session row in place; a no-op if it is not there."""
    sets, params = [], []
    for key in _SESSION_FIELDS:
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if key == "cli_started":
            value = 1 if value else 0
        sets.append("%s = ?" % key)
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([_now(), session_id])
    conn.execute(
        "UPDATE sessions SET %s WHERE session_id = ?" % ", ".join(sets), params
    )
    conn.commit()


@_store(factory=list)
def load_sessions(conn, project_key: str, include_closed: bool = False) -> list:
    """Session rows for one project bucket, oldest first (tab order)."""
    sql = "SELECT * FROM sessions WHERE project_key = ?"
    if not include_closed:
        sql += " AND closed_at IS NULL"
    sql += " ORDER BY created_at, rowid"
    return [dict(r) for r in conn.execute(sql, (project_key,)).fetchall()]


@_store()
def mark_session_closed(conn, session_id: str) -> None:
    """Soft-delete: the tab goes away, the transcript stays recoverable.

    Closing a tab also clears the backend's own copy, so this local row can be
    the only surviving record of the conversation.
    """
    conn.execute(
        "UPDATE sessions SET closed_at = ?, updated_at = ? WHERE session_id = ?",
        (_now(), _now(), session_id),
    )
    conn.commit()


@_store()
def reopen_session(conn, session_id: str) -> None:
    """Clear ``closed_at`` so the session can be restored as an open tab."""
    conn.execute(
        "UPDATE sessions SET closed_at = NULL, updated_at = ? WHERE session_id = ?",
        (_now(), session_id),
    )
    conn.commit()


@_store(factory=list)
def load_closed_sessions(conn, project_key: str) -> list:
    """Closed sessions that still have a transcript, newest first."""
    return [
        dict(r) for r in conn.execute(
            "SELECT s.* FROM sessions s "
            "WHERE s.project_key = ? AND s.closed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.session_id) "
            "ORDER BY s.updated_at DESC, s.rowid DESC",
            (project_key,),
        ).fetchall()
    ]



# ---------------------------------------------------------------------------
# Messages and tool events
# ---------------------------------------------------------------------------

@_store()
def record_message(conn, session_id: str, role: str, content: str):
    """Append one final message and return its per-session ``seq``."""
    if not session_id or not role:
        return None
    _ensure_session(conn, session_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at) "
        "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?), ?, ?, ?)",
        (session_id, session_id, role, content or "", now),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id)
    )
    conn.commit()
    row = conn.execute(
        "SELECT seq FROM messages WHERE rowid = ?", (cur.lastrowid,)
    ).fetchone()
    return row["seq"] if row else None


@_store(factory=list)
def load_messages(conn, session_id: str) -> list:
    """All messages for a session in send order."""
    return [
        dict(r) for r in conn.execute(
            "SELECT seq, role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    ]


@_store()
def record_tool_event(conn, session_id: str, call_id: str, tool_name: str, title: str) -> None:
    """Note that a tool started, anchored to the turn it belongs to."""
    if not session_id:
        return
    _ensure_session(conn, session_id)
    conn.execute(
        "INSERT INTO tool_events (session_id, after_seq, call_id, tool_name, title, status, created_at) "
        "VALUES (?, (SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?), ?, ?, ?, 'running', ?)",
        (session_id, session_id, call_id or "", tool_name or "", title or "", _now()),
    )
    conn.commit()


@_store()
def complete_tool_event(conn, session_id: str, call_id: str, ok: bool) -> None:
    """Close out the most recent running event for this call id."""
    conn.execute(
        "UPDATE tool_events SET status = ? WHERE id = ("
        "  SELECT id FROM tool_events WHERE session_id = ? AND call_id = ? "
        "  ORDER BY id DESC LIMIT 1)",
        ("ok" if ok else "error", session_id, call_id or ""),
    )
    conn.commit()


@_store(factory=list)
def load_tool_events(conn, session_id: str) -> list:
    """Tool events for a session, in the order they should be replayed."""
    return [
        dict(r) for r in conn.execute(
            "SELECT after_seq, call_id, tool_name, title, status FROM tool_events "
            "WHERE session_id = ? ORDER BY after_seq, id",
            (session_id,),
        ).fetchall()
    ]


@_store()
def clear_session_messages(conn, session_id: str) -> None:
    """Drop a session's transcript but keep the session itself."""
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM tool_events WHERE session_id = ?", (session_id,))
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (_now(), session_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Project keying
# ---------------------------------------------------------------------------

def path_key(project_path: str) -> str:
    """Public form of the path-derived fallback bucket."""
    return _path_key(project_path)


def _copy_session(conn, session_id: str) -> str:
    """Duplicate one session and its transcript under a fresh id.

    The copy is inert history: it deliberately drops the CLI continuity
    fields, so nothing can resume the same CLI conversation twice.
    """
    new_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (session_id, project_key, project_path, title, backend, "
        "                      agent_mode, cli_session_id, cli_started, cli_cwd, "
        "                      created_at, updated_at, closed_at) "
        "SELECT ?, project_key, project_path, title, backend, agent_mode, "
        "       NULL, 0, NULL, created_at, updated_at, closed_at "
        "FROM sessions WHERE session_id = ?",
        (new_id, session_id),
    )
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at) "
        "SELECT ?, seq, role, content, created_at FROM messages WHERE session_id = ?",
        (new_id, session_id),
    )
    conn.execute(
        "INSERT INTO tool_events (session_id, after_seq, call_id, tool_name, title, status, created_at) "
        "SELECT ?, after_seq, call_id, tool_name, title, status, created_at "
        "FROM tool_events WHERE session_id = ?",
        (new_id, session_id),
    )
    return new_id


def _fork(conn, src_key: str, new_key: str, new_path: str) -> None:
    """Split a bucket in two after a Save As.

    The *live* rows move to *new_key* -- the user is now editing the copy, so
    their tabs keep their session ids, backend threads and CLI continuity
    intact.  A duplicate of each row stays behind under *src_key* as the
    original file's frozen snapshot.
    """
    rows = conn.execute(
        "SELECT session_id FROM sessions WHERE project_key = ?", (src_key,)
    ).fetchall()
    if not rows:
        return
    for row in rows:
        _copy_session(conn, row["session_id"])
    conn.execute(
        "UPDATE sessions SET project_key = ?, project_path = ?, updated_at = ? "
        "WHERE project_key = ? AND session_id IN (%s)"
        % ", ".join("?" for _ in rows),
        (new_key, new_path, _now(), src_key, *[r["session_id"] for r in rows]),
    )
    conn.commit()


@_store(default=0)
def discard_empty_bucket(conn, project_key: str) -> int:
    """Drop a bucket that never got a message — abandoned drafts, mostly.

    "New Project" walks away from its draft bucket; without this they would
    pile up forever.  Buckets that hold an actual conversation are left alone.
    """
    if not project_key:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id IN "
        "(SELECT session_id FROM sessions WHERE project_key = ?)",
        (project_key,),
    ).fetchone()
    if row and row["n"]:
        return 0
    cur = conn.execute("DELETE FROM sessions WHERE project_key = ?", (project_key,))
    conn.commit()
    return cur.rowcount or 0


@_store()
def rekey_project(conn, old_key: str, new_key: str, new_path: str = None) -> None:
    """Move a whole bucket to a new key (draft -> saved project, id churn)."""
    if not old_key or not new_key or old_key == new_key:
        return
    if new_path is None:
        conn.execute(
            "UPDATE sessions SET project_key = ?, updated_at = ? WHERE project_key = ?",
            (new_key, _now(), old_key),
        )
    else:
        conn.execute(
            "UPDATE sessions SET project_key = ?, project_path = ?, updated_at = ? "
            "WHERE project_key = ?",
            (new_key, os.path.abspath(new_path), _now(), old_key),
        )
    _move_active_session(conn, old_key, new_key)
    conn.commit()


_ACTIVE_PREFIX = "active:"


def _move_active_session(conn, old_key: str, new_key: str) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_ACTIVE_PREFIX + old_key,)
    ).fetchone()
    if not row or not row["value"]:
        return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_ACTIVE_PREFIX + new_key, row["value"]),
    )
    conn.execute("DELETE FROM meta WHERE key = ?", (_ACTIVE_PREFIX + old_key,))


@_store()
def set_active_session(conn, project_key: str, session_id: str) -> None:
    """Remember which tab was selected for this project."""
    if not project_key:
        return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_ACTIVE_PREFIX + project_key, session_id or ""),
    )
    conn.commit()


@_store(default="")
def get_active_session(conn, project_key: str) -> str:
    if not project_key:
        return ""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (_ACTIVE_PREFIX + project_key,),
    ).fetchone()
    return (row["value"] if row else "") or ""


@_store()
def resolve_project_key(conn, project_id: str, project_path: str):
    """Bucket for a *saved* project, repairing the mapping as a side effect.

    Keyed on the project's own id rather than its path, so renaming or moving
    a project file keeps its history.  Three hazards make this more than a
    lookup, and each is detected via the recorded ``project_path``:

    * Save As copies the id verbatim, so id alone would silently share one
      transcript between two files -- forked here.
    * A legacy ``"T0"`` project is handed a fresh random id on every open
      (the migration never marks the project dirty), so the id churns while
      the path stays put -- adopted by path here.
    * Untitled projects regenerate their id too; callers must use
      ``new_draft_key`` for those and never reach this function.
    """
    if not project_path:
        return None
    ppath = os.path.abspath(project_path)
    pid = (project_id or "").strip()
    if not pid or pid == "T0":
        return _path_key(ppath)

    # Every bucket descended from this project id: the id itself plus any
    # fork created by a Save As.  Fork keys carry a random suffix rather than a
    # path hash, so that renaming a forked project does not change its key.
    family, seen = [], set()
    for row in conn.execute(
        "SELECT project_key, project_path, MAX(updated_at) AS seen_at FROM sessions "
        "WHERE project_key = ? OR project_key LIKE ? "
        "GROUP BY project_key, project_path ORDER BY seen_at DESC",
        (pid, pid + ":%"),
    ).fetchall():
        if row["project_key"] in seen:
            continue
        seen.add(row["project_key"])
        family.append(row)

    # 1. A bucket already recorded against this exact file.
    for row in family:
        if row["project_path"] and os.path.abspath(row["project_path"]) == ppath:
            return row["project_key"]

    # 2. A bucket whose file is no longer there -- this project was renamed or
    #    moved, which is precisely what id-keying exists to survive.
    for row in family:
        recorded = row["project_path"]
        if not recorded or not os.path.exists(recorded):
            conn.execute(
                "UPDATE sessions SET project_path = ?, updated_at = ? WHERE project_key = ?",
                (ppath, _now(), row["project_key"]),
            )
            conn.commit()
            return row["project_key"]

    # 3. Every sibling is still on disk, so this is a copy of one of them.
    #    Fork off the most recently used -- the project the user saved from.
    if family:
        fork_key = "%s:%s" % (pid, uuid.uuid4().hex[:8])
        _fork(conn, family[0]["project_key"], fork_key, ppath)
        return fork_key

    # 4. Nothing under this id.  If a bucket is recorded at this exact path the
    #    id churned underneath us (the T0 case) -- adopt it.
    row = conn.execute(
        "SELECT project_key FROM sessions WHERE project_path = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (ppath,),
    ).fetchone()
    if row and row["project_key"] and row["project_key"] != pid:
        conn.execute(
            "UPDATE sessions SET project_key = ?, updated_at = ? WHERE project_key = ?",
            (pid, _now(), row["project_key"]),
        )
        conn.commit()
    return pid


@_store(default=0)
def import_legacy_sessions(conn, project_key: str, project_path: str, sessions: list) -> int:
    """Seed a bucket from the old metadata-only JSON store.

    Those files are named by a one-way hash of the project path, so they can
    only be imported once we know which project we are looking at.  They hold
    no messages -- this is purely to keep tab titles and backend choices.
    """
    if not sessions:
        return 0
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE project_key = ? LIMIT 1", (project_key,)
    ).fetchone()
    if existing:
        return 0
    now = _now()
    count = 0
    for sess in sessions:
        sid = (sess or {}).get("session_id")
        if not sid:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, project_key, project_path, title, "
            "                                backend, agent_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid, project_key, os.path.abspath(project_path) if project_path else None,
                sess.get("title"), sess.get("backend"), sess.get("agent_mode"), now, now,
            ),
        )
        count += 1
    conn.commit()
    return count
