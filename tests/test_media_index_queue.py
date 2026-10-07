"""Which queued file indexes next, and which files wait only for a sign-in."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index.queue import next_index, signin_skipped_ids, timeline_file_ids  # noqa: E402


def test_a_file_on_the_timeline_goes_before_older_imports():
    queue = [("a", False), ("b", False), ("c", False)]
    assert next_index(queue, {"c"}) == 2


def test_the_oldest_timeline_file_goes_first_among_timeline_files():
    queue = [("a", False), ("b", False), ("c", False)]
    assert next_index(queue, {"b", "c"}) == 1


def test_with_nothing_on_the_timeline_it_is_first_in_first_out():
    assert next_index([("a", False), ("b", False)], set()) == 0


def test_an_empty_queue_is_safe():
    assert next_index([], {"a"}) == 0


def test_draining_a_queue_never_starves_or_repeats_a_file():
    queue = [(str(i), False) for i in range(6)]
    used = {"4", "2"}
    order = []
    while queue:
        order.append(queue.pop(next_index(queue, used))[0])
    assert order == ["2", "4", "0", "1", "3", "5"]
    assert sorted(order) == [str(i) for i in range(6)]


def test_timeline_file_ids_ignore_clips_without_a_file():
    clips = [{"file_id": "a"}, {"file_id": ""}, {"id": "x"}, None, "junk", {"file_id": 7}]
    assert timeline_file_ids(clips) == {"a", "7"}
    assert timeline_file_ids(None) == set()


def test_only_files_skipped_for_sign_in_are_requeued():
    files = [
        {"id": "a", "ai_metadata": {"skip_code": "signin", "skip_reason": "Sign in"}},
        {"id": "b", "ai_metadata": {"skip_reason": "Clip exceeds the 30-minute limit."}},
        {"id": "c", "ai_metadata": {"analyzed": True}},
        {"id": "d"},
        {"ai_metadata": {"skip_code": "signin"}},
        None,
        {"id": "e", "ai_metadata": {"skip_code": "signin"}},
    ]
    assert signin_skipped_ids(files) == ["a", "e"]
    assert signin_skipped_ids(None) == []
