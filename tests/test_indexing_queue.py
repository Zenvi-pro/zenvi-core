"""Unit tests for file indexing queue helpers (no OpenShot / PyQt import)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from classes.indexing_status import PENDING, derive_indexing_status  # noqa: E402


class IndexingQueueLogic:
  """Minimal replica of FilesModel queue drain for headless tests."""

  _MAX_INDEXING_WORKERS = 2

  def __init__(self):
      self._active_indexers = []
      self._indexing_queue = []

  def is_file_indexing(self, file_id):
      fid = str(file_id or "")
      return any(str(w.get("id", "")) == fid for w in self._active_indexers)

  def _enqueue_index(self, file_id, summarize_only=False):
      fid = str(file_id or "")
      if not fid or self.is_file_indexing(fid):
          return
      if any(qid == fid for qid, _ in self._indexing_queue):
          return
      self._indexing_queue.append((fid, bool(summarize_only)))
      self._drain_indexing_queue()

  def _drain_indexing_queue(self):
      while len(self._active_indexers) < self._MAX_INDEXING_WORKERS and self._indexing_queue:
          file_id, summarize_only = self._indexing_queue.pop(0)
          if self.is_file_indexing(file_id):
              continue
          self._start_indexing_worker(file_id, summarize_only)

  def is_file_queued(self, file_id):
      fid = str(file_id or "")
      return any(qid == fid for qid, _ in self._indexing_queue)

  def has_active_indexing(self):
      return bool(self._active_indexers or self._indexing_queue)

  def _start_indexing_worker(self, file_id, summarize_only=False):
      self._active_indexers.append({"id": str(file_id)})


class IndexingQueueTests(unittest.TestCase):
    def test_enqueue_dedupes_active_and_queued(self):
        model = IndexingQueueLogic()
        model._active_indexers = [{"id": "a"}]
        model._indexing_queue = [("a", False)]
        model._enqueue_index("a", summarize_only=False)
        self.assertEqual(model._indexing_queue, [("a", False)])

    def test_drain_respects_max_workers(self):
        model = IndexingQueueLogic()
        model._indexing_queue = [("1", False), ("2", False), ("3", False)]
        started = []

        def fake_start(fid, summarize_only=False):
            started.append(fid)
            model._active_indexers.append({"id": str(fid)})

        model._start_indexing_worker = fake_start
        model._active_indexers = [{}, {}]
        model._drain_indexing_queue()
        self.assertEqual(started, [])

        model._active_indexers = [{}]
        model._drain_indexing_queue()
        self.assertEqual(started, ["1"])

        model._active_indexers.pop()
        model._drain_indexing_queue()
        self.assertEqual(started, ["1", "2"])
        self.assertEqual(model._indexing_queue, [("3", False)])


class QueuedBadgeStatusTests(unittest.TestCase):
    """A file waiting for a worker slot must not read as "not yet analyzed"."""

    def _full_model(self):
        model = IndexingQueueLogic()
        model._enqueue_index("a")
        model._enqueue_index("b")
        model._enqueue_index("c")  # both slots busy -> stays queued
        return model

    def test_queued_file_reports_pending_badge(self):
        model = self._full_model()
        self.assertEqual(model._indexing_queue, [("c", False)])
        status = derive_indexing_status(
            {},
            is_active=model.is_file_indexing("c"),
            is_queued=model.is_file_queued("c"),
        )
        self.assertEqual(status.state, PENDING)

    def test_aggregate_stays_active_while_queue_has_work(self):
        model = self._full_model()
        model._active_indexers.clear()  # workers finished, drain not run yet
        self.assertTrue(model.has_active_indexing())

    def test_aggregate_clears_when_nothing_is_running_or_queued(self):
        model = IndexingQueueLogic()
        self.assertFalse(model.has_active_indexing())


if __name__ == "__main__":
    unittest.main()
