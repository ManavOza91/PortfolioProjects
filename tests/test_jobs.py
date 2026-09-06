"""Running the long jobs from the app instead of a terminal.

Nothing here touches the network or runs a real sweep — the point is the job
machinery: does a button start work, is a second one refused, does a crash stay
contained, and does the page report honestly while it runs.
"""

from __future__ import annotations

import threading
import time

import pytest

from signal_engine import jobs


@pytest.fixture(autouse=True)
def _clean_registry():
    jobs.reset()
    yield
    jobs.reset()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# The buttons come from config
# ---------------------------------------------------------------------------


def test_the_buttons_are_configuration_not_code():
    """A customer selling something else relabels these; they don't edit Python."""
    keys = [j.key for j in jobs.all_jobs()]
    assert "detect" in keys
    assert "directory-eu" in keys
    assert "directory-us" in keys


def test_eu_and_us_directory_sweeps_are_separate_buttons():
    """Asked for explicitly: EU/UK and US refresh independently."""
    specs = {str(s["key"]): s for s in jobs.job_specs()}
    assert specs["directory-eu"]["sources"] == ["eudamed", "mhra"]
    assert specs["directory-us"]["sources"] == ["openfda"]


def test_an_unknown_job_is_refused_by_name():
    with pytest.raises(jobs.JobRefused):
        jobs.start("not-a-job")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def test_a_job_runs_in_the_background_and_keeps_its_result():
    release = threading.Event()

    def _slow(job, spec):
        release.wait(timeout=5)
        return "swept everything"

    job = jobs.start("detect", runner=_slow)
    assert job.running, "start() must return immediately, not block the request"

    release.set()
    assert _wait_for(lambda: jobs.get_job("detect").status == "done")

    finished = jobs.get_job("detect")
    assert finished.result == "swept everything"
    assert finished.error is None
    assert finished.elapsed_seconds is not None


def test_a_second_job_is_refused_while_one_is_running():
    """They share a SQLite file. Queuing is simpler than concurrent writes."""
    release = threading.Event()
    jobs.start("directory-eu", runner=lambda j, s: release.wait(timeout=5) or "done")

    with pytest.raises(jobs.JobRefused) as refusal:
        jobs.start("directory-us", runner=lambda j, s: "should not run")

    assert "still running" in str(refusal.value)
    release.set()
    assert _wait_for(lambda: jobs.get_job("directory-eu").status == "done")


def test_the_same_job_cannot_be_double_started_by_a_double_click():
    release = threading.Event()
    jobs.start("detect", runner=lambda j, s: release.wait(timeout=5) or "done")

    with pytest.raises(jobs.JobRefused):
        jobs.start("detect", runner=lambda j, s: "second run")

    release.set()
    assert _wait_for(lambda: jobs.get_job("detect").status == "done")


def test_a_crashing_sweep_is_contained_and_reported():
    """A dead register must not take the app down with it."""
    def _boom(job, spec):
        raise RuntimeError("EUDAMED fell over")

    jobs.start("directory-eu", runner=_boom)
    assert _wait_for(lambda: jobs.get_job("directory-eu").status == "failed")

    failed = jobs.get_job("directory-eu")
    assert "EUDAMED fell over" in failed.error
    assert failed.result is None
    # And the app is still usable afterwards.
    assert jobs.anything_running() is None


def test_progress_says_what_it_actually_knows():
    """No fake spinner: report a real query count, or admit there isn't one."""
    class FakeReport:
        queries_made = 0

    report = FakeReport()
    release = threading.Event()

    def _runner(job, spec):
        job._report = report
        release.wait(timeout=5)
        return "done"

    job = jobs.start("directory-us", runner=_runner)
    assert "no counter available" in job.progress

    report.queries_made = 42
    assert "42 queries made" in jobs.get_job("directory-us").progress

    release.set()
    assert _wait_for(lambda: jobs.get_job("directory-us").status == "done")
    # Finished jobs report no progress — they report a result.
    assert jobs.get_job("directory-us").progress == ""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_tasks_page_lists_every_button(session):
    from fastapi.testclient import TestClient

    from signal_engine.web.app import app

    client = TestClient(app)
    page = client.get("/tasks")

    assert page.status_code == 200
    assert "Check for new signals" in page.text
    assert "Refresh EU/UK directory" in page.text
    assert "Refresh US directory" in page.text


def test_the_page_only_refreshes_itself_while_something_runs(session):
    """A meta refresh on an idle page would reload under you for no reason."""
    from fastapi.testclient import TestClient

    from signal_engine.web.app import app

    client = TestClient(app)
    assert "http-equiv=\"refresh\"" not in client.get("/tasks").text

    release = threading.Event()
    jobs.start("detect", runner=lambda j, s: release.wait(timeout=5) or "done")
    try:
        assert "http-equiv=\"refresh\"" in client.get("/tasks").text
    finally:
        release.set()
        _wait_for(lambda: jobs.get_job("detect").status == "done")


def test_starting_a_job_from_the_page_redirects_rather_than_hanging(session):
    """The request must return at once; the sweep continues behind it."""
    from fastapi.testclient import TestClient

    from signal_engine.web.app import app

    client = TestClient(app)
    release = threading.Event()
    jobs.all_jobs()
    jobs._JOBS["detect"].status = "idle"

    # Patch the action so the route starts something harmless.
    original = jobs.ACTIONS["detect"]
    jobs.ACTIONS["detect"] = lambda j, s: release.wait(timeout=5) or "done"
    try:
        response = client.post("/tasks/detect", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert _wait_for(lambda: jobs.get_job("detect").running)
    finally:
        release.set()
        _wait_for(lambda: jobs.get_job("detect").status == "done")
        jobs.ACTIONS["detect"] = original
