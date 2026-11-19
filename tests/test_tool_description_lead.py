"""Confusable desktop tools lead with a "Use for" sentence that names the words users type.

find_tools matches the user's words against each description, and the first sentence is what
the model reads to tell near-twin tools apart. Each pair below is one the benchmark missed.
"""

import pytest


@pytest.mark.parametrize("name, user_word", [
    ("add_title_tool", "lower third"),
    ("add_animated_title_tool", "three-dimensional"),
    ("duck_under_speech_tool", "dialogue"),
    ("list_transitions_tool", "wipe"),
    ("set_clip_volume_tool", "quieter"),
    ("build_reel_tool", "promo"),
])
def test_confusable_tool_leads_with_use_for_and_the_users_word(name, user_word):
    from classes import tool_handlers  # noqa: F401  (merges the editor-tool registry)
    from classes.agent_mcp_server import iter_tool_defs

    desc = next(d["description"] for d in iter_tool_defs() if d["name"] == name)
    assert desc.startswith("Use for "), desc[:80]
    assert user_word in desc.lower()
