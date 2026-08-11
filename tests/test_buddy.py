"""Buddy Score behaviour.

The load-bearing test here is test_spray_scores_worse_than_targeted. If it ever
fails, the score has started rewarding the behaviour the tool exists to replace.
"""

from __future__ import annotations

import datetime as dt

import pytest

from signal_engine.buddy import (
    bounds_for,
    compute_buddy_score,
    compute_trend,
    quarter_bounds,
    rollups,
    week_bounds,
)
from signal_engine.config import get_config
from signal_engine.ingest import create_signal, log_activity
from signal_engine.models import Activity
from signal_engine.scoring import rescore_company


def _signalled(session, make_company, name: str, today: dt.date):
    company = make_company(name)
    create_signal(
        session, company, type_key="manual_diy_production_at_capacity", detected_date=today
    )
    create_signal(
        session, company, type_key="new_lyo_capacity",
        detected_date=today - dt.timedelta(days=10),
    )
    rescore_company(session, company)
    assert company.tier in ("A", "B")
    return company


def _unsignalled(session, make_company, name: str):
    company = make_company(name)
    rescore_company(session, company)
    assert company.tier not in ("A", "B")
    return company


def _spread_over_week(n: int, today: dt.date) -> list[dt.date]:
    """n dates inside the current week, cycling across the days available so far."""
    start, _ = week_bounds(today)
    available = (today - start).days + 1
    return [start + dt.timedelta(days=i % available) for i in range(n)]


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_spray_scores_worse_than_targeted(session, make_company, today):
    """40 touches to unsignalled companies must score worse than 15 to signalled ones.

    Otherwise the score trains the seller back into the spray model that produced
    400-500 leads concentrated in 28 companies, most of them irrelevant.
    """
    spray_co = _unsignalled(session, make_company, "Castleford Pharma Group")
    for when in _spread_over_week(40, today):
        log_activity(session, company=spray_co, type="email", date=when)
    spray = compute_buddy_score(session, day=today).score

    # Clear the slate so the targeted run is measured on its own.
    for a in session.query(Activity).all():
        session.delete(a)
    session.flush()

    target_co = _signalled(session, make_company, "Northwind Diagnostics", today)
    for when in _spread_over_week(15, today):
        log_activity(session, company=target_co, type="email", date=when)
    targeted = compute_buddy_score(session, day=today).score

    assert targeted > spray, (
        f"40 unsignalled touches scored {spray}, 15 signalled touches scored "
        f"{targeted}. The score is rewarding spray."
    )


def test_signal_quality_is_zero_without_tiered_outreach(session, make_company, today):
    company = _unsignalled(session, make_company, "Meridian Life Sciences Holdings")
    for when in _spread_over_week(10, today):
        log_activity(session, company=company, type="email", date=when)

    result = compute_buddy_score(session, day=today)
    assert result.signal_quality == 0
    assert result.volume > 0
    assert any("Tier A/B" in note for note in result.notes)


def test_signal_quality_is_full_when_all_outreach_is_tiered(session, make_company, today):
    company = _signalled(session, make_company, "Halden Biologics", today)
    for when in _spread_over_week(6, today):
        log_activity(session, company=company, type="email", date=when)

    result = compute_buddy_score(session, day=today)
    assert result.signal_quality == pytest.approx(
        get_config().buddy["components"]["signal_quality"]
    )
    assert result.signal_quality_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def test_empty_period_scores_zero(session, today):
    result = compute_buddy_score(session, day=today)
    assert result.score == 0
    assert result.touches == 0


def test_volume_caps_at_the_target(session, make_company, today):
    company = _signalled(session, make_company, "Kestrel Molecular", today)
    target = get_config().buddy["weekly_touch_target"]
    for when in _spread_over_week(target * 4, today):
        log_activity(session, company=company, type="email", date=when)

    result = compute_buddy_score(session, day=today)
    assert result.volume == pytest.approx(get_config().buddy["components"]["volume"])


def test_meetings_drive_progression_harder_than_replies(session, make_company, today):
    company = _signalled(session, make_company, "Torvald Reagents", today)
    start, _ = week_bounds(today)

    log_activity(session, company=company, type="email", date=start, outcome="reply")
    reply_only = compute_buddy_score(session, day=today).progression

    log_activity(session, company=company, type="meeting", date=start, outcome="meeting_booked")
    with_meeting = compute_buddy_score(session, day=today).progression

    assert with_meeting > reply_only


def test_consistency_rewards_spread_not_bursts(session, make_company, today):
    """Steady beats bursts — same touch count, more days, higher consistency."""
    company = _signalled(session, make_company, "Steady Ltd", today)
    start, _ = week_bounds(today)
    available = (today - start).days + 1
    if available < 2:
        pytest.skip("Needs at least two elapsed days in the current week")

    for _ in range(4):
        log_activity(session, company=company, type="email", date=start)
    burst = compute_buddy_score(session, day=today).consistency

    for a in session.query(Activity).all():
        session.delete(a)
    session.flush()

    for i in range(4):
        log_activity(
            session, company=company, type="email",
            date=start + dt.timedelta(days=i % available),
        )
    spread = compute_buddy_score(session, day=today).consistency

    assert spread >= burst


def test_components_cannot_exceed_one_hundred(session, make_company, today):
    company = _signalled(session, make_company, "Maxed Out Ltd", today)
    for when in _spread_over_week(200, today):
        log_activity(session, company=company, type="meeting", date=when, outcome="meeting_booked")

    result = compute_buddy_score(session, day=today)
    assert result.score <= 100.0


# ---------------------------------------------------------------------------
# Periods, rollups and trend
# ---------------------------------------------------------------------------


def test_week_starts_on_monday():
    start, end = week_bounds(dt.date(2026, 8, 13))  # a Thursday
    assert start == dt.date(2026, 8, 10)
    assert end == dt.date(2026, 8, 16)
    assert start.weekday() == 0


def test_quarter_bounds_cover_whole_quarters():
    assert quarter_bounds(dt.date(2026, 5, 4)) == (dt.date(2026, 4, 1), dt.date(2026, 6, 30))
    assert quarter_bounds(dt.date(2026, 12, 31)) == (dt.date(2026, 10, 1), dt.date(2026, 12, 31))


def test_unknown_period_type_is_rejected():
    with pytest.raises(ValueError, match="Unknown period type"):
        bounds_for("fortnight", dt.date(2026, 1, 1))


def test_targets_scale_with_period_length(session, today):
    weekly = compute_buddy_score(session, period_type="week", day=today)
    yearly = compute_buddy_score(session, period_type="year", day=today)
    assert yearly.touch_target > weekly.touch_target * 40


def test_rollups_cover_all_four_periods(session, today):
    result = rollups(session, day=today)
    assert set(result) == {"week", "month", "quarter", "year"}


def test_trend_reports_new_when_there_is_no_history(session, make_company, today):
    company = _signalled(session, make_company, "Brand New Ltd", today)
    log_activity(session, company=company, type="email", date=today)
    assert compute_trend(session, day=today).direction == "new"
