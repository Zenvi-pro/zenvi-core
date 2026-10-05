"""classes.handoff.jobs: the handoff executor and its running-job registry."""

import threading

import pytest

from classes.handoff import jobs


def test_submit_job_runs_off_the_caller_and_reports_on_done():
    done = []
    caller = threading.current_thread().name

    def work(job):
        job.report(0.5, "half")
        return threading.current_thread().name

    job = jobs.submit_job(work, label="probe", key="k-done", on_done=done.append)
    result = job.wait(10)
    assert result.startswith("handoff") and result != caller
    assert job.state == jobs.DONE and job.progress == 0.5 and job.message == "half"
    assert done == [job] and jobs.job_for("k-done") is None


def test_failed_job_keeps_its_error():
    def work(job):
        raise RuntimeError("render broke")

    job = jobs.submit_job(work, label="fails")
    with pytest.raises(RuntimeError, match="render broke"):
        job.wait(10)
    assert job.state == jobs.FAILED and job.snapshot()["error"] == "render broke"


def test_cancel_is_cooperative_and_registered_jobs_are_listed():
    started, release = threading.Event(), threading.Event()

    def work(job):
        started.set()
        while not job.should_cancel():
            release.wait(0.01)
        job.raise_if_cancelled()

    job = jobs.submit_job(work, label="long", key="file-1", kind="remotion")
    assert started.wait(10)
    assert jobs.job_for("file-1") is job and job in jobs.running_jobs(kind="remotion")
    assert jobs.cancel_job(job.id) is True
    with pytest.raises(jobs.JobCancelled):
        job.wait(10)
    assert job.state == jobs.CANCELLED and jobs.get_job(job.id) is None
    assert job.cancel() is False


def test_track_job_registers_blocking_work_and_records_failure():
    with jobs.track_job("inline", key="file-2") as job:
        assert jobs.job_for("file-2") is job and job.state == jobs.RUNNING
        job.report(1.0, "done")
    assert job.state == jobs.DONE and jobs.job_for("file-2") is None
    with pytest.raises(ValueError):
        with jobs.track_job("inline-fail", key="file-3") as failing:
            raise ValueError("nope")
    assert failing.state == jobs.FAILED and jobs.job_for("file-3") is None


def test_listeners_hear_start_and_finish():
    events = []

    def listener(job):
        events.append((job.label, job.state))

    jobs.add_listener(listener)
    try:
        with jobs.track_job("heard"):
            pass
    finally:
        jobs.remove_listener(listener)
    assert ("heard", jobs.RUNNING) in events and ("heard", jobs.DONE) in events


def test_shutdown_cancels_running_and_queued_jobs_and_silences_callbacks(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(jobs, "EXECUTOR", ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff-test"))
    monkeypatch.setattr(jobs, "CHECK_EXECUTOR", ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff-tc"))
    monkeypatch.setattr(jobs, "_shutting_down", False)
    started, done_calls = threading.Event(), []

    def long(job):
        started.set()
        while not job.should_cancel():
            time.sleep(0.01)
        job.raise_if_cancelled()

    running = jobs.submit_job(long, label="running", on_done=done_calls.append)
    assert started.wait(5)
    queued = jobs.submit_job(lambda job: "never", label="queued", on_done=done_calls.append)
    t0 = time.monotonic()
    jobs.shutdown()
    with pytest.raises(jobs.JobCancelled):
        running.wait(5)
    assert queued.state == jobs.CANCELLED and running.state == jobs.CANCELLED
    assert done_calls == []  # no GUI callbacks once Zenvi is quitting
    after = jobs.submit_job(lambda job: 1, label="after quit")
    assert after.state == jobs.CANCELLED and time.monotonic() - t0 < 5
