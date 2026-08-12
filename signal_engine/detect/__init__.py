"""Automatic signal detection from free public sources.

Watchlist-first: every company you already track is checked against each source.
Discovery (finding companies you have never heard of) is off by default and turned
on with `--discover`, because a discovered company is a guess, not a signal.

Nothing here decides on its own that something is worth acting on. Uncertain finds
land on the Review page for you to approve or reject.
"""

from .base import Detection, DetectorResult, SourceError
from .runner import DETECTORS, RunReport, run_detection

__all__ = [
    "DETECTORS",
    "Detection",
    "DetectorResult",
    "RunReport",
    "SourceError",
    "run_detection",
]
