"""Automatic detection: matching, the review gate, and dedupe.

No test here touches the network. Detectors are fed canned payloads, because the
thing worth protecting is the judgement in the runner, not httpx.
"""

from __future__ import annotations

import datetime as dt

import pytest

from signal_engine.detect import base, news, openfda, runner, sbir
from signal_engine.detect.base import Detection, classify_by_keyword, match_company_name

TODAY = dt.date(2026, 8, 12)
SINCE = dt.date(2026, 1, 1)


# ---------------------------------------------------------------------------
# Company matching
# ---------------------------------------------------------------------------


def test_exact_match_ignores_legal_suffix_and_case():
    result = match_company_name("HOLOGIC, INC.", "Hologic")
    assert result.matched
    assert result.kind == "exact"


def test_partial_match_never_clears_the_auto_bar():
    """"Northwind Bio" inside "Northwind Bio Diagnostics" is a guess, not a fact."""
    from signal_engine.config import get_config

    result = match_company_name("Northwind Bio Diagnostics", "Northwind Bio")
    assert result.matched
    assert result.kind == "partial"
    assert result.confidence < get_config().auto_review_threshold


def test_a_match_carried_only_by_generic_words_is_rejected():
    """Otherwise every "... Diagnostics" matches every other "... Diagnostics"."""
    assert not match_company_name("Diagnostics", "Northwind Diagnostics").matched
    assert not match_company_name("Life Sciences", "Redcliff Life Sciences").matched


def test_unrelated_names_do_not_match():
    assert not match_company_name("Acme Freight", "Northwind Diagnostics").matched


# ---------------------------------------------------------------------------
# Keyword classification
# ---------------------------------------------------------------------------


def test_keyword_maps_a_headline_to_a_taxonomy_type():
    type_key, word = classify_by_keyword("Acme opens new fill-finish suite in Leeds")
    assert type_key == "new_lyo_capacity"
    assert word == "fill-finish"


def test_a_headline_with_no_keyword_is_not_a_signal():
    assert classify_by_keyword("Acme names new chief financial officer") == (None, None)


def test_every_configured_keyword_type_exists_in_the_taxonomy():
    """Config drift here would silently drop detections on the floor."""
    from signal_engine.config import get_config, get_taxonomy

    known = {t.key for t in get_taxonomy().types}
    configured = set(get_config().detection.get("keywords", {}))
    assert configured <= known, f"keywords reference unknown types: {configured - known}"


# ---------------------------------------------------------------------------
# Detector shaping
# ---------------------------------------------------------------------------


FDA_ROW = {
    "k_number": "K253634",
    "applicant": "Northwind Diagnostics",
    "device_name": "Northwind Respiratory Panel",
    "decision_date": "2026-06-17",
    "decision_description": "Substantially Equivalent",
}


def test_openfda_row_becomes_a_detection_with_a_link_back():
    detection = openfda.OpenFDADetector()._to_detection(
        FDA_ROW, "regulatory_submission", "510(k)"
    )
    assert detection is not None
    assert detection.company_name == "Northwind Diagnostics"
    assert detection.detected_date == dt.date(2026, 6, 17)
    assert detection.url.endswith("K253634")
    assert "K253634" in detection.title


def test_sbir_accepts_both_payload_shapes():
    row = {"firm": "Acme Bio", "award_title": "Lyophilised reagent study"}
    assert sbir._rows([row]) == [row]
    assert sbir._rows({"data": [row]}) == [row]
    assert sbir._rows({"unexpected": 1}) == []


def test_a_news_item_only_counts_when_the_company_is_named_and_a_keyword_hits():
    detector = news.NewsDetector()
    stamp = "Tue, 11 Aug 2026 09:00:00 GMT"

    hit = detector._to_detection(
        {"title": "Northwind Bio opens new fill-finish line", "pubDate": stamp},
        "Northwind Bio", SINCE, 0.55,
    )
    assert hit is not None and hit.type_key == "new_lyo_capacity"

    # Named, but says nothing that maps to a signal type.
    assert detector._to_detection(
        {"title": "Northwind Bio appoints a new CFO", "pubDate": stamp},
        "Northwind Bio", SINCE, 0.55,
    ) is None

    # Right words, wrong company — Google News returns loosely related stories.
    assert detector._to_detection(
        {"title": "Acme Corp opens new fill-finish line", "pubDate": stamp},
        "Northwind Bio", SINCE, 0.55,
    ) is None


def test_news_discovery_is_deliberately_empty():
    """Guessing a company name out of a headline would manufacture companies."""
    assert news.NewsDetector().discover(SINCE).detections == []


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _detection(company: str, **kw) -> Detection:
    return Detection(
        type_key=kw.pop("type_key", "regulatory_submission"),
        company_name=company,
        title=kw.pop("title", "510(k) K1: a device"),
        detected_date=kw.pop("detected_date", dt.date(2026, 6, 1)),
        url=kw.pop("url", "https://example.test/K1"),
        source_name="test",
        source_confidence=kw.pop("source_confidence", 0.98),
        raw=kw.pop("raw", {}),
    )


class FakeDetector:
    """A source that returns exactly what a test hands it."""

    name = "openfda"
    watchlist: list[Detection] = []
    discovered: list[Detection] = []

    def enabled(self) -> bool:
        return True

    def for_companies(self, names, since):
        return base.DetectorResult(detections=list(self.watchlist), queries_made=len(names))

    def discover(self, since):
        return base.DetectorResult(detections=list(self.discovered), queries_made=1)


@pytest.fixture()
def fake_source(monkeypatch):
    """Swap the openfda detector for a canned one, restoring it afterwards."""

    def _install(watchlist=(), discovered=()):
        cls = type("Fake", (FakeDetector,),
                   {"watchlist": list(watchlist), "discovered": list(discovered)})
        monkeypatch.setitem(runner.DETECTORS, "openfda", cls)
        return cls

    return _install


def test_an_exact_match_from_a_trusted_source_scores(session, make_company, fake_source):
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Northwind Diagnostics Ltd")])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.scored) == 1
    assert not report.review
    assert report.scored[0].signal_id is not None


def test_an_uncertain_match_waits_for_review_instead_of_scoring(
    session, make_company, fake_source
):
    make_company("Northwind Bio")
    fake_source(watchlist=[_detection("Northwind Bio Diagnostics Group")])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert not report.scored
    assert len(report.review) == 1

    from signal_engine.models import Signal

    signal = session.get(Signal, report.review[0].signal_id)
    assert signal.status == "review"
    assert signal.source == "auto"
    assert signal.detail_url == "https://example.test/K1"


def test_a_weak_source_cannot_score_even_on_an_exact_match(
    session, make_company, fake_source
):
    """A press headline is a mention. It reaches you; it does not score itself."""
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Northwind Diagnostics", source_confidence=0.55)])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert not report.scored
    assert len(report.review) == 1


def test_a_company_we_do_not_track_is_skipped_unless_discovering(
    session, make_company, fake_source
):
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Someone Else Entirely")])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.unmatched) == 1
    assert not report.scored and not report.review


def test_a_discovered_company_never_scores_automatically(session, fake_source):
    """Discovery produces a watchlist entry and a review item, never a Tier A."""
    fake_source(discovered=[_detection("Brand New Bio Ltd")])

    report = runner.run_detection(
        session, sources=["openfda"], since=SINCE, discover=True
    )

    assert not report.scored
    assert len(report.review) == 1
    assert report.review[0].new_company

    from sqlalchemy import select

    from signal_engine.models import Company

    company = session.scalar(select(Company).where(Company.created_by == "detect"))
    assert company is not None
    assert company.status == "watchlist"


def test_running_twice_does_not_duplicate_a_signal(session, make_company, fake_source):
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Northwind Diagnostics")])

    first = runner.run_detection(session, sources=["openfda"], since=SINCE)
    second = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(first.scored) == 1
    assert not second.scored
    assert len(second.duplicates) == 1


def test_dry_run_writes_nothing(session, make_company, fake_source):
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Northwind Diagnostics")])

    report = runner.run_detection(
        session, sources=["openfda"], since=SINCE, dry_run=True
    )
    assert len(report.scored) == 1

    from sqlalchemy import select

    from signal_engine.models import Signal

    assert session.scalars(select(Signal).where(Signal.source == "auto")).all() == []


def test_a_dead_source_is_reported_and_the_run_continues(
    session, make_company, monkeypatch
):
    """Free public APIs go down. That must not cost us the whole run."""
    make_company("Northwind Diagnostics")

    class DeadSource(FakeDetector):
        def for_companies(self, names, since):
            return base.DetectorResult(
                errors=[base.SourceError("openfda", "rate-limited")], queries_made=1
            )

    class LiveSource(FakeDetector):
        watchlist = [_detection("Northwind Diagnostics")]

    monkeypatch.setitem(runner.DETECTORS, "openfda", DeadSource)
    monkeypatch.setitem(runner.DETECTORS, "sbir", LiveSource)

    report = runner.run_detection(session, sources=["openfda", "sbir"], since=SINCE)

    assert [e.message for e in report.errors] == ["rate-limited"]
    assert len(report.scored) == 1
    assert "rate-limited" in report.render()


def test_an_unknown_signal_type_is_never_invented(session, make_company, fake_source):
    """A source naming a type outside the taxonomy is dropped, not guessed at."""
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[_detection("Northwind Diagnostics", type_key="not_a_real_type")])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.unmatched) == 1
    assert not report.scored


def test_a_scored_detection_moves_the_company_score(session, make_company, fake_source):
    company = make_company("Northwind Diagnostics")
    assert (company.current_score or 0) == 0

    fake_source(watchlist=[_detection("Northwind Diagnostics")])
    runner.run_detection(session, sources=["openfda"], since=SINCE)

    session.refresh(company)
    assert company.current_score > 0
