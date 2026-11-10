"""Startup must not stop on a dialog per bad thumbnail, and a launch that gives up must say so."""

import ast
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from classes import emoji_catalog, info
from windows.models import emoji_model, transition_model

_LAUNCH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src", "launch.py"))


@pytest.fixture
def app(monkeypatch):
    """The app the models read translations from; no window exists yet at startup."""
    fake = SimpleNamespace(_tr=lambda text: text)
    monkeypatch.setattr(emoji_model, "get_app", lambda: fake)
    monkeypatch.setattr(transition_model, "get_app", lambda: fake)
    return fake


def _libopenshot_that_cannot_read(*bad_names):
    """Clip() raises for the named files, as libopenshot does for a corrupt image."""
    def clip(path):
        if os.path.basename(path) in bad_names:
            raise RuntimeError("cannot read " + os.path.basename(path))
        return MagicMock()
    return SimpleNamespace(Clip=clip)


def _bare(cls, **attrs):
    """Only the attributes update_model reads: the real constructor builds Qt models the stub cannot."""
    obj = cls.__new__(cls)
    obj.__dict__.update(attrs)
    return obj


def _launch_tree():
    with open(_LAUNCH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _launch_function(name):
    """A top-level def from src/launch.py, run on its own: importing launch.py starts the app."""
    defs = [node for node in _launch_tree().body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert defs, "src/launch.py has no top-level def %s()" % name
    namespace = {"sys": sys, "logger": MagicMock()}
    exec(compile(ast.Module(body=defs, type_ignores=[]), _LAUNCH, "exec"), namespace)
    return namespace[name], namespace["logger"]


def test_a_bad_emoji_thumbnail_is_logged_and_skipped_without_a_dialog(tmp_path, monkeypatch, app):
    good, bad = tmp_path / "1F600.svg", tmp_path / "1F4A9.svg"
    entries = [emoji_catalog.EmojiEntry(code=p.stem, path=str(p), name=p.stem, group="smileys-emotion",
                                        group_name="Smileys") for p in (bad, good)]
    monkeypatch.setattr(emoji_catalog, "entries", lambda: entries)
    monkeypatch.setattr(info, "IMAGES_PATH", str(tmp_path / "images"))
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(emoji_model, "openshot", _libopenshot_that_cannot_read("1F4A9.svg"))
    log, dialog = MagicMock(), MagicMock()
    monkeypatch.setattr(emoji_model, "log", log)
    monkeypatch.setattr(emoji_model, "QMessageBox", dialog, raising=False)

    model = _bare(emoji_model.EmojisModel, model=MagicMock(), model_paths={}, emoji_groups=[])
    model.update_model()

    assert not dialog.called
    assert any(str(bad) in str(call) for call in log.warning.call_args_list)
    assert model.model.appendRow.call_count == 1
    assert list(model.model_paths) == [str(good)]


def test_a_bad_transition_thumbnail_is_logged_and_skipped_without_a_dialog(tmp_path, monkeypatch, app):
    common, extra = tmp_path / "transitions" / "common", tmp_path / "transitions" / "extra"
    common.mkdir(parents=True)
    extra.mkdir()
    for path in (common / "fade_in.png", common / "corrupt.png", extra / "zoom.png"):
        path.touch()
    monkeypatch.setattr(info, "PATH", str(tmp_path))
    monkeypatch.setattr(info, "TRANSITIONS_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(info, "IMAGES_PATH", str(tmp_path / "images"))
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(transition_model, "openshot", _libopenshot_that_cannot_read("corrupt.png"))
    log, dialog = MagicMock(), MagicMock()
    monkeypatch.setattr(transition_model, "log", log)
    monkeypatch.setattr(transition_model, "QMessageBox", dialog, raising=False)

    model = _bare(transition_model.TransitionsModel, model=MagicMock(), model_paths={}, app=app)
    model.update_model()

    assert not dialog.called
    assert any("corrupt.png" in str(call) for call in log.warning.call_args_list)
    assert set(model.model_paths) == {str(common / "fade_in.png"), str(extra / "zoom.png")}


def test_a_second_launch_the_running_zenvi_never_answers_says_so_and_exits_1():
    give_up, logger = _launch_function("_exit_unanswered_handoff")

    with pytest.raises(SystemExit) as exited:
        give_up("no-reply", "no answer within 5 s")

    assert exited.value.code == 1
    assert logger.error.call_count == 1
    message = logger.error.call_args.args[0] % logger.error.call_args.args[1:]
    assert "already running" in message and "no answer within 5 s" in message


def test_the_give_up_branch_of_main_goes_through_that_function():
    main = next(node for node in _launch_tree().body if isinstance(node, ast.FunctionDef) and node.name == "main")
    called = {node.func.id for node in ast.walk(main)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_exit_unanswered_handoff" in called


@pytest.mark.parametrize("notice_fails", [False, True])
def test_the_give_up_shows_a_notice_and_exits_1_even_if_the_notice_fails(monkeypatch, notice_fails):
    from classes import update_installer

    shown = []

    def notice(system, version, text=None):
        shown.append(text)
        if notice_fails:
            raise OSError("no notification service")

    monkeypatch.setattr(update_installer, "_show_update_notice", notice)
    give_up, _logger = _launch_function("_exit_unanswered_handoff")

    with pytest.raises(SystemExit) as exited:
        give_up("no-reply", "no answer within 5 s")

    assert exited.value.code == 1
    assert shown == ["Zenvi is already starting. If no window appears, end zenvi.exe in Task Manager and try again."]


def test_an_update_notice_keeps_its_own_text_unless_one_is_given(monkeypatch):
    from classes import update_installer

    sent = []
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda cmd, **kw: sent.append(cmd))
    update_installer._show_update_notice("linux", "1.2.3")
    update_installer._show_update_notice("linux", "", text="Zenvi is already starting.")
    assert [cmd[-1] for cmd in sent] == ["Updating to version 1.2.3…", "Zenvi is already starting."]
