"""Read-side queries for the dashboard.

Scores are computed live here rather than read from the cache, because decay means
yesterday's stored score is already slightly wrong. The cache exists for sorting and
history; the queue you act on is always current.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_config
from .models import Activity, Company, Contact, Signal, SignalType
from .scoring import Contribution, ScoreBreakdown, build_contributions, score_from_contributions


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# The ranked company queue
# ---------------------------------------------------------------------------


@dataclass
class QueueRow:
    company: Company
    breakdown: ScoreBreakdown
    last_touch: dt.date | None
    touch_count: int
    contact_count: int
    research_action: str

    @property
    def score(self) -> float:
        return self.breakdown.score

    @property
    def tier(self) -> str | None:
        return self.breakdown.tier

    @property
    def reason(self) -> str:
        return self.breakdown.top_reason

    @property
    def live_signals(self) -> list[Contribution]:
        return sorted(
            self.breakdown.contributions, key=lambda c: c.effective_weight, reverse=True
        )

    @property
    def days_since_touch(self) -> int | None:
        return (_today() - self.last_touch).days if self.last_touch else None

    @property
    def never_contacted(self) -> bool:
        return self.touch_count == 0


def _research_action(company: Company, breakdown: ScoreBreakdown) -> str:
    """A deterministic pointer at who to look for.

    Finding the right person is a manual research step, and it is the step that
    produces the best outreach. This does not try to do it — it says where to start.
    The coach will make this specific per company in a later phase.
    """
    cfg = get_config()
    fit = breakdown.product_fit or company.product_fit or "both"
    products = cfg.products

    if fit == "A":
        who = products.get("A", {}).get("buyers", "")
    elif fit == "B":
        who = products.get("B", {}).get("buyers", "")
    else:
        who = " Or ".join(
            filter(
                None,
                [
                    str(products.get("A", {}).get("buyers", "")).strip(),
                    str(products.get("B", {}).get("buyers", "")).strip(),
                ],
            )
        )
    who = " ".join(str(who).split())
    return f"Research on their site, LinkedIn and recent papers: {who}"


def company_breakdown(
    session: Session, company: Company, *, as_of: dt.date | None = None
) -> ScoreBreakdown:
    types = {
        t.key: t
        for t in session.scalars(
            select(SignalType).where(SignalType.tenant_id == company.tenant_id)
        )
    }
    signals = list(session.scalars(select(Signal).where(Signal.company_id == company.id)))
    return score_from_contributions(build_contributions(signals, types, as_of=as_of))


def ranked_queue(
    session: Session,
    *,
    tenant_id: int | None = None,
    limit: int | None = 50,
    tiers: set[str] | None = None,
    product_fit: str | None = None,
    include_unsignalled: bool = False,
    region: str | None = None,
    as_of: dt.date | None = None,
) -> list[QueueRow]:
    """Companies with active signals, strongest first.

    Companies with no signal do not appear unless explicitly asked for. That is the
    whole thesis: no signal, no entry.

    `region` slices this queue only. It never merges in the research directory —
    a region is a view over the signalled list, not a combined list.
    """
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    as_of = as_of or _today()

    types = {
        t.key: t
        for t in session.scalars(select(SignalType).where(SignalType.tenant_id == tenant_id))
    }

    companies = list(
        session.scalars(select(Company).where(Company.tenant_id == tenant_id))
    )
    if region:
        from .directory import region_for

        companies = [c for c in companies if region_for(c.country) == region]
    if not companies:
        return []

    signals_by_company: dict[int, list[Signal]] = {}
    for s in session.scalars(select(Signal).where(Signal.tenant_id == tenant_id)):
        signals_by_company.setdefault(s.company_id, []).append(s)

    touch_rows = session.execute(
        select(
            Activity.company_id,
            func.count(Activity.id),
            func.max(Activity.date),
        )
        .where(Activity.tenant_id == tenant_id, Activity.direction == "out")
        .group_by(Activity.company_id)
    ).all()
    touches = {cid: (count, last) for cid, count, last in touch_rows}

    contact_rows = session.execute(
        select(Contact.company_id, func.count(Contact.id))
        .where(Contact.tenant_id == tenant_id)
        .group_by(Contact.company_id)
    ).all()
    contacts = dict(contact_rows)

    rows: list[QueueRow] = []
    for company in companies:
        contributions = build_contributions(
            signals_by_company.get(company.id, []), types, as_of=as_of
        )
        if not contributions and not include_unsignalled:
            continue
        breakdown = score_from_contributions(contributions)

        if tiers and breakdown.tier not in tiers:
            continue
        fit = breakdown.product_fit or company.product_fit
        if product_fit and fit not in (product_fit, "both"):
            continue

        count, last = touches.get(company.id, (0, None))
        if isinstance(last, str):  # SQLite may hand back a string from max()
            last = dt.date.fromisoformat(last)

        rows.append(
            QueueRow(
                company=company,
                breakdown=breakdown,
                last_touch=last,
                touch_count=count,
                contact_count=contacts.get(company.id, 0),
                research_action=_research_action(company, breakdown),
            )
        )

    rows.sort(key=lambda r: r.score, reverse=True)
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------
# Which signal types actually produce meetings
# ---------------------------------------------------------------------------


@dataclass
class ConversionRow:
    type_key: str
    label: str
    companies: int = 0
    touches: int = 0
    replies: int = 0
    meetings: int = 0

    @property
    def reply_rate(self) -> float:
        return self.replies / self.touches if self.touches else 0.0

    @property
    def meeting_rate(self) -> float:
        return self.meetings / self.touches if self.touches else 0.0


def signal_conversion(
    session: Session, *, tenant_id: int | None = None
) -> list[ConversionRow]:
    """Conversion by the signal that was governing at the time of contact.

    This is the answer to "which signal types actually produce meetings", and it is
    only trustworthy because activity records the signal it acted on rather than
    joining to whatever the company's strongest signal happens to be today.
    """
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id

    labels = {
        t.key: t.label
        for t in session.scalars(select(SignalType).where(SignalType.tenant_id == tenant_id))
    }
    signal_types = {
        s.id: s.type_key
        for s in session.scalars(select(Signal).where(Signal.tenant_id == tenant_id))
    }

    rows: dict[str, ConversionRow] = {}
    seen_companies: dict[str, set[int]] = {}

    for a in session.scalars(select(Activity).where(Activity.tenant_id == tenant_id)):
        key = signal_types.get(a.signal_id_at_time_of_contact or -1) or "__none__"
        row = rows.setdefault(
            key, ConversionRow(type_key=key, label=labels.get(key, "No signal at time of contact"))
        )
        seen_companies.setdefault(key, set()).add(a.company_id)

        outcome = (a.outcome or "").strip().lower()
        if a.direction == "out" and a.type in {"email", "call", "linkedin", "meeting"}:
            row.touches += 1
        if a.type == "reply" or a.direction == "in" or outcome in {"reply", "replied"}:
            row.replies += 1
        if a.type == "meeting" or outcome in {"meeting_booked", "meeting"}:
            row.meetings += 1

    for key, row in rows.items():
        row.companies = len(seen_companies.get(key, ()))

    return sorted(rows.values(), key=lambda r: (r.meetings, r.touches), reverse=True)


# ---------------------------------------------------------------------------
# Activity and review summaries
# ---------------------------------------------------------------------------


@dataclass
class ActivitySummary:
    since: dt.date
    total: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    recent: list[tuple[Activity, Company]] = field(default_factory=list)


def activity_summary(
    session: Session, *, days: int = 30, tenant_id: int | None = None, limit: int = 12
) -> ActivitySummary:
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    since = _today() - dt.timedelta(days=days)

    summary = ActivitySummary(since=since)
    rows = list(
        session.execute(
            select(Activity, Company)
            .join(Company, Company.id == Activity.company_id)
            .where(Activity.tenant_id == tenant_id, Activity.date >= since)
            .order_by(Activity.date.desc(), Activity.id.desc())
        ).all()
    )
    summary.total = len(rows)
    for activity, _company in rows:
        summary.by_type[activity.type] = summary.by_type.get(activity.type, 0) + 1
    summary.recent = [(a, c) for a, c in rows[:limit]]
    return summary


def review_queue(
    session: Session, *, tenant_id: int | None = None, limit: int = 50
) -> list[tuple[Signal, Company]]:
    """Signals held back: below the confidence bar, or nothing scoreable found."""
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    return list(
        session.execute(
            select(Signal, Company)
            .join(Company, Company.id == Signal.company_id)
            .where(
                Signal.tenant_id == tenant_id,
                Signal.status.in_(("review", "unscored")),
            )
            .order_by(Signal.created_at.desc())
            .limit(limit)
        ).all()
    )


@dataclass
class Totals:
    companies: int = 0
    signalled_companies: int = 0
    watchlist: int = 0
    signals_scored: int = 0
    signals_review: int = 0
    contacts: int = 0
    activity_30d: int = 0


def totals(session: Session, *, tenant_id: int | None = None) -> Totals:
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    since = _today() - dt.timedelta(days=30)

    def count(stmt) -> int:
        return int(session.scalar(stmt) or 0)

    signalled = count(
        select(func.count(func.distinct(Signal.company_id))).where(
            Signal.tenant_id == tenant_id, Signal.status == "scored"
        )
    )
    return Totals(
        companies=count(select(func.count(Company.id)).where(Company.tenant_id == tenant_id)),
        signalled_companies=signalled,
        watchlist=count(
            select(func.count(Company.id)).where(
                Company.tenant_id == tenant_id, Company.status == "watchlist"
            )
        ),
        signals_scored=count(
            select(func.count(Signal.id)).where(
                Signal.tenant_id == tenant_id, Signal.status == "scored"
            )
        ),
        signals_review=count(
            select(func.count(Signal.id)).where(
                Signal.tenant_id == tenant_id, Signal.status.in_(("review", "unscored"))
            )
        ),
        contacts=count(select(func.count(Contact.id)).where(Contact.tenant_id == tenant_id)),
        activity_30d=count(
            select(func.count(Activity.id)).where(
                Activity.tenant_id == tenant_id, Activity.date >= since
            )
        ),
    )
