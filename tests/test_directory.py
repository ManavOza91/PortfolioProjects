"""The ICP directory: fit, filters, and the wall between it and the pipeline.

The wall is the point of this file. A company in the directory has fired no
signal, so it must be invisible to every query that decides who to contact.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from signal_engine import directory as dirmod
from signal_engine.directory import (
    Candidate,
    DirectoryFilters,
    browse,
    build_directory,
    directory_total,
    set_entry_state,
    size_band,
)
from signal_engine.models import Company, DirectoryEntry

TODAY = dt.date(2026, 8, 13)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test in this file may touch the network.

    build_directory walks EUDAMED's risk classes, so an unstubbed fetch would make
    the suite hit a slow public API a few hundred times. Tests that need a payload
    override this with their own monkeypatch.
    """
    def _fail(*args, **kwargs):
        raise AssertionError("a test tried to make a real HTTP request")

    monkeypatch.setattr(dirmod, "fetch", _fail)


# ---------------------------------------------------------------------------
# The wall
# ---------------------------------------------------------------------------


def test_a_directory_entry_is_not_a_company(session):
    """Fit alone must never create a row in `companies`."""
    _entry(session, "Northwind Assays", country="DE")

    assert session.scalars(select(Company)).all() == []


def test_the_priority_queue_cannot_see_the_directory(session, make_company):
    """The queue reads `companies`. Directory entries live elsewhere by design."""
    from signal_engine.reporting import ranked_queue

    for n in range(5):
        _entry(session, f"Fitting Company {n}", country="GB")

    assert directory_total(session) == 5
    assert ranked_queue(session, limit=50) == []


def test_the_buddy_score_cannot_see_the_directory(session):
    """A directory of 800 must not dilute or flatter the Buddy Score."""
    from signal_engine.buddy import compute_buddy_score

    before = compute_buddy_score(session, period_type="week").score
    for n in range(20):
        _entry(session, f"Fitting Company {n}", country="GB")
    after = compute_buddy_score(session, period_type="week").score

    assert before == after


def test_a_company_already_in_the_pipeline_is_not_re_listed(session, make_company):
    """It earned its way in. It does not belong on a research list."""
    make_company("Northwind Diagnostics Ltd")
    report = _build(session, [
        Candidate(name="Northwind Diagnostics", source="eudamed",
                  source_ref="DE-MF-1", country="DE", keywords={"assay"}),
    ])

    assert report.skipped_already_a_company == 1
    assert report.entries_added == 0


# ---------------------------------------------------------------------------
# Unknown is not zero
# ---------------------------------------------------------------------------


def test_an_unknown_size_stays_unknown(session):
    assert size_band(None) is None


def test_size_bands_come_from_config_not_code():
    assert size_band(3) == "micro"
    assert size_band(20) == "small"
    assert size_band(100) == "medium"
    assert size_band(5000) == "large"


def test_a_floor_count_cannot_claim_a_small_size():
    """"At least 3 devices" fits a micro business and it fits bioMérieux."""
    assert size_band(3, is_floor=True) is None
    assert size_band(100, is_floor=True) is None


def test_a_floor_count_can_still_confirm_a_large_one():
    """Once it passes every ceiling, what we didn't see cannot make it smaller."""
    assert size_band(5000, is_floor=True) == "large"


def test_a_company_with_no_country_is_kept_not_excluded(session):
    """Absence of evidence is not evidence of a bad fit."""
    report = _build(session, [
        Candidate(name="Mystery Diagnostics", source="eudamed",
                  source_ref="XX-1", country=None, keywords={"assay"}),
    ])

    assert report.entries_added == 1
    assert report.skipped_out_of_territory == 0
    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.country is None
    assert entry.size_band is None


def test_an_unknown_size_is_inside_the_target_bands(session):
    """`target_size_bands` includes "unknown" — see config. Guarded here."""
    from signal_engine.config import get_config

    assert "unknown" in get_config().icp["target_size_bands"]


def test_large_manufacturers_are_filtered_out(session):
    """The brief: cold outbound to large corporates without a signal went silent."""
    report = _build(session, [
        Candidate(name="Enormous Devices PLC", source="eudamed", source_ref="DE-MF-9",
                  country="DE", device_count=4000, keywords={"assay"}),
    ])

    assert report.skipped_wrong_size == 1
    assert report.entries_added == 0


def test_a_company_outside_the_territories_is_filtered_out(session, monkeypatch):
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().icp, "territories", ["GB", "DE"])
    report = _build(session, [
        Candidate(name="Faraway Diagnostics", source="eudamed", source_ref="BR-MF-1",
                  country="BR", keywords={"assay"}),
    ])

    assert report.skipped_out_of_territory == 1


def test_an_excluded_territory_is_dropped_even_with_an_open_allow_list(
    session, monkeypatch
):
    """"I sell everywhere except X" is the realistic case; an allow-list can't say it."""
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().icp, "territories", [])
    monkeypatch.setitem(get_config().icp, "exclude_territories", ["CN"])

    report = _build(session, [
        Candidate(name="Nanjing Vazyme", source="eudamed", source_ref="CN-MF-1",
                  country="CN", keywords={"assay"}),
        Candidate(name="Phadia AB", source="eudamed", source_ref="SE-MF-1",
                  country="SE", keywords={"assay"}),
    ])

    assert report.skipped_out_of_territory == 1
    assert report.entries_added == 1
    assert session.scalars(select(DirectoryEntry)).one().name == "Phadia AB"


def test_an_unknown_country_is_never_excluded(session, monkeypatch):
    """We can't prove it's in the excluded country, so it stays."""
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().icp, "exclude_territories", ["CN"])
    report = _build(session, [
        Candidate(name="Mystery Diagnostics", source="eudamed", source_ref="XX-1",
                  country=None, keywords={"assay"}),
    ])

    assert report.skipped_out_of_territory == 0
    assert report.entries_added == 1


def test_no_configured_territories_means_every_territory(session, monkeypatch):
    """The shipped default is empty, because any list is a guess about a business.

    Filtering belongs on the Directory page, per search — not baked into the build.
    """
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().icp, "territories", [])
    report = _build(session, [
        Candidate(name="Faraway Diagnostics", source="eudamed", source_ref="BR-MF-1",
                  country="BR", keywords={"assay"}),
    ])

    assert report.skipped_out_of_territory == 0
    assert report.entries_added == 1


# ---------------------------------------------------------------------------
# Filtering, and the absence of a rank
# ---------------------------------------------------------------------------


def test_the_directory_is_alphabetical_not_ranked(session):
    """No ordering by attribute count — that would sort by our own coverage."""
    for name, count in [("Zeta Assays", 1), ("Alpha Assays", 90), ("Mid Assays", 20)]:
        _entry(session, name, country="GB", device_count=count)

    names = [e.name for e in browse(session).entries]
    assert names == ["Alpha Assays", "Mid Assays", "Zeta Assays"]


def test_filtering_by_territory(session):
    _entry(session, "British Assays", country="GB")
    _entry(session, "German Assays", country="DE")

    result = browse(session, DirectoryFilters(territory="GB"))
    assert [e.name for e in result.entries] == ["British Assays"]
    assert result.total == 1


def test_filtering_by_device_keyword(session):
    _entry(session, "Kit Maker", country="GB", keywords="test kit")
    _entry(session, "Reagent Maker", country="GB", keywords="reagent")

    result = browse(session, DirectoryFilters(keyword="test kit"))
    assert [e.name for e in result.entries] == ["Kit Maker"]


def test_filtering_by_size_including_unknown(session):
    _entry(session, "Known Size", country="GB", device_count=3)
    _entry(session, "Unknown Size", country="GB", device_count=None)

    assert [e.name for e in browse(session, DirectoryFilters(size="micro")).entries] \
        == ["Known Size"]
    assert [e.name for e in browse(session, DirectoryFilters(size="unknown")).entries] \
        == ["Unknown Size"]


def test_the_page_is_capped_so_a_list_of_800_is_never_dumped(session):
    from signal_engine.config import get_config

    per_page = int(get_config().icp["page_size_display"])
    for n in range(per_page + 15):
        _entry(session, f"Company {n:03d}", country="GB")

    result = browse(session)
    assert len(result.entries) == per_page
    assert result.total == per_page + 15
    assert result.pages == 2

    assert len(browse(session, DirectoryFilters(page=2)).entries) == 15


def test_dismissing_an_entry_hides_it_without_deleting_it(session):
    entry = _entry(session, "Not For Us", country="GB")

    set_entry_state(session, entry.id, dismissed=True)

    assert browse(session).total == 0
    assert browse(session, DirectoryFilters(include_dismissed=True)).total == 1


def test_a_refresh_does_not_reset_your_review_state(session):
    """Marking something researched must survive the next slow batch run."""
    _build(session, [
        Candidate(name="Northwind Assays", source="eudamed", source_ref="DE-MF-7",
                  country="DE", device_count=4, keywords={"assay"}),
    ])
    entry = session.scalars(select(DirectoryEntry)).one()
    set_entry_state(session, entry.id, reviewed=True, notes="spoke to them at a show")

    report = _build(session, [
        Candidate(name="Northwind Assays", source="eudamed", source_ref="DE-MF-7",
                  country="DE", device_count=9, keywords={"assay", "reagent"}),
    ])

    assert report.entries_updated == 1
    assert report.entries_added == 0
    session.refresh(entry)
    assert entry.reviewed is True
    assert entry.notes == "spoke to them at a show"
    assert entry.device_count == 9


def test_dry_run_writes_nothing(session):
    report = _build(session, [
        Candidate(name="Northwind Assays", source="eudamed", source_ref="DE-MF-3",
                  country="DE", keywords={"assay"}),
    ], dry_run=True)

    assert report.entries_added == 1
    assert session.scalars(select(DirectoryEntry)).all() == []


# ---------------------------------------------------------------------------
# Source shaping
# ---------------------------------------------------------------------------


def test_the_risk_class_sweep_is_what_finds_ivd_companies(session, monkeypatch):
    """IVDR classes A-D are IVDs by definition; MDR devices are I/IIa/IIb/III.

    It is the only working "these are diagnostics" filter EUDAMED offers, and it
    is why the directory returns companies at all — a trade-name keyword search
    found three.
    """
    calls: list[dict] = []

    def _fetch(source, url, *, params=None, **kw):
        calls.append(params or {})
        return {"last": True, "content": [{
            "manufacturerName": "Phadia AB",
            "manufacturerSrn": "SE-MF-000014170",
            "tradeName": "ImmunoCAP Allergen rx3",
            "riskClass": {"code": "refdata.risk-class.class-b"},
        }]}, None

    monkeypatch.setattr(dirmod, "fetch", _fetch)
    build_directory(session, keywords=[], sources=["eudamed"])

    swept = [c.get("riskClassCode") for c in calls if c.get("riskClassCode")]
    assert swept == [
        "refdata.risk-class.class-b",
        "refdata.risk-class.class-c",
        "refdata.risk-class.class-d",
    ], "class A is excluded — it is buffers and receptacles, not assays"

    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.country == "SE"
    assert "IVDR B" in entry.device_keywords


def test_a_swept_device_count_is_always_marked_as_a_floor(session, monkeypatch):
    """We counted what we walked past, not the manufacturer's true catalogue."""
    monkeypatch.setattr(dirmod, "fetch", lambda *a, **k: ({"last": True, "content": [
        {"manufacturerName": "Phadia AB", "manufacturerSrn": "SE-MF-1",
         "tradeName": f"Kit {n}", "riskClass": {"code": "refdata.risk-class.class-b"}}
        for n in range(3)
    ]}, None))

    build_directory(session, keywords=[], sources=["eudamed"])

    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.device_count_is_floor is True
    assert entry.device_count >= 3


def test_the_sweep_discards_out_of_territory_rows_as_it_goes(session, monkeypatch):
    """When territories ARE configured, drop rows during the walk.

    Otherwise we accumulate tens of thousands of rows just to throw them away.
    """
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().icp, "territories", ["SE"])
    monkeypatch.setattr(dirmod, "fetch", lambda *a, **k: ({"last": True, "content": [
        {"manufacturerName": "Nanjing Vazyme", "manufacturerSrn": "CN-MF-1",
         "riskClass": {"code": "refdata.risk-class.class-b"}},
        {"manufacturerName": "Phadia AB", "manufacturerSrn": "SE-MF-1",
         "riskClass": {"code": "refdata.risk-class.class-b"}},
    ]}, None))

    report = build_directory(session, keywords=[], sources=["eudamed"])

    assert report.candidates_found == 1
    assert session.scalars(select(DirectoryEntry)).one().name == "Phadia AB"


def test_the_country_comes_out_of_the_eudamed_srn(session, monkeypatch):
    payload = {
        "last": True,
        "content": [{
            "manufacturerName": "GA Generic Assays GmbH",
            "manufacturerSrn": "DE-MF-000032990",
            "tradeName": "Anti-CCP",
            "riskClass": {"code": "refdata.risk-class.class-iia"},
        }],
    }
    monkeypatch.setattr(dirmod, "fetch", lambda *a, **k: (payload, None))

    build_directory(session, keywords=["assay"], sources=["eudamed"])

    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.country == "DE"
    assert entry.highest_risk_class == "class-iia"
    assert entry.source_ref == "DE-MF-000032990"


def test_an_mhra_country_name_becomes_a_code(session, monkeypatch):
    rows = [{
        "MAN_ORGANISATION_ID": 34990,
        "MAN_ORGANISATION_NAME": "Northwind Assays Ltd",
        "MAN_COUNTRY": "England, United Kingdom",
    }]
    monkeypatch.setattr(dirmod, "fetch", lambda *a, **k: (rows, None))

    build_directory(session, keywords=["assay"], sources=["mhra"])

    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.country == "GB"


def test_an_unrecognised_country_stays_unknown_rather_than_being_guessed(session):
    assert dirmod._country_code("Republic of Somewhere") is None
    assert dirmod._country_code(None) is None
    assert dirmod._country_code("de") == "DE"


def test_a_dead_register_is_reported_and_the_run_continues(session, monkeypatch):
    from signal_engine.detect.base import SourceError

    monkeypatch.setattr(
        dirmod, "fetch", lambda *a, **k: (None, SourceError("eudamed", "timed out"))
    )
    report = build_directory(session, keywords=["assay"], sources=["eudamed"])

    # One per risk-class sweep plus one per keyword — every leg reports, and none
    # of them aborts the run.
    assert report.errors
    assert {e.message for e in report.errors} == {"timed out"}
    assert report.entries_added == 0
    assert "timed out" in report.render()


def test_two_keywords_finding_one_company_produce_one_entry(session):
    report = _build(session, [
        Candidate(name="Northwind Assays", source="eudamed", source_ref="DE-MF-5",
                  country="DE", keywords={"assay"}, examples=["Panel A"]),
        Candidate(name="Northwind Assays", source="eudamed", source_ref="DE-MF-5",
                  country="DE", keywords={"reagent"}, examples=["Buffer B"]),
    ])

    assert report.entries_added == 1
    entry = session.scalars(select(DirectoryEntry)).one()
    assert entry.device_keywords == "assay, reagent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(session, name: str, *, country=None, device_count=None, keywords=None):
    entry = DirectoryEntry(
        name=name,
        country=country,
        size_band=size_band(device_count),
        device_count=device_count,
        device_keywords=keywords,
        source="eudamed",
        source_ref=f"ref-{name}",
        first_seen=TODAY,
        last_seen=TODAY,
    )
    session.add(entry)
    session.flush()
    return entry


def _build(session, candidates: list[Candidate], *, dry_run: bool = False):
    """Run the write path against canned candidates, skipping the network."""
    merged: dict[str, Candidate] = {}
    for candidate in candidates:
        key = f"{candidate.source}:{candidate.source_ref}"
        if key in merged:
            merged[key].merge(candidate)
        else:
            merged[key] = candidate

    import signal_engine.directory as module

    def _fake_collect(keyword, out, report, icp):
        out.update(merged)
        report.queries_made += 1

    originals = (
        module._collect_eudamed, module._collect_mhra, module._sweep_eudamed_class,
    )
    module._collect_eudamed = _fake_collect
    module._collect_mhra = lambda *a, **k: None
    module._sweep_eudamed_class = lambda *a, **k: None
    try:
        return build_directory(
            session, keywords=["assay"], sources=["eudamed"], dry_run=dry_run
        )
    finally:
        (module._collect_eudamed, module._collect_mhra,
         module._sweep_eudamed_class) = originals
