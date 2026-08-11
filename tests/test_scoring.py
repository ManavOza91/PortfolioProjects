"""Scoring rules: slow decay, structural exemption, compounding, tiers.

These encode findings from real deals. If one of them starts failing, the tool has
stopped matching the market it was built for.
"""

from __future__ import annotations

import datetime as dt

import pytest

from signal_engine.config import get_config
from signal_engine.ingest import create_signal
from signal_engine.scoring import (
    assign_tier,
    compounding_multiplier,
    decay_factor,
    rescore_company,
    score_company,
)


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def test_fresh_signal_holds_full_weight(today):
    factor, expired = decay_factor(today, decays=True, as_of=today)
    assert factor == pytest.approx(1.0)
    assert expired is False


def test_decay_is_about_ten_percent_per_quarter(today):
    one_quarter = today - dt.timedelta(days=91)
    factor, _ = decay_factor(one_quarter, decays=True, as_of=today)
    assert factor == pytest.approx(0.90, abs=0.01)


def test_six_month_old_signal_is_not_dead(today):
    """Finding 2: a six-month-old signal is still worth most of its weight.

    Standard sales-tool decay would have written this off. Two real deals sat in
    pipeline 18-24 months before closing.
    """
    six_months = today - dt.timedelta(days=182)
    factor, _ = decay_factor(six_months, decays=True, as_of=today)
    assert factor > 0.75


def test_decay_floors_and_never_reaches_zero(today):
    """At 10%/quarter the floor is reached at roughly two and a quarter years,
    and nothing ever goes below it — however old."""
    floor = get_config().decay["floor"]
    for years in (3, 5, 20, 50):
        factor, _ = decay_factor(
            today - dt.timedelta(days=365 * years), decays=True, as_of=today
        )
        assert factor == pytest.approx(floor)
        assert factor > 0


def test_two_year_old_signal_still_scores(today, make_company, session):
    """A company lost to a competitor in 2024 re-engaged in 2026 wanting a second unit."""
    company = make_company("Halden Biologics")
    create_signal(
        session,
        company,
        type_key="competitor_dissatisfaction",
        detected_date=today - dt.timedelta(days=730),
        confidence=0.9,
    )
    breakdown = score_company(session, company)
    # Still worth ~43% of its original 28 points after two years, not zero.
    assert breakdown.raw_sum == pytest.approx(28 * 0.43, abs=0.5)
    assert breakdown.tier in ("B", "C")


def test_structural_signals_do_not_decay(today):
    """A disclosed multi-product pipeline does not stop being multi-product."""
    ancient = today - dt.timedelta(days=365 * 4)
    factor, expired = decay_factor(ancient, decays=False, as_of=today)
    assert factor == 1.0
    assert expired is False


def test_expiry_drops_to_floor_not_to_zero(today):
    factor, expired = decay_factor(
        today - dt.timedelta(days=10),
        decays=True,
        expiry_date=today - dt.timedelta(days=1),
        as_of=today,
    )
    assert expired is True
    assert factor == pytest.approx(get_config().decay["floor"])
    assert factor > 0


def test_future_dated_signal_does_not_exceed_full_weight(today):
    factor, _ = decay_factor(today + dt.timedelta(days=30), decays=True, as_of=today)
    assert factor == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Compounding
# ---------------------------------------------------------------------------


def test_two_independent_signals_in_window_compound(session, make_company, today):
    """Observed pattern: regulatory submission -> funding inside 7 months."""
    company = make_company("Kestrel Molecular")
    create_signal(
        session, company, type_key="regulatory_submission",
        detected_date=today - dt.timedelta(days=50),
    )
    create_signal(
        session, company, type_key="funding_scale_up",
        detected_date=today - dt.timedelta(days=20),
    )
    breakdown = score_company(session, company)
    assert breakdown.compounding_applied is True
    assert breakdown.multiplier == pytest.approx(1.30)
    assert breakdown.score == pytest.approx(breakdown.raw_sum * 1.30)


def test_same_type_twice_does_not_compound(session, make_company, today):
    """Two job postings are one story, not two independent signals."""
    company = make_company("Torvald Reagents")
    for offset in (10, 30):
        create_signal(
            session, company, type_key="job_posting_relevant_science",
            detected_date=today - dt.timedelta(days=offset),
        )
    breakdown = score_company(session, company)
    assert breakdown.compounding_applied is False
    assert breakdown.multiplier == 1.0


def test_signals_outside_the_window_do_not_compound(session, make_company, today):
    company = make_company("Perrin Therapeutics")
    create_signal(
        session, company, type_key="regulatory_submission",
        detected_date=today - dt.timedelta(days=400),
    )
    create_signal(
        session, company, type_key="funding_scale_up",
        detected_date=today - dt.timedelta(days=5),
    )
    breakdown = score_company(session, company)
    assert breakdown.compounding_applied is False


def test_single_signal_never_compounds():
    from signal_engine.scoring import Contribution

    one = [
        Contribution(1, "a", "A", 30, 1.0, 30, dt.date(2026, 1, 1), 0, True, False, "manual", "B", None)
    ]
    multiplier, reason = compounding_multiplier(one)
    assert multiplier == 1.0
    assert reason is None


# ---------------------------------------------------------------------------
# Tiers, qualifiers and exclusions
# ---------------------------------------------------------------------------


def test_tiers_follow_configured_thresholds():
    cfg = get_config()
    top = cfg.tiers[0]
    assert assign_tier(top.min_score) == top.name
    assert assign_tier(top.min_score + 100) == top.name
    assert assign_tier(0) == cfg.tiers[-1].name


def test_the_heaviest_signal_alone_reaches_quality_outreach():
    """Guard on the taxonomy/tier calibration.

    The Buddy Score only counts outreach to Tier A/B companies as quality. If the
    B threshold is set above the heaviest weight in the taxonomy, then no single
    signal can ever reach B — and acting on the single best signal available scores
    as spray. That is exactly backwards, and it is an easy mistake to make by
    editing either config.yaml or data/taxonomy.yaml in isolation.

    If this fails: lower the Tier B threshold, or raise a signal weight.
    """
    from signal_engine.config import get_taxonomy

    cfg = get_config()
    heaviest = max(t.base_weight for t in get_taxonomy().types)
    quality_tiers = set(cfg.buddy.get("quality_tiers", ["A", "B"]))

    tier = assign_tier(heaviest)
    assert tier in quality_tiers, (
        f"The heaviest signal in the taxonomy is {heaviest:g} points, which lands in "
        f"Tier {tier}. Quality outreach requires {sorted(quality_tiers)}, so acting on "
        "the single strongest signal available would score as low-quality outreach."
    )


def test_funding_alone_does_not_reach_the_top_tier(session, make_company, today):
    """Finding 3: funding is a qualifier, not a trigger.

    None of the eleven companies that engaged bought because they had just raised.
    """
    company = make_company("Freshly Funded Inc")
    create_signal(session, company, type_key="funding_scale_up", detected_date=today)
    breakdown = score_company(session, company)
    assert breakdown.tier != "A"


def test_diy_at_capacity_alone_reaches_a_high_tier(session, make_company, today):
    """Finding 1: the highest-converting signal must surface on its own."""
    company = make_company("Northwind Diagnostics")
    create_signal(
        session, company, type_key="manual_diy_production_at_capacity", detected_date=today
    )
    breakdown = score_company(session, company)
    assert breakdown.tier in ("A", "B")


def test_no_category_is_excluded(session, make_company, today):
    """The old system excluded 'veterinary' and would have blocked a real engager."""
    company = make_company(
        "Ambervale Animal Health", segment="Veterinary diagnostics", country="Ireland"
    )
    create_signal(
        session, company, type_key="manual_diy_production_at_capacity", detected_date=today
    )
    create_signal(
        session, company, type_key="iso13485_facility_expansion",
        detected_date=today - dt.timedelta(days=30),
    )
    breakdown = score_company(session, company)
    assert breakdown.score > 0
    assert breakdown.tier == "A"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_rescoring_is_idempotent(session, make_company, today):
    company = make_company("Steady State Ltd")
    create_signal(session, company, type_key="new_lyo_capacity", detected_date=today)

    first = rescore_company(session, company).score
    second = rescore_company(session, company).score
    assert first == pytest.approx(second)


def test_a_signal_promotes_a_company_off_the_watchlist(session, make_company, today):
    company = make_company("Imported Co", status="watchlist")
    assert company.status == "watchlist"

    create_signal(session, company, type_key="new_lyo_capacity", detected_date=today)
    rescore_company(session, company)
    assert company.status == "active"


def test_a_watchlist_company_with_no_signal_stays_unscored(session, make_company):
    company = make_company("Just A Logo Ltd", status="watchlist")
    rescore_company(session, company)
    assert company.current_score == 0
    assert company.status == "watchlist"


def test_unscored_and_review_signals_contribute_nothing(session, make_company, today):
    company = make_company("Held Back Ltd")
    create_signal(
        session, company, type_key="new_lyo_capacity",
        detected_date=today, status="review",
    )
    create_signal(
        session, company, type_key="regulatory_submission",
        detected_date=today, status="unscored",
    )
    breakdown = score_company(session, company)
    assert breakdown.score == 0
    assert breakdown.signal_count == 0


def test_reweighting_the_taxonomy_does_not_rewrite_history(session, make_company, today):
    """A signal snapshots its weight, so changing a weight tomorrow does not
    silently restate what a company was worth last year."""
    from sqlalchemy import select

    from signal_engine.models import SignalType

    company = make_company("Historic Ltd")
    signal = create_signal(session, company, type_key="new_lyo_capacity", detected_date=today)
    original = signal.weight

    spec = session.scalar(select(SignalType).where(SignalType.key == "new_lyo_capacity"))
    spec.base_weight = 5.0
    session.flush()

    assert signal.weight == original
    assert score_company(session, company).raw_sum == pytest.approx(original)
