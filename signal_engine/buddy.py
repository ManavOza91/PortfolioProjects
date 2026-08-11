"""The Buddy Score — a leading indicator of pipeline health.

Pipeline value is lagging. Outbound behaviour is leading. This scores the behaviour.

    Progression     30   replies -> conversations -> meetings booked
    Signal quality  25   % of outreach to Tier A/B signal companies
    Volume          25   touches against your own target
    Consistency     20   active days, no gaps — steady beats bursts

Signal quality is weighted high on purpose. Forty touches to unsignalled companies
must score worse than fifteen to signalled ones, or the score trains you back into
the spray model it exists to replace. tests/test_buddy.py enforces exactly that.

Meetings are the terminal metric, not orders. Cycles run six months to two years,
which is far too long for orders to be usable feedback.

Tier is read from `company_tier_at_time_of_contact`, recorded when the touch was
logged — so rescoring a company months later cannot retroactively flatter or damn
outreach you already sent.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_config
from .models import Activity, BuddySnapshot

PeriodType = str  # week | month | quarter | year


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------


def week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day - dt.timedelta(days=day.weekday())  # Monday
    return start, start + dt.timedelta(days=6)


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day.replace(day=1)
    end = (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def quarter_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    q = (day.month - 1) // 3
    start = dt.date(day.year, q * 3 + 1, 1)
    end_month = start.month + 2
    end = (dt.date(day.year, end_month, 28) + dt.timedelta(days=8)).replace(
        day=1
    ) - dt.timedelta(days=1)
    return start, end


def year_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    return dt.date(day.year, 1, 1), dt.date(day.year, 12, 31)


BOUNDS = {
    "week": week_bounds,
    "month": month_bounds,
    "quarter": quarter_bounds,
    "year": year_bounds,
}


def bounds_for(period_type: PeriodType, day: dt.date) -> tuple[dt.date, dt.date]:
    try:
        return BOUNDS[period_type](day)
    except KeyError:
        raise ValueError(
            f"Unknown period type {period_type!r}; expected one of {sorted(BOUNDS)}"
        ) from None


def previous_period(period_type: PeriodType, start: dt.date) -> tuple[dt.date, dt.date]:
    return bounds_for(period_type, start - dt.timedelta(days=1))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class BuddyResult:
    period_type: PeriodType
    period_start: dt.date
    period_end: dt.date

    progression: float = 0.0
    signal_quality: float = 0.0
    volume: float = 0.0
    consistency: float = 0.0

    touches: int = 0
    replies: int = 0
    conversations: int = 0
    meetings: int = 0
    quality_touches: int = 0
    active_days: int = 0
    touch_target: float = 0.0
    active_day_target: float = 0.0
    weeks: float = 1.0
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return round(
            self.progression + self.signal_quality + self.volume + self.consistency, 1
        )

    @property
    def signal_quality_pct(self) -> float:
        return (self.quality_touches / self.touches * 100) if self.touches else 0.0

    @property
    def reply_rate(self) -> float:
        return (self.replies / self.touches) if self.touches else 0.0

    @property
    def meeting_rate(self) -> float:
        return (self.meetings / self.touches) if self.touches else 0.0

    def detail_json(self) -> str:
        return json.dumps(
            {
                "touches": self.touches,
                "replies": self.replies,
                "conversations": self.conversations,
                "meetings": self.meetings,
                "quality_touches": self.quality_touches,
                "active_days": self.active_days,
                "touch_target": self.touch_target,
                "active_day_target": self.active_day_target,
                "weeks": round(self.weeks, 2),
                "notes": self.notes,
            }
        )


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

_OUTBOUND_TYPES = {"email", "call", "linkedin", "meeting"}
_REPLY_OUTCOMES = {"reply", "replied", "responded"}
_CONVERSATION_OUTCOMES = {"conversation", "call_held", "discussion"}
_MEETING_OUTCOMES = {"meeting_booked", "meeting", "demo_booked"}


def _classify(activities: list[Activity], quality_tiers: set[str]) -> dict[str, int]:
    touches = replies = conversations = meetings = quality = 0
    days: set[dt.date] = set()

    for a in activities:
        outcome = (a.outcome or "").strip().lower()

        is_outbound = a.direction == "out" and a.type in _OUTBOUND_TYPES
        if is_outbound:
            touches += 1
            days.add(a.date)
            if (a.company_tier_at_time_of_contact or "") in quality_tiers:
                quality += 1

        if a.type == "reply" or a.direction == "in" or outcome in _REPLY_OUTCOMES:
            replies += 1
        if outcome in _CONVERSATION_OUTCOMES:
            conversations += 1
        if a.type == "meeting" or outcome in _MEETING_OUTCOMES:
            meetings += 1

    return {
        "touches": touches,
        "replies": replies,
        "conversations": conversations,
        "meetings": meetings,
        "quality": quality,
        "active_days": len(days),
    }


def compute_buddy_score(
    session: Session,
    *,
    period_type: PeriodType = "week",
    day: dt.date | None = None,
    owner: str | None = None,
    tenant_id: int | None = None,
) -> BuddyResult:
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    day = day or dt.datetime.now(dt.timezone.utc).date()
    start, end = bounds_for(period_type, day)

    b = cfg.buddy
    weights = b["components"]
    quality_tiers = {str(t) for t in b.get("quality_tiers", ["A", "B"])}

    stmt = select(Activity).where(
        Activity.tenant_id == tenant_id,
        Activity.date >= start,
        Activity.date <= end,
    )
    if owner:
        stmt = stmt.where(Activity.created_by == owner)
    activities = list(session.scalars(stmt))

    counts = _classify(activities, quality_tiers)

    days_in_period = (end - start).days + 1
    weeks = days_in_period / 7.0

    touch_target = float(b.get("weekly_touch_target", 15)) * weeks
    active_day_target = float(b.get("weekly_active_day_target", 4)) * weeks

    result = BuddyResult(
        period_type=period_type,
        period_start=start,
        period_end=end,
        touches=counts["touches"],
        replies=counts["replies"],
        conversations=counts["conversations"],
        meetings=counts["meetings"],
        quality_touches=counts["quality"],
        active_days=counts["active_days"],
        touch_target=round(touch_target, 1),
        active_day_target=round(active_day_target, 1),
        weeks=weeks,
    )

    # --- Volume: touches against your own target, capped ------------------------
    result.volume = round(
        float(weights["volume"]) * min(1.0, counts["touches"] / touch_target)
        if touch_target
        else 0.0,
        2,
    )

    # --- Consistency: active days, no gaps --------------------------------------
    result.consistency = round(
        float(weights["consistency"]) * min(1.0, counts["active_days"] / active_day_target)
        if active_day_target
        else 0.0,
        2,
    )

    # --- Signal quality: share of outreach to Tier A/B --------------------------
    if counts["touches"]:
        share = counts["quality"] / counts["touches"]
        result.signal_quality = round(float(weights["signal_quality"]) * share, 2)
    else:
        result.signal_quality = 0.0

    # --- Progression: replies -> conversations -> meetings ----------------------
    if counts["touches"]:
        t_reply = float(b.get("target_reply_rate", 0.15)) or 1.0
        t_conv = float(b.get("target_conversation_rate", 0.08)) or 1.0
        t_meet = float(b.get("target_meeting_rate", 0.05)) or 1.0

        reply_ratio = min(1.0, (counts["replies"] / counts["touches"]) / t_reply)
        conv_ratio = min(1.0, (counts["conversations"] / counts["touches"]) / t_conv)
        meet_ratio = min(1.0, (counts["meetings"] / counts["touches"]) / t_meet)

        # Meetings weighted heaviest — they are the terminal metric.
        blended = 0.30 * reply_ratio + 0.25 * conv_ratio + 0.45 * meet_ratio
        result.progression = round(float(weights["progression"]) * blended, 2)
    else:
        result.progression = 0.0
        result.notes.append("No outbound touches logged in this period.")

    if counts["touches"] and not counts["quality"]:
        result.notes.append(
            "None of this period's outreach went to a Tier A/B signal company — "
            "this is the failure mode the tool exists to prevent."
        )

    return result


# ---------------------------------------------------------------------------
# Persistence and trend
# ---------------------------------------------------------------------------


def save_snapshot(
    session: Session, result: BuddyResult, *, owner: str | None = None
) -> BuddySnapshot:
    cfg = get_config()
    owner = owner or cfg.user_name
    snapshot = session.scalar(
        select(BuddySnapshot).where(
            BuddySnapshot.tenant_id == cfg.tenant_id,
            BuddySnapshot.owner == owner,
            BuddySnapshot.period_type == result.period_type,
            BuddySnapshot.period_start == result.period_start,
        )
    )
    if snapshot is None:
        snapshot = BuddySnapshot(
            tenant_id=cfg.tenant_id,
            owner=owner,
            period_type=result.period_type,
            period_start=result.period_start,
        )
        session.add(snapshot)

    snapshot.period_end = result.period_end
    snapshot.score = result.score
    snapshot.progression = result.progression
    snapshot.signal_quality = result.signal_quality
    snapshot.volume = result.volume
    snapshot.consistency = result.consistency
    snapshot.detail = result.detail_json()
    session.flush()
    return snapshot


@dataclass
class Trend:
    direction: str  # up | down | flat | new
    delta: float
    baseline: float
    history: list[tuple[dt.date, float]]

    @property
    def arrow(self) -> str:
        return {"up": "▲", "down": "▼", "flat": "▬", "new": "•"}[self.direction]


def compute_trend(
    session: Session,
    *,
    period_type: PeriodType = "week",
    day: dt.date | None = None,
    owner: str | None = None,
    lookback: int | None = None,
) -> Trend:
    """Current period against the average of the preceding ones."""
    cfg = get_config()
    day = day or dt.datetime.now(dt.timezone.utc).date()
    lookback = lookback or int(cfg.buddy.get("trend_lookback_periods", 4))

    current = compute_buddy_score(session, period_type=period_type, day=day, owner=owner)

    history: list[tuple[dt.date, float]] = []
    cursor_start = current.period_start
    for _ in range(lookback):
        prev_start, _prev_end = previous_period(period_type, cursor_start)
        prior = compute_buddy_score(
            session, period_type=period_type, day=prev_start, owner=owner
        )
        history.append((prior.period_start, prior.score))
        cursor_start = prev_start

    history.reverse()
    scored = [s for _, s in history if s > 0]
    if not scored:
        return Trend("new", 0.0, 0.0, history + [(current.period_start, current.score)])

    baseline = sum(scored) / len(scored)
    delta = current.score - baseline
    direction = "up" if delta > 2 else "down" if delta < -2 else "flat"
    return Trend(
        direction, round(delta, 1), round(baseline, 1),
        history + [(current.period_start, current.score)],
    )


def rollups(
    session: Session, *, day: dt.date | None = None, owner: str | None = None
) -> dict[str, BuddyResult]:
    day = day or dt.datetime.now(dt.timezone.utc).date()
    return {
        p: compute_buddy_score(session, period_type=p, day=day, owner=owner)
        for p in ("week", "month", "quarter", "year")
    }


def refresh_snapshots(
    session: Session, *, day: dt.date | None = None, owner: str | None = None
) -> dict[str, float]:
    day = day or dt.datetime.now(dt.timezone.utc).date()
    out = {}
    for period_type, result in rollups(session, day=day, owner=owner).items():
        save_snapshot(session, result, owner=owner)
        out[period_type] = result.score
    return out
