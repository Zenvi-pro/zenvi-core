"""Tool blocks in chat must read like video editing, not like a coding agent.

The assistant now runs on an OpenCode harness, which brings its own tool names
into the transcript: ``task`` when the orchestrator hands work to a specialist,
and ``bash``/``edit``/``write`` when the motion-graphics agent works on
``session/draft.html`` in its sandbox. Those names reach ``humanize_tool_name``
like any other, and its fallback would title them "Task", "Bash" and "Edit" --
which is the coding runtime leaking into a video editor's chat.

The Zenvi tools themselves must keep their existing labels: the harness swap is
supposed to be invisible in the transcript.
"""

import unittest

from classes.tool_handlers import humanize_tool_name


class TestHarnessToolsReadAsProductWork(unittest.TestCase):
    def test_delegation_is_described_as_delegation(self):
        label = humanize_tool_name("task")
        self.assertNotEqual(label, "Task")
        self.assertIn("specialist", label.lower())

    def test_sandbox_file_work_is_described_as_motion_graphics_work(self):
        for name in ("bash", "edit", "write"):
            with self.subTest(tool=name):
                label = humanize_tool_name(name)
                self.assertNotEqual(label, name.capitalize())
                self.assertIn("motion graphic", label.lower())

    def test_sandbox_reads_are_labelled(self):
        for name in ("read", "glob", "grep"):
            with self.subTest(tool=name):
                self.assertNotEqual(humanize_tool_name(name), name.capitalize())

    def test_question_and_todo_are_labelled(self):
        self.assertNotEqual(humanize_tool_name("question"), "Question")
        self.assertNotEqual(humanize_tool_name("todowrite"), "Todowrite")

    def test_no_harness_label_is_empty(self):
        for name in ("task", "bash", "edit", "write", "read", "glob", "grep",
                     "question", "todowrite"):
            with self.subTest(tool=name):
                self.assertTrue(humanize_tool_name(name).strip())


class TestZenviLabelsAreUnchanged(unittest.TestCase):
    """The harness swap must not rename anything the user already knows."""

    def test_workflow_labels_survive(self):
        self.assertEqual(humanize_tool_name("video_gen"), _expected("video_gen"))
        self.assertEqual(humanize_tool_name("stock_video"), _expected("stock_video"))

    def test_motion_graphics_labels_survive(self):
        self.assertEqual(
            humanize_tool_name("publish_session_draft_tool"), "Publish motion graphic"
        )
        self.assertEqual(humanize_tool_name("lint_session_draft_tool"), "Lint draft")

    def test_placement_tool_still_has_a_label(self):
        label = humanize_tool_name("place_motion_graphic_tool")
        self.assertTrue(label.strip())
        self.assertNotIn("_", label)

    def test_unknown_zenvi_tools_still_fall_back_readably(self):
        self.assertEqual(humanize_tool_name("some_new_thing_tool"), "Some new thing")


def _expected(name: str) -> str:
    """Whatever the shipped label table says -- this test pins stability, not text."""
    from classes.tool_handlers import (
        TOOL_DISPLAY_LABELS,
        _EXTRA_TOOL_DISPLAY_LABELS,
    )

    if name in TOOL_DISPLAY_LABELS:
        return TOOL_DISPLAY_LABELS[name]
    if name in _EXTRA_TOOL_DISPLAY_LABELS:
        return _EXTRA_TOOL_DISPLAY_LABELS[name]
    base = name[:-5] if name.endswith("_tool") else name
    return base.replace("_", " ").strip().capitalize()


if __name__ == "__main__":
    unittest.main()
