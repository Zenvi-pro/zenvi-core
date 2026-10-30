"""classes.handoff.remotion.trust: the project's code runs only after the user said so (or their agent asked).

Zenvi's menus, dialogs and the toolbar pill run their work on the handoff.jobs
executors; a re-render or Open in Studio started there asks once per project
folder per session. Agent tools run their work on their own thread and keep
their documented behaviour (no question).
"""

import os
import threading

import pytest

from classes.handoff import alpha, jobs
from classes.handoff import linked_media as lm
from classes.handoff.remotion import detect, helper, provider, studio, trust
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)
from remotion_fakes import FakeHelper, make_project


@pytest.fixture(autouse=True)
def fresh_trust():
    trust.reset()
    yield
    trust.reset()
    trust.set_asker(None)


@pytest.fixture
def asked(monkeypatch):
    """The trust question, answered by the test: ``asked.answer`` (True / False); calls in ``asked.calls``."""
    class Asked:
        answer = False
        calls = []

    def ask(root, name, action, key):
        Asked.calls.append((name, action, threading.current_thread().name))
        return Asked.answer

    Asked.calls = []
    trust.set_asker(ask)
    return Asked


def on_executor(fn, name="handoff_0"):
    """Run *fn* on a thread named like handoff.jobs' executor (what Zenvi's menus and dialogs use)."""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001  (re-raised below)
            box["error"] = exc

    t = threading.Thread(target=run, name=name)
    t.start()
    t.join(60)
    if "error" in box:
        raise box["error"]
    return box.get("result")


@pytest.fixture
def project(tmp_path):
    return detect.inspect_project(make_project(str(tmp_path / "promo")))


@pytest.fixture
def fake_helper(monkeypatch):
    fake = FakeHelper()
    monkeypatch.setattr(helper, "run_helper", fake)
    monkeypatch.setattr(helper, "prune_bundles", lambda *a, **k: [])
    monkeypatch.setattr(alpha, "has_transparency", lambda p: False)
    return fake


def _link(project, comp="Scene"):
    return lm.read_link({"zenvi_link": lm.normalize_link(provider.make_link(project, comp))})


def test_only_work_started_from_zenvis_menus_is_gated(asked):
    assert on_executor(trust.ui_initiated) and on_executor(trust.ui_initiated, "handoff-ui_1")
    assert not trust.ui_initiated()                                     # a tool call's thread
    trust.require("/nowhere", "x", action="render")                    # not gated: no question
    assert asked.calls == []


def test_a_re_render_from_the_menus_asks_once_per_folder_and_a_no_runs_nothing(project, fake_helper, asked, tmp_path):
    p = provider.RemotionProvider()

    def render():
        return p.render(_link(project), str(tmp_path), on_progress=lambda f, m: None, should_cancel=lambda: False)

    with pytest.raises(jobs.JobCancelled, match="you chose not to run its code"):
        on_executor(render)
    assert fake_helper.calls == [] and asked.calls == [("fake-remotion", "render", "handoff_0")]
    asked.answer = True                                                # the next click asks again
    result = on_executor(render)
    assert len(asked.calls) == 2 and result.codec == "h264" and fake_helper.commands() == ["still", "render"]
    on_executor(render)                                                # remembered for the session
    assert len(asked.calls) == 2
    assert trust.is_trusted(trust.trust_key(project.root))


def test_open_in_studio_from_the_menu_asks_first(project, asked, monkeypatch):
    opened = []
    monkeypatch.setattr(studio, "open_studio", lambda proj, comp=None, **k: opened.append(comp))
    with pytest.raises(trust.NotTrusted):
        on_executor(lambda: provider.RemotionProvider().open_studio(_link(project, "TitleCard")), "handoff-ui_0")
    assert opened == [] and asked.calls[0][1] == "studio"
    asked.answer = True
    on_executor(lambda: provider.RemotionProvider().open_studio(_link(project, "TitleCard")), "handoff-ui_0")
    assert opened == ["TitleCard"] and len(asked.calls) == 2


def test_no_answer_in_time_counts_as_no(project, fake_helper, tmp_path):
    def slow(root, name, action, key):
        raise TimeoutError("GUI-thread call did not finish")

    trust.set_asker(slow)
    with pytest.raises(trust.NotTrusted):
        on_executor(lambda: provider.RemotionProvider().render(_link(project), str(tmp_path),
                                                               on_progress=lambda f, m: None,
                                                               should_cancel=lambda: False))
    assert fake_helper.calls == []


def test_the_linked_clip_tools_keep_running_without_a_question(linked, project, fake_helper, asked):  # noqa: F811
    """rerender_linked_clip_tool & co run on the tool's own thread: documented behaviour, no prompt."""
    lm.register_provider(provider.RemotionProvider())
    path, completed = lm.render_link(provider.make_link(project, "Scene"))
    receipt = lm.add_linked_media(path, {k: v for k, v in completed.items() if k != "warnings"}, position=0.0)
    out = linked.call("rerender_linked_clip_tool", file_id=receipt["file_id"])
    assert not out.startswith("Error"), out
    assert asked.calls == [] and fake_helper.commands().count("render") == 2


def test_c1s_menu_re_render_path_asks_for_an_untrusted_folder(linked, project, fake_helper, asked):  # noqa: F811
    """Linked Source > Re-render / the stale pill: windows.handoff_menus.rerender_files -> rerender_many on the
    handoff executor; a received project (nothing imported this session) asks first, a "no" changes nothing."""
    lm.register_provider(provider.RemotionProvider())
    path, completed = lm.render_link(provider.make_link(project, "Scene"))  # (a tool-style render: no question)
    receipt = lm.add_linked_media(path, {k: v for k, v in completed.items() if k != "warnings"}, position=0.0)
    linked.mark()
    before = linked.file(receipt["file_id"])["path"]
    with pytest.raises(jobs.JobCancelled):
        on_executor(lambda: lm.rerender_many([receipt["file_id"]]))
    assert asked.calls and asked.calls[0][1] == "render"
    assert linked.file(receipt["file_id"])["path"] == before and linked.undo_steps_since_mark() == 0
