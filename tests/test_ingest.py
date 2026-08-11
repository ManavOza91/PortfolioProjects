"""Note ingestion, with the language model stubbed out.

The behaviours worth protecting are about what happens when parsing finds nothing,
or cannot run at all. A human observation is the highest-converting signal this
system has; it must survive both cases.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from signal_engine.ingest import (
    find_or_create_company,
    ingest_note,
    normalise_domain,
    normalise_name,
)
from signal_engine.models import Contact, Signal

from .conftest import FakeParsedSignal


def test_nothing_scoreable_stores_the_note_without_inventing_a_score(
    session, fake_outcome
):
    """'If a note contains nothing scoreable, the record enters unscored rather
    than receiving a fabricated number.'"""
    result = ingest_note(
        session,
        note="Nice people, good coffee, nothing much to report.",
        company_name="Pleasant Ltd",
        outcome=fake_outcome(
            company_name="Pleasant Ltd", signals=(), reasoning="No buying signal present."
        ),
    )

    assert result.unscored is True
    assert result.company.current_score == 0
    assert result.company.status == "watchlist"

    stored = session.scalars(select(Signal)).all()
    assert len(stored) == 1
    assert stored[0].status == "unscored"
    assert stored[0].type_key is None
    assert "good coffee" in stored[0].raw_text


def test_parse_failure_still_saves_the_raw_note(session, fake_outcome):
    result = ingest_note(
        session,
        note="Met at the show, they're building beads by hand and are maxed out.",
        company_name="Northwind Diagnostics",
        outcome=fake_outcome(ok=False, error="APIConnectionError: network unreachable"),
    )

    assert result.parse_ok is False
    stored = session.scalars(select(Signal)).all()
    assert len(stored) == 1
    assert stored[0].status == "unscored"
    assert "building beads by hand" in stored[0].raw_text
    assert "could not parse" in result.message()


def test_a_parsed_signal_scores_and_promotes_the_company(session, fake_outcome):
    result = ingest_note(
        session,
        note="Doing manual bead production, evaluating a machine next year.",
        company_name="Northwind Diagnostics",
        outcome=fake_outcome(
            company_name="Northwind Diagnostics",
            signals=[
                FakeParsedSignal(
                    "manual_diy_production_at_capacity",
                    summary="Manual bead production at capacity; evaluating next year.",
                    confidence=0.95,
                )
            ],
        ),
    )

    assert result.unscored is False
    assert result.company.status == "active"
    assert result.company.current_score > 0
    assert result.scored_signals
    assert result.scored_signals[0].raw_text.startswith("Doing manual bead production")


def test_low_confidence_goes_to_review_not_to_scoring(session, fake_outcome):
    result = ingest_note(
        session,
        note="Someone thought they might be looking at beads, not sure.",
        company_name="Hearsay Ltd",
        outcome=fake_outcome(
            company_name="Hearsay Ltd",
            signals=[FakeParsedSignal("format_shift_to_bead", confidence=0.2)],
        ),
    )

    assert result.review_signals
    assert not result.scored_signals
    assert result.company.current_score == 0


def test_two_parsed_signals_both_land(session, fake_outcome):
    result = ingest_note(
        session,
        note="510(k) submitted, and they've just raised to scale manufacturing.",
        company_name="Kestrel Molecular",
        outcome=fake_outcome(
            company_name="Kestrel Molecular",
            signals=[
                FakeParsedSignal("regulatory_submission", confidence=0.9),
                FakeParsedSignal("funding_scale_up", confidence=0.9),
            ],
        ),
    )
    assert len(result.scored_signals) == 2
    assert result.company.current_score > 0


def test_a_note_with_no_company_creates_nothing(session, fake_outcome):
    result = ingest_note(
        session,
        note="Someone somewhere is doing something.",
        outcome=fake_outcome(company_name=None, signals=()),
    )
    assert result.company is None
    assert session.scalars(select(Signal)).all() == []


def test_contact_details_create_an_optional_contact(session, fake_outcome):
    result = ingest_note(
        session,
        note="Spoke to their formulation lead about lyo capacity.",
        company_name="Halden Biologics",
        contact_name="A Person",
        contact_title="Lyophilisation specialist",
        outcome=fake_outcome(
            company_name="Halden Biologics",
            signals=[FakeParsedSignal("new_lyo_capacity", confidence=0.9)],
        ),
    )
    assert result.contact is not None
    assert result.contact.full_name == "A Person"
    assert session.scalar(select(Contact)).company_id == result.company.id


def test_notes_about_the_same_company_accumulate_on_one_record(session, fake_outcome):
    for note, key in [
        ("510(k) submitted.", "regulatory_submission"),
        ("Raised to scale manufacturing.", "funding_scale_up"),
    ]:
        ingest_note(
            session,
            note=note,
            company_name="Kestrel Molecular",
            outcome=fake_outcome(
                company_name="Kestrel Molecular",
                signals=[FakeParsedSignal(key, confidence=0.9)],
            ),
        )

    from signal_engine.models import Company

    companies = session.scalars(select(Company)).all()
    assert len(companies) == 1
    assert len(companies[0].signals) == 2
    assert companies[0].current_score > 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("Acme Diagnostics Ltd", "ACME Diagnostics"),
        ("Northwind Diagnostics, Inc.", "northwind diagnostics inc"),
        ("The Kestrel Group GmbH", "Kestrel"),
    ],
)
def test_company_names_match_across_legal_suffixes_and_case(a, b):
    assert normalise_name(a) == normalise_name(b)


def test_different_companies_do_not_collide():
    assert normalise_name("Kestrel Molecular") != normalise_name("Kestrel Diagnostics")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.Example.com/about", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("http://example.com", "example.com"),
        (None, None),
        ("", None),
    ],
)
def test_domains_normalise(raw, expected):
    assert normalise_domain(raw) == expected


def test_matching_on_domain_beats_a_different_name(session):
    first, _ = find_or_create_company(session, name="Acme Diagnostics", domain="acme.com")
    second, created = find_or_create_company(
        session, name="Acme Diagnostics Limited", domain="https://www.acme.com"
    )
    assert created is False
    assert second.id == first.id


def test_existing_detail_is_never_overwritten_by_a_blank(session):
    first, _ = find_or_create_company(
        session, name="Acme", domain="acme.com", country="United Kingdom"
    )
    again, _ = find_or_create_company(session, name="Acme", segment="IVD")
    assert again.country == "United Kingdom"
    assert again.segment == "IVD"
