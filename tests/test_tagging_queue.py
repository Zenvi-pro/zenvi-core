"""Unit tests for file tagging queue helpers (no OpenShot / PyQt import)."""
import unittest


class TaggingQueueLogic:
  """Minimal replica of FilesModel queue drain for headless tests."""

  _MAX_TAGGING_WORKERS = 2

  def __init__(self):
      self._active_taggers = []
      self._tagging_queue = []

  def is_file_tagging(self, file_id):
      fid = str(file_id or "")
      return any(str(w.get("id", "")) == fid for w in self._active_taggers)

  def _enqueue_tag(self, file_id, tag_only=False):
      fid = str(file_id or "")
      if not fid or self.is_file_tagging(fid):
          return
      if any(qid == fid for qid, _ in self._tagging_queue):
          return
      self._tagging_queue.append((fid, bool(tag_only)))
      self._drain_tagging_queue()

  def _drain_tagging_queue(self):
      while len(self._active_taggers) < self._MAX_TAGGING_WORKERS and self._tagging_queue:
          file_id, tag_only = self._tagging_queue.pop(0)
          if self.is_file_tagging(file_id):
              continue
          self._start_tagging_worker(file_id, tag_only)

  def _start_tagging_worker(self, file_id, tag_only=False):
      self._active_taggers.append({"id": str(file_id)})


class TaggingQueueTests(unittest.TestCase):
    def test_enqueue_dedupes_active_and_queued(self):
        model = TaggingQueueLogic()
        model._active_taggers = [{"id": "a"}]
        model._tagging_queue = [("a", False)]
        model._enqueue_tag("a", tag_only=False)
        self.assertEqual(model._tagging_queue, [("a", False)])

    def test_drain_respects_max_workers(self):
        model = TaggingQueueLogic()
        model._tagging_queue = [("1", False), ("2", False), ("3", False)]
        started = []

        def fake_start(fid, tag_only=False):
            started.append(fid)
            model._active_taggers.append({"id": str(fid)})

        model._start_tagging_worker = fake_start
        model._active_taggers = [{}, {}]
        model._drain_tagging_queue()
        self.assertEqual(started, [])

        model._active_taggers = [{}]
        model._drain_tagging_queue()
        self.assertEqual(started, ["1"])

        model._active_taggers.pop()
        model._drain_tagging_queue()
        self.assertEqual(started, ["1", "2"])
        self.assertEqual(model._tagging_queue, [("3", False)])


if __name__ == "__main__":
    unittest.main()
