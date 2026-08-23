"""
 @file
 @brief Coalescing, latest-wins recompute scheduler (pure logic).

 Used to decouple expensive UI recomputes (e.g. the timeline overview/minimap
 geometry) from the events that trigger them. Rapid events coalesce so only the
 most recently requested work matters, and results produced for superseded
 generations are discarded. This guarantees the consumer always converges to the
 current state and can never display a stale ("previous drag") result.

 The class contains no Qt/threading-loop wiring so it can be unit tested
 headlessly; the owning widget is responsible for running the worker and
 delivering results back on the GUI thread.
"""

import threading


class CoalescingRecomputeQueue:
    """Track recompute generations and enforce latest-wins result commits."""

    def __init__(self):
        self._lock = threading.RLock()
        self._latest_generation = 0  # highest generation requested so far
        self._committed_generation = 0  # highest generation whose result was kept
        self._pending = None  # (generation, payload) waiting to be processed
        self._result = None  # most recently committed result

    def next_generation(self):
        """Reserve and return a new, strictly increasing generation id."""
        with self._lock:
            self._latest_generation += 1
            return self._latest_generation

    def submit(self, generation, payload):
        """Queue work for a generation, coalescing to the newest pending payload."""
        with self._lock:
            if generation > self._latest_generation:
                self._latest_generation = generation
            # Coalesce: only the newest pending payload survives.
            if self._pending is None or generation >= self._pending[0]:
                self._pending = (generation, payload)
            return self._latest_generation

    def take_pending(self):
        """Pop the pending (generation, payload) for processing, or None."""
        with self._lock:
            pending = self._pending
            self._pending = None
            return pending

    def is_stale(self, generation):
        """A generation is stale once a newer one has been requested."""
        with self._lock:
            return generation < self._latest_generation

    def commit(self, result, generation):
        """Commit a computed result. Returns True if kept, False if discarded.

        Only the result for the most recently requested generation is kept;
        anything older is a superseded (stale) computation and is dropped.
        """
        with self._lock:
            if generation < self._latest_generation or generation <= self._committed_generation:
                return False
            self._committed_generation = generation
            self._result = result
            return True

    @property
    def latest_generation(self):
        with self._lock:
            return self._latest_generation

    @property
    def committed_generation(self):
        with self._lock:
            return self._committed_generation

    @property
    def result(self):
        with self._lock:
            return self._result
