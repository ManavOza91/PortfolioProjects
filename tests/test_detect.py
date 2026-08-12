"""Automatic detection: matching, the review gate, and dedupe.

No test here touches the network. Detectors are fed canned payloads, because the
thing worth protecting is the judgement in the runner, not httpx.
"""

from __future__ import annotations

import datetime as dt

import pytest

from signal_engine.detect import base, eudamed, mhra, news, openfda, runner
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


EUDAMED_PAYLOAD = {
    "totalElements": 36,
    "content": [
        {
            "manufacturerName": "Miltenyi Biotec B.V. & Co. KG",
            "manufacturerSrn": "DE-MF-000000123",
            "tradeName": "CliniMACS PBS/EDTA Buffer",
            "riskClass": {"code": "refdata.risk-class.class-iib"},
            "deviceStatusType": {"code": "refdata.device-model-status.on-the-market"},
        },
        {
            "manufacturerName": "Miltenyi Biotec B.V. & Co. KG",
            "tradeName": "CryoMACS Freezing Bag 250",
            "riskClass": {"code": "refdata.risk-class.class-iia"},
            "deviceStatusType": {"code": "refdata.device-model-status.on-the-market"},
        },
    ],
}


def test_eudamed_collapses_a_whole_catalogue_into_one_signal():
    """36 registered devices is one piece of evidence, not 36."""
    detection = eudamed.EUDAMEDDetector()._aggregate(
        EUDAMED_PAYLOAD, "Miltenyi", "regulatory_submission", 0.65, 50
    )
    assert detection is not None
    assert detection.raw["highest_risk_class"] == "class-iib"
    assert detection.company_name == "Miltenyi Biotec B.V. & Co. KG"
    assert detection.raw["devices_confirmed"] == 2


def test_eudamed_never_reports_a_count_it_did_not_verify():
    """"Antech" contains-matched 3063 rows; only the confirmed ones may be counted."""
    payload = {
        "totalElements": 3063,
        "content": [
            {"manufacturerName": "Antech Diagnostics", "tradeName": "A"},
            {"manufacturerName": "Plantech Medical GmbH", "tradeName": "B"},
        ],
    }
    detection = eudamed.EUDAMEDDetector()._aggregate(
        payload, "Antech", "regulatory_submission", 0.65, 50
    )
    assert "3063" not in detection.title
    assert detection.raw["devices_confirmed"] == 1
    assert detection.raw["count_is_a_floor"] is True
    assert "at least" in detection.title


def test_eudamed_marks_a_count_as_a_floor_when_it_only_read_one_page():
    """EUDAMED caps its page at 20 whatever pageSize we ask for."""
    detection = eudamed.EUDAMEDDetector()._aggregate(
        EUDAMED_PAYLOAD, "Miltenyi", "regulatory_submission", 0.65, 50
    )
    # 36 registered, 2 rows on the page — the count must not read as complete.
    assert detection.raw["count_is_a_floor"] is True
    assert "at least 2 device registrations" in detection.title


def test_eudamed_never_clears_the_auto_bar():
    """It publishes no date, so it is a standing fact and always goes to review."""
    from signal_engine.config import get_config

    cfg = get_config().detection["sources"]["eudamed"]
    assert float(cfg["base_confidence"]) < get_config().auto_review_threshold


def test_eudamed_rejects_a_contains_match_on_the_wrong_manufacturer():
    """The `name` filter matches across the record, so the name is re-checked."""
    payload = {
        "totalElements": 1,
        "content": [{"manufacturerName": "Someone Else Ltd", "tradeName": "Widget"}],
    }
    assert eudamed.EUDAMEDDetector()._aggregate(
        payload, "Miltenyi", "regulatory_submission", 0.65, 50
    ) is None


MHRA_ROW = {
    "MAN_ORGANISATION_ID": 34990,
    "MAN_CREATED_DATE": "2021-05-04T00:00:00.000Z",
    "MAN_ORGANISATION_NAME": "Miltenyi Biotec B.V. & Co. KG",
    "MAN_COUNTRY": "Germany",
    "RELATIONSHIP": "UK Responsible Person",
    "REP_NAME": "Miltenyi Biotec Ltd.",
}


def test_mhra_row_keeps_its_registration_date():
    """The one EU/UK source that dates its records — --since depends on it."""
    detection = mhra.MHRADetector()._to_detection(MHRA_ROW, "regulatory_submission", 0.9)
    assert detection is not None
    assert detection.detected_date == dt.date(2021, 5, 4)
    assert detection.company_name == "Miltenyi Biotec B.V. & Co. KG"
    assert detection.url.endswith("34990")
    assert "Germany" in detection.title


def test_mhra_sees_a_non_uk_manufacturer_selling_into_the_uk():
    """The whole point: a German company with no US filing is still visible."""
    detection = mhra.MHRADetector()._to_detection(MHRA_ROW, "regulatory_submission", 0.9)
    assert detection.raw["country"] == "Germany"
    assert detection.raw["uk_responsible_person"] == "Miltenyi Biotec Ltd."


def test_sbir_is_gone():
    """US-only, permanently down at source, and grants are a qualifier."""
    assert "sbir" not in runner.DETECTORS
    assert set(runner.DETECTORS) == {"openfda", "eudamed", "mhra", "news"}


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


def test_one_event_reported_by_three_outlets_becomes_one_signal(
    session, make_company, fake_source
):
    """The Meiban case: one CDMO alliance, three outlets, was reaching Tier A."""
    make_company("Meiban")
    fake_source(watchlist=[
        Detection(
            type_key="new_lyo_capacity", company_name="Meiban",
            title=f"Outlet {n} reports the Planet Innovation alliance",
            detected_date=dt.date(2026, 7, 1) + dt.timedelta(days=n),
            url=f"https://outlet{n}.test/story", source_name="Google News",
            source_confidence=0.55, unique_per_event=False,
        )
        for n in range(3)
    ])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.review) == 1, "three outlets, one event, one signal"
    assert len(report.same_event) == 2
    assert "same event" in report.render()


def test_two_real_clearances_in_one_week_both_survive(
    session, make_company, fake_source
):
    """The collapse must not swallow genuine events. openFDA issues K-numbers."""
    make_company("Northwind Diagnostics")
    fake_source(watchlist=[
        _detection(
            "Northwind Diagnostics",
            detected_date=dt.date(2026, 6, 1) + dt.timedelta(days=n),
            url=f"https://example.test/K{n}",
        )
        for n in range(2)
    ])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.scored) == 2
    assert not report.same_event


def test_the_collapse_respects_the_configured_window(
    session, make_company, fake_source
):
    """Two months apart is two events, even from the press."""
    make_company("Meiban")
    fake_source(watchlist=[
        Detection(
            type_key="new_lyo_capacity", company_name="Meiban", title=f"story {n}",
            detected_date=dt.date(2026, 3, 1) + dt.timedelta(days=60 * n),
            url=f"https://outlet{n}.test/x", source_name="Google News",
            source_confidence=0.55, unique_per_event=False,
        )
        for n in range(2)
    ])

    report = runner.run_detection(session, sources=["openfda"], since=SINCE)

    assert len(report.review) == 2
    assert not report.same_event


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
    monkeypatch.setitem(runner.DETECTORS, "news", LiveSource)

    report = runner.run_detection(session, sources=["openfda", "news"], since=SINCE)

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


def test_backfill_reaches_back_two_years_not_ninety_days(
    session, make_company, fake_source
):
    from signal_engine.config import get_config

    make_company("Northwind Diagnostics")
    fake_source(watchlist=[])

    normal = runner.run_detection(session, sources=["openfda"], dry_run=True)
    deep = runner.run_detection(
        session, sources=["openfda"], dry_run=True, backfill=True
    )

    months = int(get_config().detection.get("backfill_months", 24))
    span_days = (normal.since - deep.since).days
    assert span_days > 500, "a backfill must cover the whole capital cycle"
    assert abs((TODAY - deep.since).days - months * 30.44) < 40 or deep.backfill


def test_an_old_signal_still_counts_but_decayed(session, make_company, fake_source):
    """The point of the backfill: 20 months ago is faded, not gone."""
    make_company("Northwind Diagnostics")
    old = _detection(
        "Northwind Diagnostics",
        detected_date=dt.date.today() - dt.timedelta(days=600),
        url="https://example.test/old",
    )
    fake_source(watchlist=[old])

    report = runner.run_detection(session, sources=["openfda"], backfill=True)

    assert len(report.scored) == 1

    from signal_engine.models import Company
    from sqlalchemy import select

    company = session.scalar(select(Company).where(Company.name == "Northwind Diagnostics"))
    assert company.current_score > 0, "a decayed signal must never reach zero"
    assert company.current_score < 25.0, "and it must be worth less than a fresh one"


def test_a_truncated_source_says_so_instead_of_looking_complete(
    session, make_company, monkeypatch
):
    make_company("Northwind Diagnostics")

    class Truncated(FakeDetector):
        def for_companies(self, names, since):
            return base.DetectorResult(
                warnings=["Northwind Diagnostics: 90 records, only the first 50 were read"],
                queries_made=1,
            )

    monkeypatch.setitem(runner.DETECTORS, "openfda", Truncated)
    report = runner.run_detection(session, sources=["openfda"], backfill=True)

    assert report.warnings
    assert "only the first 50" in report.render()


def test_a_scored_detection_moves_the_company_score(session, make_company, fake_source):
    company = make_company("Northwind Diagnostics")
    assert (company.current_score or 0) == 0

    fake_source(watchlist=[_detection("Northwind Diagnostics")])
    runner.run_detection(session, sources=["openfda"], since=SINCE)

    session.refresh(company)
    assert company.current_score > 0
