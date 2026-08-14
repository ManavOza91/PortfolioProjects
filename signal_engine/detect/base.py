"""Shared machinery for the detectors.

A detector's only job is to turn a public source into `Detection` objects. It
never writes to the database, never scores anything, and never decides whether a
signal is trustworthy — the runner does all of that, identically for every source.
That keeps a new source (PubMed, careers pages, Innovate UK) to one small module.

Failure is always local. A source that is down, rate-limited or returning junk
produces a `SourceError` in the result and the other sources carry on. Free public
APIs go down; that is not a reason to lose the run.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import httpx

from ..config import get_config
from ..ingest import normalise_name

log = logging.getLogger(__name__)

_last_request_at: dict[str, float] = {}


@dataclass
class Detection:
    """One thing found in a public source, before any judgement is applied."""

    type_key: str
    company_name: str
    title: str
    detected_date: dt.date
    url: str | None = None
    source_name: str = ""
    external_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ISO-3166 alpha-2 where the source gives it. Carried onto the company so the
    # queue can be worked by region. None where the source doesn't say — the press
    # never does — and None must stay None rather than becoming a guess.
    country: str | None = None

    # 0..1, how sure the SOURCE is that this record is what it claims to be.
    # Company matching is applied on top of this by the runner.
    source_confidence: float = 0.9

    # True when the source issues one record per real-world event — a K-number is
    # one clearance, a EUDAMED UUID is one device. Two such records in the same
    # week are two events and must both stand.
    #
    # False when the source reports events rather than issuing them. Three outlets
    # covering one alliance are three URLs and one event; the runner collapses
    # them so a single announcement cannot compound itself into Tier A.
    unique_per_event: bool = True

    def summary(self) -> str:
        return self.title.strip()


@dataclass
class SourceError:
    source: str
    message: str
    fatal: bool = False


@dataclass
class DetectorResult:
    detections: list[Detection] = field(default_factory=list)
    errors: list[SourceError] = field(default_factory=list)
    queries_made: int = 0
    # Things worth saying out loud that are not failures — most often "this source
    # had more than we asked for", which on a long backfill means signals were lost.
    warnings: list[str] = field(default_factory=list)


class Detector(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult: ...

    def discover(self, since: dt.date) -> DetectorResult: ...


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _throttle(source: str) -> None:
    delay = float(get_config().detection.get("request_delay_seconds", 0.5))
    last = _last_request_at.get(source)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < delay:
            time.sleep(delay - elapsed)
    _last_request_at[source] = time.monotonic()


def fetch(
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    as_json: bool = True,
    timeout: float | None = None,
) -> tuple[Any | None, SourceError | None]:
    """One polite HTTP request. Returns (payload, error) — never raises.

    GET unless `json_body` is given, in which case POST: MHRA's public search is
    a POST endpoint behind its search page.
    """
    cfg = get_config().detection
    _throttle(source)

    headers = {"User-Agent": str(cfg.get("user_agent", "SignalEngine/0.1"))}
    timeout = timeout if timeout is not None else float(cfg.get("timeout_seconds", 30))

    try:
        if json_body is not None:
            response = httpx.post(
                url, params=params, json=json_body, headers=headers,
                timeout=timeout, follow_redirects=True,
            )
        else:
            response = httpx.get(
                url, params=params, headers=headers, timeout=timeout,
                follow_redirects=True,
            )
    except Exception as exc:  # noqa: BLE001 — a dead source must not kill the run
        return None, SourceError(source, f"could not reach {url}: {type(exc).__name__}")

    if response.status_code == 404:
        # openFDA returns 404 for "no matches", which is not an error.
        return None, None
    if response.status_code == 429:
        return None, SourceError(source, "rate-limited by the source; try again later")
    if response.status_code >= 400:
        body = response.text[:160].replace("\n", " ")
        return None, SourceError(source, f"HTTP {response.status_code} from {url}: {body}")

    if not as_json:
        return response.text, None

    try:
        return response.json(), None
    except Exception:  # noqa: BLE001
        return None, SourceError(source, f"unparsable response from {url}")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_date(value: Any, *, default: dt.date | None = None) -> dt.date:
    default = default or dt.datetime.now(dt.timezone.utc).date()
    if isinstance(value, dt.date):
        return value
    if not value:
        return default
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    # RFC 822, as used by RSS: "Tue, 11 Aug 2026 09:00:00 GMT"
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).date()
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# Company matching
# ---------------------------------------------------------------------------

# Words too generic to carry a partial match on their own.
_WEAK_TOKENS = {
    "bio", "biotech", "labs", "lab", "life", "sciences", "science", "medical",
    "health", "healthcare", "diagnostics", "diagnostic", "pharma", "pharmaceutical",
    "pharmaceuticals", "technologies", "technology", "systems", "solutions",
    "international", "global", "research", "therapeutics", "medtech",
}


@dataclass
class NameMatch:
    matched: bool
    confidence: float
    kind: str  # exact | partial | none


def match_company_name(detected: str, known: str) -> NameMatch:
    """Decide whether a name from a public record is a company we track.

    Exact (after normalising case and legal suffixes) is trusted. A containment
    match is reported at a confidence deliberately below the auto-scoring bar, so
    "Northwind" matching "Northwind Diagnostics" always reaches you for review
    rather than quietly scoring.
    """
    cfg = get_config().detection.get("matching", {})
    exact_conf = float(cfg.get("exact_confidence", 0.92))
    partial_conf = float(cfg.get("partial_confidence", 0.60))
    min_len = int(cfg.get("min_name_length", 5))

    a, b = normalise_name(detected), normalise_name(known)
    if not a or not b:
        return NameMatch(False, 0.0, "none")

    if a == b:
        return NameMatch(True, exact_conf, "exact")

    if len(a) < min_len or len(b) < min_len:
        return NameMatch(False, 0.0, "none")

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        # Refuse a containment match carried entirely by generic words.
        distinctive = [t for t in shorter.split() if t not in _WEAK_TOKENS]
        if distinctive:
            return NameMatch(True, partial_conf, "partial")

    return NameMatch(False, 0.0, "none")


def best_match(detected: str, known_names: Iterable[str]) -> tuple[str | None, NameMatch]:
    best_name: str | None = None
    best = NameMatch(False, 0.0, "none")
    for name in known_names:
        result = match_company_name(detected, name)
        if result.matched and result.confidence > best.confidence:
            best_name, best = name, result
    return best_name, best


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


def classify_by_keyword(text: str) -> tuple[str | None, str | None]:
    """Map free text to a signal type using the config's keyword table.

    Returns (type_key, matched_keyword). Longest keyword wins, so "lyophilization
    capacity" beats a bare "capacity" if both were listed.
    """
    haystack = " ".join(str(text or "").lower().split())
    if not haystack:
        return None, None

    keywords: dict[str, list[str]] = get_config().detection.get("keywords", {})
    best_key = best_word = None
    for type_key, words in keywords.items():
        for word in words or []:
            w = str(word).lower().strip()
            if w and w in haystack and (best_word is None or len(w) > len(best_word)):
                best_key, best_word = type_key, w
    return best_key, best_word


# ---------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------

# Only what the registers actually emit. Anything unrecognised stays unknown
# rather than being guessed at. Lives here rather than in directory.py so the
# detectors can use it without importing the directory (which imports this).
_COUNTRY_NAMES = {
    "united kingdom": "GB", "england, united kingdom": "GB",
    "scotland, united kingdom": "GB", "wales, united kingdom": "GB",
    "northern ireland, united kingdom": "GB",
    "ireland": "IE", "germany": "DE", "france": "FR", "netherlands": "NL",
    "belgium": "BE", "switzerland": "CH", "sweden": "SE", "denmark": "DK",
    "spain": "ES", "italy": "IT", "austria": "AT", "norway": "NO",
    "finland": "FI", "poland": "PL", "portugal": "PT", "czech republic": "CZ",
    "united states": "US", "china": "CN", "singapore": "SG", "japan": "JP",
}


def country_code(value: str | None) -> str | None:
    """Normalise a register's country field to ISO-3166 alpha-2, or None."""
    if not value:
        return None
    text = " ".join(str(value).split()).strip().casefold()
    if len(text) == 2:
        return text.upper()
    return _COUNTRY_NAMES.get(text)
