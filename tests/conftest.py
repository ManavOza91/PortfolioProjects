from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

import pytest

# Point every test at a throwaway database before anything imports the engine.
_TMP = Path(tempfile.mkdtemp(prefix="signal-engine-tests-"))
os.environ["SIGNAL_ENGINE_DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"

from signal_engine import config as config_module  # noqa: E402
from signal_engine import db as db_module  # noqa: E402
from signal_engine.models import Base  # noqa: E402
from signal_engine.seed import seed_taxonomy  # noqa: E402


@pytest.fixture()
def session():
    """A fresh, fully migrated, taxonomy-seeded database per test."""
    config_module.reset_caches()
    db_module.reset_engine()

    path = _TMP / "test.db"
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()

    engine = db_module.get_engine()
    Base.metadata.create_all(engine)

    factory = db_module.get_session_factory()
    s = factory()
    seed_taxonomy(s)
    s.commit()
    try:
        yield s
    finally:
        s.close()
        db_module.reset_engine()


@pytest.fixture()
def today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


@pytest.fixture()
def make_company(session):
    from signal_engine.ingest import find_or_create_company

    def _make(name: str, **kw):
        company, _ = find_or_create_company(session, name=name, **kw)
        return company

    return _make


class FakeParsedSignal:
    def __init__(self, type_key, summary="", confidence=0.9, product_fit="both", detected_date=None):
        self.type_key = type_key
        self.summary = summary
        self.confidence = confidence
        self.product_fit = product_fit
        self.detected_date = detected_date
        self.evidence = summary
        self.timeline = None


class FakeParsedEntry:
    def __init__(self, company_name=None, signals=(), reasoning="", **kw):
        self.company_name = company_name
        self.company_domain = kw.get("company_domain")
        self.company_country = kw.get("company_country")
        self.company_segment = kw.get("company_segment")
        self.contact_full_name = kw.get("contact_full_name")
        self.contact_job_title = kw.get("contact_job_title")
        self.contact_email = kw.get("contact_email")
        self.contact_linkedin_url = kw.get("contact_linkedin_url")
        self.contact_persona = kw.get("contact_persona")
        self.signals = list(signals)
        self.nothing_scoreable = not signals
        self.reasoning = reasoning


@pytest.fixture()
def fake_outcome():
    """Build a ParseOutcome without touching the network."""
    from signal_engine.llm.client import ParseOutcome

    def _make(company_name=None, signals=(), reasoning="", ok=True, error=None):
        return ParseOutcome(
            ok=ok,
            data=FakeParsedEntry(company_name, signals, reasoning) if ok else None,
            error=error,
        )

    return _make
