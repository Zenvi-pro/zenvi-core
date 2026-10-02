"""Flag arguments the handlers parse from 'true'/'false' also accept a real JSON boolean."""

from __future__ import annotations

import pytest

from classes.agent_tools.schema import validate_args


@pytest.mark.parametrize("tool,arg", [
    ("import_files_tool", "skip_indexing"),
    ("export_video_tool", "show_dialog"),
    ("add_clip_to_timeline_tool", "full_file"),
    ("duck_under_speech_tool", "dry_run"),
])
@pytest.mark.parametrize("value", [True, False, "true", "false"])
def test_flag_accepts_bool_and_string(tool, arg, value):
    assert validate_args(tool, {arg: value}) is None


def test_non_flag_strings_still_reject_bool():
    assert validate_args("import_files_tool", {"paths": True}) is not None
