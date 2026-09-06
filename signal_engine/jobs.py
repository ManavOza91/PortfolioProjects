"""Running the long jobs from the app, so the terminal is never needed.

`detect` and `directory-refresh` take minutes to tens of minutes. A web request
cannot wait that long, so each runs on a background thread and the page reports
what it is doing. The browser re-requests the page every few seconds — a meta
refresh, not JavaScript, because this app has no build step and works offline.

Two deliberate limits, both stated in the UI rather than hidden:

* **One job at a time.** They share a SQLite file, and two sweeps writing at once
  is not worth the trouble for a single-user tool.
* **A restart loses a running job.** State lives in memory, not the database. If
  you close the app mid-sweep, nothing is corrupted — whatever the sweep had
  already written is committed — but the run stops and the result is gone.

Progress is honest about what it can see: the directory sweep shares its live
report object, so the query count is real. Detection has no such counter mid-run,
so it shows elapsed time and says that is all it knows.
"""

from __future__ import annotations

import datetime as dt
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import get_config

_LOCK = threading.Lock()
_JOBS: dict[str, "Job"] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class Job:
    key: str
    label: str
    blurb: str = ""
    status: str = "idle"          # idle | running | done | failed
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    result: str | None = None
    error: str | None = None
    # A live handle on the running report, so the page can show real progress
    # rather than a spinner that means nothing.
    _report: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed_seconds(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or _now()
        return int((end - self.started_at).total_seconds())

    @property
    def elapsed_human(self) -> str:
        seconds = self.elapsed_seconds
        if seconds is None:
            return ""
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"

    @property
    def progress(self) -> str:
        """What we can honestly say while it runs."""
        if not self.running:
            return ""
        queries = getattr(self._report, "queries_made", None)
        if queries:
            return f"{queries} queries made so far"
        return "started — no counter available for this job yet"


# ---------------------------------------------------------------------------
# What the buttons do
# ---------------------------------------------------------------------------


# What the buttons are when config says nothing about them.
#
# This is a fallback, not a default to design around: `ui.jobs` in config.yaml
# overrides it entirely, and that is where a customer relabels or regroups them.
# It exists because config.yaml is deliberately NOT overwritten when the app
# updates — the right call, since it holds your territories and thresholds — and
# without this a config written before a feature existed would silently remove
# that feature's buttons and leave an empty page with no explanation.
#
# Nothing here names a product, a signal type or a threshold. Register names and
# the two code paths are mechanism, which is why they can live in code at all.
FALLBACK_JOBS: list[dict] = [
    {
        "key": "detect",
        "label": "Check for new signals",
        "blurb": "Looks for new regulatory filings and press on the companies you track.",
        "action": "detect",
    },
    {
        "key": "directory-eu",
        "label": "Refresh EU/UK directory",
        "blurb": "Walks EUDAMED and MHRA. Slow — tens of minutes.",
        "action": "directory",
        "sources": ["eudamed", "mhra"],
    },
    {
        "key": "directory-us",
        "label": "Refresh US directory",
        "blurb": "Walks openFDA. Minutes, not hours — it filters by country at source.",
        "action": "directory",
        "sources": ["openfda"],
    },
    {
        "key": "clear-directory",
        "label": "Empty the directory",
        "blurb": (
            "Deletes entries you haven't looked at, so the next refresh rebuilds "
            "the list under your current ICP settings. Anything you marked "
            "researched or dismissed is kept. Your companies and signals are in a "
            "different table and are not touched."
        ),
        "action": "clear-directory",
    },
]


def job_specs() -> list[dict]:
    """The buttons. From config where set, otherwise the fallback above."""
    configured = list(get_config().raw.get("ui", {}).get("jobs", []) or [])
    return configured or [dict(spec) for spec in FALLBACK_JOBS]


def using_fallback_jobs() -> bool:
    """True when config has nothing to say — the page mentions it quietly."""
    return not (get_config().raw.get("ui", {}).get("jobs") or [])


def _spec(key: str) -> dict | None:
    for spec in job_specs():
        if str(spec.get("key")) == key:
            return spec
    return None


def _run_detect(job: Job, spec: dict) -> str:
    from .db import session_scope
    from .detect.runner import RunReport, run_detection

    with session_scope() as session:
        report = run_detection(session, sources=spec.get("sources") or None)
        if isinstance(report, RunReport):
            job._report = report
        return report.render()


def _run_directory(job: Job, spec: dict) -> str:
    from .db import session_scope
    from .directory import BuildReport, build_directory

    # Hand the job the live report before the sweep starts, so the page can show
    # the query count climbing rather than an unmoving "please wait".
    report = BuildReport()
    job._report = report

    with session_scope() as session:
        build_directory(session, sources=list(spec.get("sources") or []), report=report)
    return report.render()


def _run_clear_directory(job: Job, spec: dict) -> str:
    from .db import session_scope
    from .directory import clear_unreviewed, directory_total

    with session_scope() as session:
        removed = clear_unreviewed(session)
        left = directory_total(session)

    return (
        f"Cleared {removed} unreviewed entries from the directory.\n"
        f"{left} remain — the ones you marked researched or dismissed.\n\n"
        "Run a directory refresh to repopulate it under the current ICP settings."
    )


ACTIONS: dict[str, Callable[[Job, dict], str]] = {
    "detect": _run_detect,
    "directory": _run_directory,
    "clear-directory": _run_clear_directory,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def all_jobs() -> list[Job]:
    """Every configured button, with whatever we know about its last run."""
    out: list[Job] = []
    with _LOCK:
        for spec in job_specs():
            key = str(spec.get("key"))
            job = _JOBS.get(key)
            if job is None:
                job = Job(
                    key=key,
                    label=str(spec.get("label", key)),
                    blurb=str(spec.get("blurb", "")),
                )
                _JOBS[key] = job
            else:
                job.label = str(spec.get("label", job.label))
                job.blurb = str(spec.get("blurb", job.blurb))
            out.append(job)
    return out


def get_job(key: str) -> Job | None:
    for job in all_jobs():
        if job.key == key:
            return job
    return None


def anything_running() -> Job | None:
    for job in all_jobs():
        if job.running:
            return job
    return None


class JobRefused(Exception):
    """Raised when a job cannot start, with a reason fit to show the user."""


def start(key: str, *, runner: Callable[[Job, dict], str] | None = None) -> Job:
    """Begin a job on a background thread. Raises JobRefused if it cannot."""
    spec = _spec(key)
    if spec is None:
        raise JobRefused(f"There is no job called {key!r}.")

    action = str(spec.get("action", ""))
    run = runner or ACTIONS.get(action)
    if run is None:
        raise JobRefused(f"Job {key!r} has no action I know how to run.")

    all_jobs()  # make sure the registry is populated

    with _LOCK:
        busy = next((j for j in _JOBS.values() if j.status == "running"), None)
        if busy is not None:
            raise JobRefused(
                f"{busy.label} is still running ({busy.elapsed_human}). "
                "One at a time — they share the same database file."
            )

        job = _JOBS[key]
        job.status = "running"
        job.started_at = _now()
        job.finished_at = None
        job.result = None
        job.error = None
        job._report = None

    def _worker() -> None:
        try:
            output = run(job, spec)
        except Exception:  # noqa: BLE001 — a failed sweep must not kill the app
            with _LOCK:
                job.status = "failed"
                job.error = traceback.format_exc(limit=3)
                job.finished_at = _now()
            return
        with _LOCK:
            job.status = "done"
            job.result = output
            job.finished_at = _now()
            job._report = None

    threading.Thread(target=_worker, name=f"job-{key}", daemon=True).start()
    return job


def reset() -> None:
    """Used by tests."""
    with _LOCK:
        _JOBS.clear()
