"""Contacts must be deletable wholesale without touching anything else.

This is the property that makes a contacts-free commercial edition viable and keeps
GDPR obligations minimal. It is a schema guarantee, so it gets a test rather than a
paragraph in a README.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from signal_engine.buddy import compute_buddy_score, week_bounds
from signal_engine.ingest import (
    create_signal,
    log_activity,
    purge_personal_data,
    upsert_contact,
)
from signal_engine.models import Activity, Contact
from signal_engine.reporting import ranked_queue, signal_conversion
from signal_engine.scoring import rescore_company


@pytest.fixture()
def populated(session, make_company, today):
    company = make_company("Northwind Diagnostics", country="United Kingdom")
    create_signal(
        session, company, type_key="manual_diy_production_at_capacity", detected_date=today
    )
    create_signal(
        session, company, type_key="conference_presentation",
        detected_date=today - dt.timedelta(days=20),
    )
    rescore_company(session, company)

    contact = upsert_contact(
        session,
        company,
        full_name="A Person",
        job_title="Assay development lead",
        email="a.person@example.com",
        linkedin_url="https://linkedin.com/in/example",
    )
    start, _ = week_bounds(today)
    for i in range(4):
        log_activity(
            session,
            company=company,
            contact=contact,
            type="email",
            date=min(start + dt.timedelta(days=i), today),
            outcome="reply" if i == 0 else None,
        )
    session.flush()
    return company, contact


def test_purge_deletes_every_contact(session, populated):
    result = purge_personal_data(session)
    assert result.contacts_deleted == 1
    assert session.scalar(select(Contact).limit(1)) is None


def test_purge_keeps_activity_history(session, populated):
    before = len(session.query(Activity).all())
    result = purge_personal_data(session)
    after = session.query(Activity).all()

    assert len(after) == before
    assert result.activity_rows_kept == before
    assert result.activity_rows_detached == before
    # Detached from the person, still attached to the company.
    assert all(a.contact_id is None for a in after)
    assert all(a.company_id is not None for a in after)


def test_purge_leaves_scoring_untouched(session, populated):
    company, _ = populated
    score_before = company.current_score
    tier_before = company.tier

    purge_personal_data(session)
    rescore_company(session, company)

    assert company.current_score == pytest.approx(score_before)
    assert company.tier == tier_before


def test_purge_leaves_the_buddy_score_working(session, populated, today):
    before = compute_buddy_score(session, day=today)
    assert before.touches > 0

    purge_personal_data(session)
    after = compute_buddy_score(session, day=today)

    assert after.touches == before.touches
    assert after.score == pytest.approx(before.score)
    assert after.signal_quality == pytest.approx(before.signal_quality)


def test_purge_leaves_reporting_working(session, populated):
    queue_before = ranked_queue(session)
    conversion_before = signal_conversion(session)

    purge_personal_data(session)

    queue_after = ranked_queue(session)
    conversion_after = signal_conversion(session)

    assert [r.company.id for r in queue_after] == [r.company.id for r in queue_before]
    assert [r.touches for r in conversion_after] == [r.touches for r in conversion_before]
    assert queue_after[0].contact_count == 0


def test_deleting_one_contact_detaches_rather_than_cascades(session, populated):
    """The ON DELETE SET NULL rule has to actually be enforced, not just declared.

    SQLite ignores foreign keys unless PRAGMA foreign_keys is on, so this test is
    really checking that db.py turns it on.
    """
    company, contact = populated
    session.delete(contact)
    session.flush()

    remaining = session.query(Activity).all()
    assert len(remaining) == 4
    assert all(a.contact_id is None for a in remaining)
    assert all(a.company_id == company.id for a in remaining)


def test_the_system_runs_with_no_contacts_at_all(session, make_company, today):
    """Contacts are entirely optional — companies and signals are enough."""
    company = make_company("No Contacts Ltd")
    create_signal(session, company, type_key="new_lyo_capacity", detected_date=today)
    rescore_company(session, company)
    log_activity(session, company=company, type="email", date=today)

    assert ranked_queue(session)[0].company.id == company.id
    assert compute_buddy_score(session, day=today).touches == 1
    assert session.scalar(select(Contact).limit(1)) is None
