"""Manual signal entry — the primary path, with no API key and no anthropic package.

The app must be fully usable on the form alone. Parsing is an optional extra.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from signal_engine.ingest import create_signal, find_or_create_company
from signal_engine.models import Signal
from signal_engine.scoring import rescore_company


def _add(session, company_name, type_key, *, days_ago=0, timeline=None, notes=None):
    """What the /add/signal route does, minus the HTTP layer."""
    company, _ = find_or_create_company(session, name=company_name)
    spec = next(
        t for t in session.scalars(select(__import__(
            "signal_engine.models", fromlist=["SignalType"]).SignalType))
        if t.key == type_key
    )
    signal = create_signal(
        session,
        company,
        type_key=spec.key,
        source="manual",
        raw_text=notes,
        parsed_summary=notes or spec.label,
        confidence=0.9,
        detected_date=dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days_ago),
        product_fit=spec.product_fit,
        timeline=timeline,
    )
    return company, signal, rescore_company(session, company)


def test_a_manually_entered_signal_scores_immediately(session):
    company, signal, breakdown = _add(
        session, "Vantage Biosciences", "manual_diy_production_at_capacity"
    )
    assert signal.status == "scored"
    assert signal.weight == 30
    assert breakdown.score == pytest.approx(30.0)
    assert breakdown.tier == "B"
    assert company.status == "active"


def test_the_weight_comes_from_the_taxonomy_not_the_user(session):
    _, signal, _ = _add(session, "Vantage", "conference_presentation")
    assert signal.weight == 8  # the taxonomy's value, not anything the form supplied


def test_timeline_is_stored_as_its_own_field(session):
    _, signal, _ = _add(
        session, "Vantage", "manual_diy_production_at_capacity",
        timeline="evaluating next year",
    )
    assert signal.timeline == "evaluating next year"

    reloaded = session.scalar(select(Signal).where(Signal.id == signal.id))
    assert reloaded.timeline == "evaluating next year"


def test_two_manual_signals_compound_exactly_like_parsed_ones(session):
    _add(session, "Kestrel Molecular", "regulatory_submission", days_ago=40)
    _, _, breakdown = _add(session, "Kestrel Molecular", "funding_scale_up", days_ago=5)

    assert breakdown.compounding_applied is True
    assert breakdown.multiplier == pytest.approx(1.30)
    assert breakdown.score == pytest.approx(breakdown.raw_sum * 1.30)
    # Two mid-weight signals reach B, not A. A needs a heavier pair or a third.
    assert breakdown.tier == "B"


def test_backdating_applies_decay(session):
    _, _, fresh = _add(session, "Fresh Ltd", "new_lyo_capacity", days_ago=0)
    _, _, old = _add(session, "Old Ltd", "new_lyo_capacity", days_ago=365)
    assert old.score < fresh.score
    assert old.score > 0  # never zero


def test_manual_entry_never_touches_the_language_model(session, monkeypatch):
    """Belt and braces: if anything reached for the API here, this would fail."""
    import signal_engine.llm.client as client_module

    def explode():
        raise AssertionError("manual entry must not call the language model")

    monkeypatch.setattr(client_module, "get_client", explode)
    _, _, breakdown = _add(session, "No API Ltd", "new_lyo_capacity")
    assert breakdown.score > 0


def test_the_app_imports_without_the_anthropic_package():
    """`anthropic` is an optional extra. Importing the web app must not need it."""
    import importlib

    module = importlib.import_module("signal_engine.web.app")
    assert module.app is not None

    routes = {getattr(r, "path", None) for r in module.app.routes}
    assert "/add/signal" in routes
