"""Unit tests for the local chat-history store (classes.chat_history)."""

import os
import threading

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    from classes import chat_history as ch
    ch.close()
    monkeypatch.setattr(ch, "CHAT_DB_PATH", str(tmp_path / "chat_history.db"))
    yield ch
    ch.close()


@pytest.fixture
def project(tmp_path):
    """A project file that actually exists, so path checks are meaningful."""
    path = tmp_path / "a.zvn"
    path.write_text("{}")
    return str(path)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_fresh_db_is_created_and_versioned(store, tmp_path):
    store.upsert_session("s1", "P1")
    assert os.path.isfile(str(tmp_path / "chat_history.db"))
    assert store.load_sessions("P1")


def test_message_round_trip_stores_raw_text(store):
    store.upsert_session("s1", "P1")
    store.record_message("s1", "user", "make it **bold**")
    store.record_message("s1", "assistant", "# done\n- one")
    msgs = store.load_messages("s1")
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "make it **bold**"),
        ("assistant", "# done\n- one"),
    ]


def test_seq_is_monotonic_per_session(store):
    store.upsert_session("a", "P1")
    store.upsert_session("b", "P1")
    assert [store.record_message("a", "user", str(i)) for i in range(3)] == [1, 2, 3]
    # A second session numbers itself independently.
    assert store.record_message("b", "user", "x") == 1
    assert [m["seq"] for m in store.load_messages("a")] == [1, 2, 3]


def test_message_survives_a_missing_session_row(store):
    """A dropped parent row must never cost us the transcript."""
    assert store.record_message("orphan", "user", "hi") == 1
    assert len(store.load_messages("orphan")) == 1


def test_clear_session_messages_keeps_the_session(store):
    store.upsert_session("s1", "P1", title="Keep me")
    store.record_message("s1", "user", "hi")
    store.record_tool_event("s1", "c1", "list_clips_tool", "Listing")
    store.clear_session_messages("s1")
    assert store.load_messages("s1") == []
    assert store.load_tool_events("s1") == []
    assert store.load_sessions("P1")[0]["title"] == "Keep me"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_upsert_preserves_fields_the_caller_omits(store):
    store.upsert_session("s1", "P1", title="First", backend="codex", agent_mode="agent")
    store.upsert_session("s1", "P1")  # a bare re-register, e.g. on reload
    row = store.load_sessions("P1")[0]
    assert (row["title"], row["backend"], row["agent_mode"]) == ("First", "codex", "agent")


def test_update_session_patches_in_place(store):
    store.upsert_session("s1", "P1", backend="claude_code")
    store.update_session("s1", title="Renamed", cli_session_id="cli-1", cli_started=True)
    row = store.load_sessions("P1")[0]
    assert row["title"] == "Renamed"
    assert row["cli_session_id"] == "cli-1"
    assert bool(row["cli_started"]) is True
    assert row["backend"] == "claude_code"


def test_update_session_on_unknown_id_is_a_no_op(store):
    store.update_session("nope", title="x")
    assert store.load_sessions("P1") == []


def test_closing_a_session_hides_it_but_keeps_the_transcript(store):
    store.upsert_session("s1", "P1")
    store.record_message("s1", "user", "hi")
    store.mark_session_closed("s1")
    assert store.load_sessions("P1") == []
    assert len(store.load_sessions("P1", include_closed=True)) == 1
    assert len(store.load_messages("s1")) == 1


def test_tool_events_record_status_and_anchor_to_the_turn(store):
    store.upsert_session("s1", "P1")
    store.record_message("s1", "user", "list my clips")
    store.record_tool_event("s1", "c1", "list_clips_tool", "Listing clips")
    store.complete_tool_event("s1", "c1", True)
    store.record_tool_event("s1", "c2", "export_video_tool", "Exporting")
    store.complete_tool_event("s1", "c2", False)
    events = store.load_tool_events("s1")
    assert [(e["call_id"], e["status"], e["after_seq"]) for e in events] == [
        ("c1", "ok", 1), ("c2", "error", 1),
    ]


# ---------------------------------------------------------------------------
# Project keying -- the truth table
# ---------------------------------------------------------------------------

def test_untitled_project_has_no_resolved_key(store):
    assert store.resolve_project_key("PID1", "") is None
    assert store.new_draft_key().startswith("draft:")
    assert store.new_draft_key() != store.new_draft_key()


def test_unusable_project_id_falls_back_to_a_path_bucket(store, project):
    assert store.resolve_project_key("", project) == store.path_key(project)
    assert store.resolve_project_key("T0", project) == store.path_key(project)


def test_new_project_gets_a_bucket_named_for_its_id(store, project):
    assert store.resolve_project_key("PID1", project) == "PID1"


def test_reopening_the_same_file_reuses_its_bucket(store, project):
    key = store.resolve_project_key("PID1", project)
    store.upsert_session("s1", key, project_path=project)
    assert store.resolve_project_key("PID1", project) == key


def test_renaming_a_project_keeps_its_history(store, project, tmp_path):
    key = store.resolve_project_key("PID1", project)
    store.upsert_session("s1", key, project_path=project)
    store.record_message("s1", "user", "hi")

    moved = str(tmp_path / "renamed.zvn")
    os.rename(project, moved)

    assert store.resolve_project_key("PID1", moved) == key
    assert len(store.load_messages("s1")) == 1


def test_save_as_forks_so_the_copy_inherits_but_then_diverges(store, project, tmp_path):
    key = store.resolve_project_key("PID1", project)
    store.upsert_session("s1", key, project_path=project, cli_session_id="cli-1")
    store.record_message("s1", "user", "shared history")

    copy = str(tmp_path / "copy.zvn")          # Save As: both files now exist
    open(copy, "w").write("{}")
    fork = store.resolve_project_key("PID1", copy)
    assert fork != key and fork.startswith("PID1:")

    # The live tab follows the user to the copy, ids and CLI continuity intact.
    live = store.load_sessions(fork)
    assert [s["session_id"] for s in live] == ["s1"]
    assert live[0]["cli_session_id"] == "cli-1"
    assert len(store.load_messages("s1")) == 1

    # The original keeps a frozen snapshot under a new id, with CLI ids cleared
    # so nothing can resume the same CLI conversation twice.
    snapshot = store.load_sessions(key)
    assert len(snapshot) == 1
    assert snapshot[0]["session_id"] != "s1"
    assert snapshot[0]["cli_session_id"] is None
    assert len(store.load_messages(snapshot[0]["session_id"])) == 1

    # New messages in the copy do not reach the original.
    store.record_message("s1", "user", "only in the copy")
    assert len(store.load_messages(snapshot[0]["session_id"])) == 1


def test_a_forked_project_survives_being_renamed(store, project, tmp_path):
    """The fork key must not be path-derived, or a rename would orphan it."""
    key = store.resolve_project_key("PID1", project)
    store.upsert_session("s1", key, project_path=project)
    copy = str(tmp_path / "copy.zvn")
    open(copy, "w").write("{}")
    fork = store.resolve_project_key("PID1", copy)

    moved = str(tmp_path / "copy-renamed.zvn")
    os.rename(copy, moved)
    assert store.resolve_project_key("PID1", moved) == fork


def test_a_churning_project_id_adopts_the_bucket_at_that_path(store, project):
    """Legacy "T0" projects are handed a fresh id on every open."""
    store.upsert_session("s1", "OLDID", project_path=project)
    store.record_message("s1", "user", "hi")

    assert store.resolve_project_key("NEWID", project) == "NEWID"
    assert [s["session_id"] for s in store.load_sessions("NEWID")] == ["s1"]
    assert len(store.load_messages("s1")) == 1
    # Stable from then on.
    assert store.resolve_project_key("NEWID", project) == "NEWID"


def test_rekey_project_moves_a_draft_bucket_onto_a_saved_project(store, project):
    draft = store.new_draft_key()
    store.upsert_session("s1", draft)
    store.record_message("s1", "user", "typed before saving")

    store.rekey_project(draft, "PID1", project)

    assert store.load_sessions(draft) == []
    rows = store.load_sessions("PID1")
    assert [s["session_id"] for s in rows] == ["s1"]
    assert rows[0]["project_path"] == os.path.abspath(project)
    assert len(store.load_messages("s1")) == 1


# ---------------------------------------------------------------------------
# Legacy import and concurrency
# ---------------------------------------------------------------------------

def test_legacy_sessions_are_imported_once(store, project):
    legacy = [
        {"session_id": "s1", "title": "Old chat", "backend": "codex", "agent_mode": "agent"},
        {"session_id": "s2", "title": "Another"},
    ]
    assert store.import_legacy_sessions("PID1", project, legacy) == 2
    # A bucket that already has rows is left alone.
    assert store.import_legacy_sessions("PID1", project, legacy) == 0
    rows = store.load_sessions("PID1")
    assert [r["title"] for r in rows] == ["Old chat", "Another"]
    assert rows[0]["backend"] == "codex"


def test_concurrent_writers_do_not_lose_or_corrupt_messages(store):
    store.upsert_session("s1", "P1")
    errors = []

    def writer(n):
        try:
            for i in range(25):
                store.record_message("s1", "user", "w%d-%d" % (n, i))
        except Exception as ex:  # pragma: no cover - failure path
            errors.append(ex)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    seqs = [m["seq"] for m in store.load_messages("s1")]
    assert seqs == list(range(1, 101))


def test_an_abandoned_empty_draft_is_discarded(store):
    draft = store.new_draft_key()
    store.upsert_session("s1", draft)
    assert store.discard_empty_bucket(draft) == 1
    assert store.load_sessions(draft) == []


def test_a_draft_that_was_actually_used_is_kept(store):
    draft = store.new_draft_key()
    store.upsert_session("s1", draft)
    store.record_message("s1", "user", "worth keeping")
    assert store.discard_empty_bucket(draft) == 0
    assert len(store.load_sessions(draft)) == 1
