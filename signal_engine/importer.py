"""CSV import from an existing system.

Companies import as WATCHLIST ENTRIES WITH NO SIGNAL HISTORY. Contacts import as
plain records attached to companies. They are a starting address book, not scored
pipeline — nothing here creates a signal, and nothing here gives a company a score.
A company still has to earn its way in.

Column mapping lives in data/mapping.yaml. If that file is absent the importer falls
back to a set of common header aliases, so a Google Sheets export usually works
untouched. Run with dry_run=True first; it writes nothing and tells you exactly what
it would do.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy.orm import Session

from .config import PROJECT_ROOT, get_config
from .ingest import find_company, find_or_create_company, normalise_domain, upsert_contact

IMPORT_DIR = PROJECT_ROOT / "data" / "import"
MAPPING_PATH = PROJECT_ROOT / "data" / "mapping.yaml"

# Used when no mapping.yaml exists, or for any field the mapping omits.
DEFAULT_ALIASES: dict[str, dict[str, list[str]]] = {
    "companies": {
        "name": ["company", "company name", "account", "organisation", "organization", "name"],
        "domain": ["domain", "website", "web site", "url", "company website"],
        "country": ["country", "location", "region"],
        "segment": ["segment", "industry", "market", "vertical", "category", "type"],
        "size_band": ["size", "size band", "employees", "headcount", "company size"],
        "notes": ["notes", "note", "comments", "comment"],
    },
    "contacts": {
        "full_name": ["full name", "name", "contact", "contact name"],
        "first_name": ["first name", "firstname", "given name"],
        "last_name": ["last name", "lastname", "surname", "family name"],
        "job_title": ["job title", "title", "role", "position"],
        "email": ["email", "e-mail", "email address"],
        "linkedin_url": ["linkedin", "linkedin url", "linkedin profile", "profile url"],
        "country": ["country", "location"],
        "persona": ["persona", "buyer persona", "segment"],
        "company_name": ["company", "company name", "account", "organisation", "organization"],
        "company_domain": ["company website", "company domain", "domain", "website"],
        "notes": ["notes", "note", "comments"],
    },
}


@dataclass
class ImportPlan:
    dry_run: bool
    companies_created: int = 0
    companies_matched: int = 0
    contacts_created: int = 0
    contacts_matched: int = 0
    rows_skipped: int = 0
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def report(self) -> str:
        head = "DRY RUN — nothing was written." if self.dry_run else "Import complete."
        lines = [head, ""]
        if self.files:
            lines.append("Files read: " + ", ".join(self.files))
        lines += [
            f"Companies: {self.companies_created} new, "
            f"{self.companies_matched} already known",
            f"Contacts:  {self.contacts_created} new, "
            f"{self.contacts_matched} already known",
            f"Rows skipped (no usable name): {self.rows_skipped}",
        ]
        if self.samples:
            lines += ["", "Sample of what would be created:"]
            lines += [f"  - {s}" for s in self.samples[:10]]
        if self.warnings:
            lines += ["", "Warnings:"]
            lines += [f"  ! {w}" for w in self.warnings]
        lines += [
            "",
            "Imported companies are watchlist entries with no signal history and no "
            "score. They enter the pipeline only when a signal fires.",
        ]
        return "\n".join(lines)


def load_mapping(path: Path | None = None) -> dict[str, Any]:
    path = path or MAPPING_PATH
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _norm_header(h: str) -> str:
    return " ".join(str(h or "").replace("_", " ").strip().lower().split())


def _resolve_columns(
    headers: Iterable[str], section: str, mapping: dict[str, Any]
) -> dict[str, str]:
    """Map our field names to the CSV's actual headers.

    Explicit mapping wins; anything it does not cover falls back to aliases. Header
    matching is case- and underscore-insensitive.
    """
    lookup = {_norm_header(h): h for h in headers}
    resolved: dict[str, str] = {}

    configured = (mapping.get(section) or {}).get("columns") or {}
    for field_name, candidates in configured.items():
        options = candidates if isinstance(candidates, list) else [candidates]
        for candidate in options:
            actual = lookup.get(_norm_header(candidate))
            if actual:
                resolved[field_name] = actual
                break

    for field_name, aliases in DEFAULT_ALIASES.get(section, {}).items():
        if field_name in resolved:
            continue
        for alias in aliases:
            actual = lookup.get(_norm_header(alias))
            if actual:
                resolved[field_name] = actual
                break

    return resolved


def _value(row: dict[str, str], columns: dict[str, str], field_name: str) -> str | None:
    header = columns.get(field_name)
    if not header:
        return None
    raw = (row.get(header) or "").strip()
    return raw or None


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    # utf-8-sig strips the BOM Google Sheets exports carry.
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(r) for r in reader]
        headers = list(reader.fieldnames or [])
    return headers, rows


def _files_for(section: str, mapping: dict[str, Any], import_dir: Path) -> list[Path]:
    configured = (mapping.get(section) or {}).get("file")
    if configured:
        names = configured if isinstance(configured, list) else [configured]
        return [import_dir / n for n in names]
    # No file configured: any CSV whose name mentions the section.
    stem = section.rstrip("s")
    return sorted(
        p for p in import_dir.glob("*.csv") if stem in p.stem.lower()
    )


def run_import(
    session: Session,
    *,
    dry_run: bool = True,
    import_dir: Path | None = None,
    mapping_path: Path | None = None,
) -> ImportPlan:
    cfg = get_config()
    import_dir = import_dir or IMPORT_DIR
    mapping = load_mapping(mapping_path)
    plan = ImportPlan(dry_run=dry_run)

    if not import_dir.exists():
        plan.warnings.append(f"{import_dir} does not exist — nothing to import.")
        return plan

    all_csvs = sorted(import_dir.glob("*.csv"))
    if not all_csvs:
        plan.warnings.append(
            f"No CSV files found in {import_dir}. Export your sheets to CSV and put "
            "them there. Name them so 'company' or 'contact' appears in the filename, "
            "or list them explicitly in data/mapping.yaml."
        )
        return plan

    company_files = _files_for("companies", mapping, import_dir)
    contact_files = _files_for("contacts", mapping, import_dir)

    unclaimed = set(all_csvs) - set(company_files) - set(contact_files)
    for path in sorted(unclaimed):
        plan.warnings.append(
            f"{path.name} was not imported — its name does not identify it as a "
            "companies or contacts file. Add it to data/mapping.yaml to include it."
        )

    for path in company_files:
        _import_companies(session, path, mapping, plan, dry_run=dry_run, cfg=cfg)
    for path in contact_files:
        _import_contacts(session, path, mapping, plan, dry_run=dry_run, cfg=cfg)

    if dry_run:
        session.rollback()
    else:
        session.flush()
    return plan


def _import_companies(
    session: Session,
    path: Path,
    mapping: dict[str, Any],
    plan: ImportPlan,
    *,
    dry_run: bool,
    cfg,
) -> None:
    if not path.exists():
        plan.warnings.append(f"{path.name} listed in mapping but not found in data/import/")
        return

    headers, rows = _read_csv(path)
    columns = _resolve_columns(headers, "companies", mapping)
    plan.files.append(path.name)

    if "name" not in columns:
        plan.warnings.append(
            f"{path.name}: could not find a company-name column among {headers}. "
            "Set companies.columns.name in data/mapping.yaml."
        )
        return

    for row in rows:
        name = _value(row, columns, "name")
        if not name:
            plan.rows_skipped += 1
            continue

        domain = normalise_domain(_value(row, columns, "domain"))
        existing = find_company(session, name=name, domain=domain, tenant_id=cfg.tenant_id)
        if existing is not None:
            plan.companies_matched += 1
            if not dry_run:
                existing.domain = existing.domain or domain
                existing.country = existing.country or _value(row, columns, "country")
                existing.segment = existing.segment or _value(row, columns, "segment")
                existing.size_band = existing.size_band or _value(row, columns, "size_band")
            continue

        plan.companies_created += 1
        if len(plan.samples) < 20:
            plan.samples.append(f"company: {name}" + (f" ({domain})" if domain else ""))

        company, _ = find_or_create_company(
            session,
            name=name,
            domain=domain,
            country=_value(row, columns, "country"),
            segment=_value(row, columns, "segment"),
            size_band=_value(row, columns, "size_band"),
            status="watchlist",  # imported, never scored, never promoted
            created_by="import",
        )
        note = _value(row, columns, "notes")
        if note:
            company.notes = note


def _import_contacts(
    session: Session,
    path: Path,
    mapping: dict[str, Any],
    plan: ImportPlan,
    *,
    dry_run: bool,
    cfg,
) -> None:
    if not path.exists():
        plan.warnings.append(f"{path.name} listed in mapping but not found in data/import/")
        return

    headers, rows = _read_csv(path)
    columns = _resolve_columns(headers, "contacts", mapping)
    plan.files.append(path.name)

    if "company_name" not in columns and "company_domain" not in columns:
        plan.warnings.append(
            f"{path.name}: no company column found among {headers}. Contacts must "
            "attach to a company. Set contacts.columns.company_name in data/mapping.yaml."
        )
        return

    for row in rows:
        company_name = _value(row, columns, "company_name")
        company_domain = normalise_domain(_value(row, columns, "company_domain"))
        if not company_name and not company_domain:
            plan.rows_skipped += 1
            continue

        full_name = _value(row, columns, "full_name")
        if not full_name:
            first = _value(row, columns, "first_name")
            last = _value(row, columns, "last_name")
            full_name = " ".join(filter(None, [first, last])) or None

        email = _value(row, columns, "email")
        linkedin = _value(row, columns, "linkedin_url")
        if not any([full_name, email, linkedin]):
            plan.rows_skipped += 1
            continue

        company = find_company(
            session, name=company_name, domain=company_domain, tenant_id=cfg.tenant_id
        )
        created_company = company is None
        if company is None:
            company, _ = find_or_create_company(
                session,
                name=company_name or company_domain or "Unknown",
                domain=company_domain,
                status="watchlist",
                created_by="import",
            )
            plan.companies_created += 1

        before = {c.id for c in company.contacts}
        contact = upsert_contact(
            session,
            company,
            full_name=full_name,
            job_title=_value(row, columns, "job_title"),
            email=email,
            linkedin_url=linkedin,
            country=_value(row, columns, "country"),
            persona=_value(row, columns, "persona"),
            source="import",
            notes=_value(row, columns, "notes"),
            created_by="import",
        )
        if contact.id in before:
            plan.contacts_matched += 1
        else:
            plan.contacts_created += 1
            if len(plan.samples) < 20:
                label = full_name or email or linkedin
                plan.samples.append(
                    f"contact: {label} @ {company.name}"
                    + (" (new company)" if created_company else "")
                )
