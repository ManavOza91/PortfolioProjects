"""A small worked example so the shape of the thing is visible before real data exists.

Every company here is fictional. The *patterns* are drawn from the eleven companies
that actually engaged: a DIY producer at capacity, a 2024 signal converting in 2026,
a regulatory-then-funding compound, and two large corporates contacted cold with no
signal that went silent — the failure mode this tool exists to prevent.

Loading it twice is safe; it matches on company name.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_config
from .ingest import create_signal, find_or_create_company, log_activity, upsert_contact
from .models import Activity
from .scoring import rescore_all


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _d(days_ago: int) -> dt.date:
    return _today() - dt.timedelta(days=days_ago)


def _this_week(offset: int) -> dt.date:
    """A date inside the current Monday-start week, never in the future.

    The current-week touches are placed relative to the week rather than as a fixed
    number of days ago, so the demo shows a live Buddy Score whichever day it is run.
    """
    today = _today()
    week_start = today - dt.timedelta(days=today.weekday())
    return min(week_start + dt.timedelta(days=offset), today)


# Touches inside the current week, so the Buddy Score has something to show.
# (company name, weekday offset, type, direction, outcome)
THIS_WEEK = [
    ("Northwind Diagnostics", 0, "email", "out", "reply"),
    ("Northwind Diagnostics", 2, "call", "out", "meeting_booked"),
    ("Halden Biologics", 0, "linkedin", "out", "conversation"),
    ("Halden Biologics", 3, "email", "out", None),
    ("Ambervale Animal Health", 1, "email", "out", "reply"),
    ("Kestrel Molecular", 1, "linkedin", "out", None),
    ("Torvald Reagents", 2, "email", "out", None),
    ("Perrin Therapeutics", 3, "email", "out", None),
    ("Castleford Pharma Group", 4, "email", "out", "no_response"),
]


DEMO = [
    # (name, country, segment, [(days_ago, type_key, summary, source)], contacts, activity)
    {
        "name": "Northwind Diagnostics",
        "country": "United Kingdom",
        "segment": "IVD manufacturer",
        "signals": [
            (14, "manual_diy_production_at_capacity",
             "Building beads on a home-made rig and cannot keep up with demand; "
             "evaluating a machine next financial year.", "event"),
            (30, "conference_presentation", "Exhibited at LyoTalk.", "event"),
        ],
        "contacts": [("Assay development lead", None)],
        "activity": [(10, "email", "out", "reply"), (4, "call", "out", "conversation")],
    },
    {
        "name": "Kestrel Molecular",
        "country": "United States",
        "segment": "Point-of-care diagnostics",
        "signals": [
            (48, "regulatory_submission", "510(k) submitted for their respiratory panel.", "auto"),
            (20, "funding_scale_up",
             "Series B raised, stated use of proceeds is manufacturing scale-up.", "auto"),
            (75, "job_posting_relevant_science", "Two open reagent formulation roles.", "auto"),
        ],
        "contacts": [],
        "activity": [(8, "email", "out", "no_response")],
    },
    {
        "name": "Halden Biologics",
        "country": "Denmark",
        "segment": "CDMO",
        "signals": [
            (620, "competitor_dissatisfaction",
             "Bought a competitor unit in 2024, unhappy with support; now asking about "
             "a second instrument.", "manual"),
            (25, "new_lyo_capacity", "Commissioning a new fill-finish line.", "auto"),
        ],
        "contacts": [("Lyophilisation specialist", None)],
        "activity": [(15, "linkedin", "out", "reply"), (6, "meeting", "out", "meeting_booked")],
    },
    {
        "name": "Torvald Reagents",
        "country": "Sweden",
        "segment": "Reagent supplier",
        "signals": [
            (200, "format_shift_to_bead",
             "Moving their flagship kit from vial format to unit-dose beads.", "manual"),
            (190, "multi_product_pipeline",
             "Four assay families disclosed across the portfolio.", "manual"),
        ],
        "contacts": [],
        "activity": [],
    },
    {
        "name": "Ambervale Animal Health",
        "country": "Ireland",
        "segment": "Veterinary diagnostics",
        "signals": [
            (35, "manual_diy_production_at_capacity",
             "Producing lyophilised beads manually for a companion-animal panel and "
             "hitting a throughput ceiling.", "manual"),
            (60, "iso13485_facility_expansion", "New cleanroom suite opened.", "auto"),
        ],
        "contacts": [],
        # Present precisely because the old system's "veterinary" exclusion would
        # have blocked a company of exactly this shape that actually engaged.
        "activity": [(3, "email", "out", None)],
    },
    {
        "name": "Perrin Therapeutics",
        "country": "Switzerland",
        "segment": "Biotech",
        "signals": [
            (110, "phase_1_parenteral", "Lead injectable biologic entered Phase 1.", "auto"),
            (400, "grant_award", "EU Horizon award for formulation development.", "auto"),
        ],
        "contacts": [],
        "activity": [],
    },
    {
        "name": "Castleford Pharma Group",
        "country": "United Kingdom",
        "segment": "Large pharma",
        "signals": [],  # deliberately none
        "contacts": [],
        # Contacted cold, no signal, went silent. Two of eleven looked exactly like this.
        "activity": [(45, "email", "out", "no_response"), (38, "email", "out", "no_response")],
    },
    {
        "name": "Meridian Life Sciences Holdings",
        "country": "United States",
        "segment": "Large pharma",
        "signals": [],
        "contacts": [],
        "activity": [(52, "linkedin", "out", "no_response")],
    },
]


def load_demo(session: Session) -> str:
    cfg = get_config()
    created = 0
    signals = 0

    for spec in DEMO:
        company, was_new = find_or_create_company(
            session,
            name=spec["name"],
            country=spec["country"],
            segment=spec["segment"],
            status="watchlist",
            created_by="demo",
        )
        if was_new:
            created += 1

        existing_summaries = {s.parsed_summary for s in company.signals}
        for days_ago, type_key, summary, source in spec["signals"]:
            if summary in existing_summaries:
                continue
            create_signal(
                session,
                company,
                type_key=type_key,
                source=source,
                raw_text=summary,
                parsed_summary=summary,
                confidence=0.92,
                detected_date=_d(days_ago),
                created_by="demo",
            )
            signals += 1

        for title, email in spec["contacts"]:
            upsert_contact(
                session, company, job_title=title, email=email, source="demo", created_by="demo"
            )

    session.flush()
    rescore_all(session)

    # Activity is logged after scoring so each touch records the tier as it stands.
    touches = 0
    for spec in DEMO:
        company, _ = find_or_create_company(session, name=spec["name"], created_by="demo")
        already = {
            (a.date, a.type)
            for a in session.scalars(
                select(Activity).where(Activity.company_id == company.id)
            )
        }
        for days_ago, kind, direction, outcome in spec["activity"]:
            if (_d(days_ago), kind) in already:
                continue
            log_activity(
                session,
                company=company,
                type=kind,
                direction=direction,
                date=_d(days_ago),
                outcome=outcome,
                created_by=cfg.user_name,
            )
            touches += 1

    for name, offset, kind, direction, outcome in THIS_WEEK:
        company, _ = find_or_create_company(session, name=name, created_by="demo")
        when = _this_week(offset)
        already = {
            (a.date, a.type)
            for a in session.scalars(
                select(Activity).where(Activity.company_id == company.id)
            )
        }
        if (when, kind) in already:
            continue
        log_activity(
            session,
            company=company,
            type=kind,
            direction=direction,
            date=when,
            outcome=outcome,
            created_by=cfg.user_name,
        )
        touches += 1

    session.flush()
    return (
        f"Demo loaded: {created} companies, {signals} signals, {touches} logged touches.\n"
        "Two companies have no signal at all and were contacted cold — they sit at the "
        "bottom of the queue and drag the Buddy Score's signal-quality component down, "
        "which is the point."
    )
