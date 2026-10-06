"""launch.py's command line (classes.launch_args): --headless / --project."""

import pytest

from classes import launch_args


def _parse(*argv):
    return launch_args.parse(list(argv), "1.0.0")


def test_plain_launch_keeps_file_arguments():
    args, extra = _parse("cut.zvn")
    assert args.headless is False and args.project is None
    assert args.remain == ["cut.zvn"]
    assert extra == []


def test_headless_with_a_project():
    args, _extra = _parse("--headless", "--project", "/Movies/cut.zvn")
    assert args.headless is True
    assert args.project == "/Movies/cut.zvn"
    assert args.remain == []


def test_headless_without_a_project_is_an_untitled_session():
    args, _extra = _parse("--headless")
    assert args.headless is True and args.project is None


def test_project_flag_works_for_the_window_too():
    args, _extra = _parse("--project", "cut.zvn")
    assert args.headless is False and args.project == "cut.zvn"


@pytest.mark.parametrize("argv", [
    ("--headless", "cut.zvn"),              # headless takes --project, not a file
    ("cut.zvn", "--headless"),              # would be swallowed as a file name
    ("--headless", "--project"),            # --project needs a value
])
def test_unusable_command_lines_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        _parse(*argv)
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_existing_flags_still_parse():
    args, extra = _parse("-d", "--web-backend", "qwidget", "-V", "-style", "fusion")
    assert args.debug and args.version
    assert args.web_backend == "qwidget"
    # Unknown options are left for Qt, as before.
    assert extra == ["-style"]
    assert args.remain == ["fusion"]


def test_help_mentions_the_headless_contract(capsys):
    with pytest.raises(SystemExit):
        _parse("--help")
    out = capsys.readouterr().out
    assert "--headless" in out and "headless_mcp.json" in out and "shutdown_headless_tool" in out
    assert "--project PATH" in out


@pytest.mark.parametrize("argv", [
    ("clip.mp4", "--project", "cut.zvn"),
    ("clip.mp4", "--project=cut.zvn"),
])
def test_project_after_a_media_path_is_still_the_project(argv):
    """Review #216: REMAINDER swallowed a --project that followed a file name."""
    args, _extra = _parse(*argv)
    assert args.project == "cut.zvn"
    assert args.remain == ["clip.mp4"]
