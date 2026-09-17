"""Planning tool guard tests — import the real allowlist from ai_chat_ui."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from PyQt5.QtWidgets import QApplication  # noqa: F401
except ImportError:
    QApplication = None

if QApplication is not None:
    from windows.ai_chat_ui import _is_planning_tool_allowed as is_planning_tool_allowed
else:
    is_planning_tool_allowed = None


@unittest.skipIf(is_planning_tool_allowed is None, "PyQt5 required")
class PlanningToolGuardTests(unittest.TestCase):
    def test_blocks_timeline_mutations(self):
        self.assertFalse(is_planning_tool_allowed("add_clip_to_timeline_tool"))

    def test_allows_research_brief_tool(self):
        self.assertTrue(is_planning_tool_allowed("save_planning_research_brief_tool"))

    def test_allows_plan_crud(self):
        self.assertTrue(is_planning_tool_allowed("update_edit_plan_step_tool"))

    def test_allows_search_clips(self):
        self.assertTrue(is_planning_tool_allowed("search_clips_tool"))


if __name__ == "__main__":
    unittest.main()
