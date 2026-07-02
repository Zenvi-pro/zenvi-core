"""Unit tests for the coalescing latest-wins recompute queue."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.recompute_queue import CoalescingRecomputeQueue  # noqa: E402


def test_latest_wins_commit_out_of_order():
    q = CoalescingRecomputeQueue()
    g1 = q.next_generation()
    g2 = q.next_generation()
    g3 = q.next_generation()
    assert (g1, g2, g3) == (1, 2, 3)

    # Newest result commits first.
    assert q.commit("r3", g3) is True
    # Older results arriving later are discarded (superseded).
    assert q.commit("r2", g2) is False
    assert q.commit("r1", g1) is False

    assert q.committed_generation == 3
    assert q.result == "r3"


def test_in_order_commit_keeps_only_newest():
    q = CoalescingRecomputeQueue()
    g1 = q.next_generation()
    g2 = q.next_generation()
    g3 = q.next_generation()

    # While g3 is the latest requested, earlier results are stale and dropped.
    assert q.commit("r1", g1) is False
    assert q.commit("r2", g2) is False
    assert q.commit("r3", g3) is True
    assert q.result == "r3"


def test_is_stale_tracks_latest_request():
    q = CoalescingRecomputeQueue()
    g1 = q.next_generation()
    assert q.is_stale(g1) is False
    g2 = q.next_generation()
    assert q.is_stale(g1) is True
    assert q.is_stale(g2) is False


def test_coalesces_to_newest_pending_payload():
    q = CoalescingRecomputeQueue()
    q.submit(q.next_generation(), "p1")
    q.submit(q.next_generation(), "p2")
    q.submit(q.next_generation(), "p3")

    # Only the newest pending payload survives coalescing.
    pending = q.take_pending()
    assert pending == (3, "p3")
    # And the queue is drained afterwards.
    assert q.take_pending() is None


def test_out_of_order_submit_keeps_highest_generation():
    q = CoalescingRecomputeQueue()
    q.submit(3, "p3")
    q.submit(1, "p1")  # late, lower generation must not overwrite the newer pending
    assert q.take_pending() == (3, "p3")


if __name__ == "__main__":
    test_latest_wins_commit_out_of_order()
    test_in_order_commit_keeps_only_newest()
    test_is_stale_tracks_latest_request()
    test_coalesces_to_newest_pending_payload()
    test_out_of_order_submit_keeps_highest_generation()
    print("test_recompute_queue: ok")
