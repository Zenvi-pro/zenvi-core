"""Planning tool guard tests (mirrors ai_chat_ui allowlist)."""
import unittest

_PLANNING_SAFE_TOOLS = frozenset({
    "get_project_info_tool", "list_files_tool", "list_clips_tool", "list_layers_tool",
    "get_timeline_state_tool", "list_markers_tool", "get_file_info_tool",
    "get_clips_with_full_metadata_tool", "get_timeline_placements_metadata_tool",
    "search_clip_scenes_tool", "search_clips_tool", "search_pexels_videos_tool",
    "search_freesound_music_tool", "list_transitions_tool", "search_transitions_tool",
    "save_edit_plan_tool", "update_edit_plan_step_tool", "finalize_edit_plan_tool",
    "save_edit_checkpoint_tool", "watch_clip_tool",
})


def is_planning_tool_allowed(tool_name: str) -> bool:
    if not tool_name:
        return False
    if tool_name in _PLANNING_SAFE_TOOLS:
        return True
    if tool_name.startswith("research_") or tool_name.startswith("web_search"):
        return True
    return False


class PlanningToolGuardTests(unittest.TestCase):
    def test_blocks_timeline_mutations(self):
        self.assertFalse(is_planning_tool_allowed("add_clip_to_timeline_tool"))

    def test_allows_plan_crud(self):
        self.assertTrue(is_planning_tool_allowed("update_edit_plan_step_tool"))

    def test_allows_search_clips(self):
        self.assertTrue(is_planning_tool_allowed("search_clips_tool"))


if __name__ == "__main__":
    unittest.main()
