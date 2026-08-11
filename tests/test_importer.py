"""CSV import.

The rule the tests protect: imported companies are watchlist entries with NO signal
history and NO score. A spreadsheet is an address book, not a pipeline.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from signal_engine.importer import run_import
from signal_engine.models import Company, Contact, Signal

COMPANIES_CSV = """\
Company Name,Website,Country,Segment,Company Size
Northwind Diagnostics,https://www.northwinddx.com,United Kingdom,IVD manufacturer,50-200
Kestrel Molecular,kestrelmolecular.com,United States,Point-of-care,10-50
Ambervale Animal Health,ambervale.ie,Ireland,Veterinary diagnostics,10-50
,,,,
"""

CONTACTS_CSV = """\
Full Name,Job Title,Email,LinkedIn,Company
A Person,Assay development lead,a.person@northwinddx.com,https://linkedin.com/in/ap,Northwind Diagnostics
B Person,Head of Formulation,b.person@kestrelmolecular.com,,Kestrel Molecular
C Person,,,,Brand New Company Ltd
,,,,Nobody Ltd
"""


@pytest.fixture()
def import_dir(tmp_path):
    (tmp_path / "companies.csv").write_text(COMPANIES_CSV, encoding="utf-8")
    (tmp_path / "contacts.csv").write_text(CONTACTS_CSV, encoding="utf-8")
    return tmp_path


def test_dry_run_writes_nothing(session, import_dir):
    plan = run_import(session, dry_run=True, import_dir=import_dir)

    assert plan.companies_created == 4  # 3 listed + 1 created via a contact row
    assert plan.contacts_created == 3
    assert session.scalars(select(Company)).all() == []
    assert "DRY RUN" in plan.report()


def test_real_run_creates_companies_and_contacts(session, import_dir):
    run_import(session, dry_run=False, import_dir=import_dir)

    companies = session.scalars(select(Company)).all()
    contacts = session.scalars(select(Contact)).all()

    assert {c.name for c in companies} == {
        "Northwind Diagnostics",
        "Kestrel Molecular",
        "Ambervale Animal Health",
        "Brand New Company Ltd",
    }
    assert len(contacts) == 3


def test_imported_companies_have_no_signals_and_no_score(session, import_dir):
    """The whole thesis: a target list is a watchlist, never a fetch queue."""
    run_import(session, dry_run=False, import_dir=import_dir)

    assert session.scalars(select(Signal)).all() == []
    for company in session.scalars(select(Company)):
        assert company.status == "watchlist"
        assert company.current_score == 0
        assert company.first_signal_date is None


def test_import_is_idempotent(session, import_dir):
    run_import(session, dry_run=False, import_dir=import_dir)
    first = len(session.scalars(select(Company)).all())

    second_plan = run_import(session, dry_run=False, import_dir=import_dir)

    assert len(session.scalars(select(Company)).all()) == first
    assert second_plan.companies_created == 0
    assert second_plan.companies_matched >= 3
    assert second_plan.contacts_created == 0


def test_urls_and_suffixes_do_not_create_duplicates(session, import_dir):
    """The companies file has a full URL; the contacts file matches on name."""
    run_import(session, dry_run=False, import_dir=import_dir)
    northwind = [
        c for c in session.scalars(select(Company)) if "Northwind" in c.name
    ]
    assert len(northwind) == 1
    assert northwind[0].domain == "northwinddx.com"


def test_rows_without_a_usable_name_are_skipped_not_crashed(session, import_dir):
    plan = run_import(session, dry_run=False, import_dir=import_dir)
    assert plan.rows_skipped == 2  # the blank company row and the nameless contact
    assert not any(c.name == "" for c in session.scalars(select(Company)))


def test_headers_are_matched_without_a_mapping_file(session, tmp_path):
    """A Google Sheets export usually works untouched — different case, underscores,
    and a UTF-8 BOM included."""
    (tmp_path / "companies.csv").write_text(
        "﻿company_name,DOMAIN,country\nOddly Headed Ltd,oddly.com,Norway\n",
        encoding="utf-8",
    )
    run_import(session, dry_run=False, import_dir=tmp_path)

    company = session.scalar(select(Company))
    assert company.name == "Oddly Headed Ltd"
    assert company.domain == "oddly.com"
    assert company.country == "Norway"


def test_an_explicit_mapping_overrides_the_aliases(session, tmp_path):
    (tmp_path / "companies.csv").write_text(
        "Col A,Col B\nMapped Ltd,mapped.com\n", encoding="utf-8"
    )
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(
        "companies:\n  file: companies.csv\n  columns:\n"
        "    name: 'Col A'\n    domain: 'Col B'\n",
        encoding="utf-8",
    )
    run_import(session, dry_run=False, import_dir=tmp_path, mapping_path=mapping)

    company = session.scalar(select(Company))
    assert company.name == "Mapped Ltd"
    assert company.domain == "mapped.com"


def test_missing_name_column_warns_instead_of_failing(session, tmp_path):
    (tmp_path / "companies.csv").write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    plan = run_import(session, dry_run=True, import_dir=tmp_path)

    assert any("company-name column" in w for w in plan.warnings)
    assert plan.companies_created == 0


def test_empty_import_dir_warns(session, tmp_path):
    plan = run_import(session, dry_run=True, import_dir=tmp_path)
    assert any("No CSV files found" in w for w in plan.warnings)


def test_unrecognised_files_are_reported_not_silently_ignored(session, tmp_path):
    (tmp_path / "companies.csv").write_text(COMPANIES_CSV, encoding="utf-8")
    (tmp_path / "random_export.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    plan = run_import(session, dry_run=True, import_dir=tmp_path)
    assert any("random_export.csv" in w for w in plan.warnings)
