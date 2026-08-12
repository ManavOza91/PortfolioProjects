"""Runs the detectors and turns what they find into signals.

Every source goes through exactly the same judgement here, so adding a source can
never accidentally invent its own rules about what gets scored.

    detection  ->  match to a company  ->  confidence  ->  scored | review
                                                             ^
                                        anything uncertain stops here
                                        for you to approve or reject
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..ingest import create_signal, find_or_create_company
from ..models import Company, Signal, SignalType
from ..scoring import rescore_company
from .base import Detection, DetectorResult, SourceError, best_match
from .news import NewsDetector
from .openfda import OpenFDADetector
from .sbir import SBIRDetector

log = logging.getLogger(__name__)

DETECTORS = {
    "openfda": OpenFDADetector,
    "sbir": SBIRDetector,
    "news": NewsDetector,
}


@dataclass
class Outcome:
    detection: Detection
    company_name: str | None
    match_kind: str
    confidence: float
    status: str          # scored | review | skipped-duplicate | skipped-unmatched
    signal_id: int | None = None
    new_company: bool = False


@dataclass
class RunReport:
    dry_run: bool
    since: dt.date
    discover: bool = False
    companies_scanned: int = 0
    queries_made: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    errors: list[SourceError] = field(default_factory=list)
    sources_run: list[str] = field(default_factory=list)

    def _by(self, status: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def scored(self) -> list[Outcome]:
        return self._by("scored")

    @property
    def review(self) -> list[Outcome]:
        return self._by("review")

    @property
    def duplicates(self) -> list[Outcome]:
        return self._by("skipped-duplicate")

    @property
    def unmatched(self) -> list[Outcome]:
        return self._by("skipped-unmatched")

    def render(self) -> str:
        head = "DRY RUN — nothing was written." if self.dry_run else "Detection complete."
        lines = [
            head,
            "",
            f"Sources:   {', '.join(self.sources_run) or 'none'}",
            f"Since:     {self.since.isoformat()}"
            + ("   (discovery on)" if self.discover else ""),
            f"Companies: {self.companies_scanned} scanned, {self.queries_made} queries made",
            "",
            f"  {len(self.scored):>4}  scored automatically",
            f"  {len(self.review):>4}  waiting for you to review",
            f"  {len(self.duplicates):>4}  already known (skipped)",
            f"  {len(self.unmatched):>4}  no company match (skipped)",
        ]

        if self.scored:
            lines += ["", "Scored:"]
            for o in self.scored[:15]:
                lines.append(f"  + {o.company_name}: {o.detection.title[:74]}")

        if self.review:
            lines += ["", "For review:"]
            for o in self.review[:15]:
                why = "uncertain company match" if o.match_kind == "partial" else "weak source"
                lines.append(
                    f"  ? {o.company_name}: {o.detection.title[:60]} "
                    f"({o.confidence:.0%}, {why})"
                )
            if len(self.review) > 15:
                lines.append(f"    ...and {len(self.review) - 15} more")

        if self.errors:
            lines += ["", "Sources that failed (the rest of the run continued):"]
            for e in self.errors:
                lines.append(f"  ! {e.source}: {e.message}")

        if self.review and not self.dry_run:
            lines += ["", "Open the Review page to approve or reject the uncertain ones."]

        return "\n".join(lines)


# ---------------------------------------------------------------------------


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _known_type_keys(session: Session, tenant_id: int) -> set[str]:
    return {
        t.key
        for t in session.scalars(
            select(SignalType).where(
                SignalType.tenant_id == tenant_id, SignalType.active.is_(True)
            )
        )
    }


def _already_seen(session: Session, detection: Detection, company_id: int) -> bool:
    """Re-running detection must never duplicate a signal.

    Keyed on the source URL where there is one — a K-number page or an article link
    is unique per record. Falls back to type + company + date, which catches sources
    that give us no stable link.
    """
    if detection.url:
        hit = session.scalar(
            select(Signal.id).where(
                Signal.company_id == company_id, Signal.detail_url == detection.url
            )
        )
        if hit:
            return True

    hit = session.scalar(
        select(Signal.id).where(
            Signal.company_id == company_id,
            Signal.type_key == detection.type_key,
            Signal.detected_date == detection.detected_date,
            Signal.source == "auto",
        )
    )
    return bool(hit)


def run_detection(
    session: Session,
    *,
    sources: list[str] | None = None,
    since: dt.date | None = None,
    dry_run: bool = False,
    discover: bool | None = None,
    tenant_id: int | None = None,
) -> RunReport:
    cfg = get_config()
    detection_cfg = cfg.detection
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id

    since = since or _today() - dt.timedelta(
        days=int(detection_cfg.get("default_lookback_days", 90))
    )
    if discover is None:
        discover = bool(detection_cfg.get("discovery", {}).get("enabled", False))

    report = RunReport(dry_run=dry_run, since=since, discover=discover)

    companies = list(
        session.scalars(select(Company).where(Company.tenant_id == tenant_id))
    )
    known_names = [c.name for c in companies]
    report.companies_scanned = len(known_names)

    valid_types = _known_type_keys(session, tenant_id)
    max_new = int(detection_cfg.get("discovery", {}).get("max_new_companies_per_run", 25))
    new_companies_made = 0

    wanted = sources or list(DETECTORS)
    for name in wanted:
        factory = DETECTORS.get(name)
        if factory is None:
            report.errors.append(SourceError(name, "unknown source"))
            continue

        detector = factory()
        if not detector.enabled():
            continue
        report.sources_run.append(name)

        results: list[DetectorResult] = []
        if known_names:
            results.append(detector.for_companies(known_names, since))
        if discover:
            results.append(detector.discover(since))

        for result in results:
            report.queries_made += result.queries_made
            report.errors.extend(result.errors)

            for detection in result.detections:
                outcome = _process(
                    session,
                    detection,
                    known_names=known_names,
                    valid_types=valid_types,
                    discover=discover,
                    can_make_new=new_companies_made < max_new,
                    dry_run=dry_run,
                )
                if outcome.new_company:
                    new_companies_made += 1
                    known_names.append(outcome.company_name or "")
                report.outcomes.append(outcome)

    if dry_run:
        session.rollback()
    else:
        # Rescore only the companies that actually gained a scored signal.
        touched = {
            o.company_name for o in report.scored if o.company_name
        }
        for company in companies:
            if company.name in touched:
                rescore_company(session, company)
        session.flush()

    return report


def _process(
    session: Session,
    detection: Detection,
    *,
    known_names: list[str],
    valid_types: set[str],
    discover: bool,
    can_make_new: bool,
    dry_run: bool,
) -> Outcome:
    cfg = get_config()

    if detection.type_key not in valid_types:
        return Outcome(detection, None, "none", 0.0, "skipped-unmatched")

    matched_name, match = best_match(detection.company_name, known_names)

    new_company = False
    if not match.matched:
        if not (discover and can_make_new):
            return Outcome(detection, None, "none", 0.0, "skipped-unmatched")
        # Discovered company: never trusted enough to score. It enters as a
        # watchlist entry and its signal waits for you.
        matched_name = detection.company_name
        match_kind = "discovered"
        confidence = min(
            detection.source_confidence,
            float(cfg.detection.get("matching", {}).get("partial_confidence", 0.60)),
        )
        new_company = True
    else:
        match_kind = match.kind
        # Confidence is the weaker of "is this record real" and "is it our company".
        confidence = min(detection.source_confidence, match.confidence)

    company, created = find_or_create_company(
        session, name=matched_name or detection.company_name,
        status="watchlist", created_by="detect",
    )
    new_company = new_company or created

    if _already_seen(session, detection, company.id):
        return Outcome(detection, company.name, match_kind, confidence,
                       "skipped-duplicate", new_company=False)

    threshold = cfg.auto_review_threshold
    status = "scored" if confidence >= threshold else "review"

    signal = create_signal(
        session,
        company,
        type_key=detection.type_key,
        source="auto",
        raw_text=json.dumps(
            {"source": detection.source_name, "title": detection.title, **detection.raw},
            indent=2, default=str,
        ),
        parsed_summary=detection.summary(),
        confidence=confidence,
        detected_date=detection.detected_date,
        detail_url=detection.url,
        status=status,
        created_by="detect",
    )

    return Outcome(
        detection, company.name, match_kind, confidence, status,
        signal_id=signal.id, new_company=new_company,
    )
