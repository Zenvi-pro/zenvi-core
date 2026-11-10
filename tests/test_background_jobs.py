"""Slow tools run in the background: the call answers with a note, and the result waits for the next turn."""

import threading

import pytest

from classes import background_jobs

TOOL = "generate_video_and_add_to_timeline_tool"


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    monkeypatch.setattr(background_jobs, "GRACE_SECONDS", 0.05)
    background_jobs.reset()
    yield
    background_jobs.reset()


def _settle():
    for thread in threading.enumerate():
        if thread.name == "zenvi-background-job":
            thread.join(2)


def test_a_tool_outside_the_list_runs_on_the_calling_thread_and_its_errors_propagate():
    caller = threading.current_thread()
    ran_on = []

    def quick():
        ran_on.append(threading.current_thread())
        return "ok"

    def broken():
        raise ValueError("bad clip")

    assert background_jobs.run("quick_tool", {}, quick) == "ok"
    assert ran_on == [caller]
    with pytest.raises(ValueError, match="bad clip"):
        background_jobs.run("quick_tool", {}, broken)


def test_a_background_tool_that_finishes_in_time_returns_its_result_and_leaves_no_note():
    assert background_jobs.run(TOOL, {"prompt": "a cat"}, lambda: "receipt-json") == "receipt-json"
    assert background_jobs.turn_notes() == ""


def test_a_slow_background_tool_returns_a_note_and_its_result_is_reported_once():
    release = threading.Event()

    def slow():
        release.wait(5)
        return "Clip placed"

    note = background_jobs.run(TOOL, {"prompt": "a cat"}, slow)
    assert note.startswith(background_jobs.BACKGROUND_PREFIX)
    assert "after 0.05 s" in note
    assert TOOL not in note
    assert "still running" in background_jobs.turn_notes()

    release.set()
    _settle()
    assert background_jobs.turn_notes() == (
        '[Background jobs]\n- AI video "a cat": finished: Clip placed\n[/Background jobs]'
    )
    assert background_jobs.turn_notes() == ""


def test_repeating_a_running_call_waits_for_it_instead_of_starting_another():
    release = threading.Event()
    runs = []

    def slow():
        runs.append("cat")
        release.wait(5)
        return "Clip placed"

    def other():
        runs.append("dog")
        return "Dog placed"

    assert background_jobs.run(TOOL, {"prompt": "a cat"}, slow).startswith(background_jobs.BACKGROUND_PREFIX)
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(background_jobs.run(TOOL, {"prompt": "a cat"}, slow)))
    waiter.start()
    release.set()
    waiter.join(2)
    assert answers == ["Clip placed"]
    assert background_jobs.run(TOOL, {"prompt": "a dog"}, other) == "Dog placed"
    assert runs == ["cat", "dog"]


def test_repeating_a_call_after_its_detached_job_finished_returns_the_stored_result():
    release = threading.Event()
    runs = []

    def slow():
        runs.append(1)
        release.wait(5)
        return "Clip placed"

    background_jobs.run(TOOL, {"prompt": "a cat"}, slow)
    release.set()
    _settle()
    assert background_jobs.run(TOOL, {"prompt": "a cat"}, slow) == "Clip placed"
    assert runs == [1]
    assert background_jobs.turn_notes() == ""


def test_a_detached_tool_that_raises_is_reported_as_an_error_on_the_next_turn():
    release = threading.Event()

    def failing():
        release.wait(5)
        raise RuntimeError("export engine crashed")

    background_jobs.run(TOOL, {"prompt": "a cat"}, failing)
    release.set()
    _settle()
    assert "finished: Error: tool execution failed: export engine crashed" in background_jobs.turn_notes()
