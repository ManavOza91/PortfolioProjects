"""The ICP directory — companies worth researching, kept apart from the pipeline.

A company appears here because of what it IS: it makes IVDs, it is in a territory
you sell into, it looks small or medium. None of that is a buying signal, so
nothing here scores, nothing here is ranked, and nothing here can reach the
priority queue. `DirectoryEntry` is a separate table from `Company` precisely so
that separation cannot be undone by a forgotten filter.

Why there is no rank:

    the attributes are coarse and most companies share them, so any ordering
    would sort by how much we happen to know rather than by how good a fit
    a company is. A confident-looking rank built on that would be a lie.

So this is a filtered directory. You choose territory, device keyword and size;
it shows what matches, in a stable alphabetical order, a page at a time.

Unknown is a first-class value here. Every attribute may be NULL, and a NULL is
rendered as "unknown" rather than defaulted to zero or excluded.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import get_config
from .detect.base import SourceError, fetch
from .ingest import normalise_name
from .models import Company, DirectoryEntry

log = logging.getLogger(__name__)

EUDAMED_URL = "https://ec.europa.eu/tools/eudamed/api/devices/udiDiData"
EUDAMED_UI = (
    "https://ec.europa.eu/tools/eudamed/#/screen/search-device"
    "?submitted=true&nameSearchType=CONTAINS&name="
)
MHRA_URL = "https://pard.mhra.gov.uk/searchManufacturers"
MHRA_UI = "https://pard.mhra.gov.uk/manufacturer-details/"

_RISK_ORDER = ["class-i", "class-iia", "class-iib", "class-iii"]

# EUDAMED encodes the manufacturer's country in the SRN: "DE-MF-000032990".
_SRN_COUNTRY = re.compile(r"^([A-Z]{2})-")


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# What we learn about one company, before it is written
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    name: str
    source: str
    source_ref: str | None = None
    country: str | None = None
    device_count: int | None = None
    device_count_is_floor: bool = False
    highest_risk_class: str | None = None
    keywords: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)
    detail_url: str | None = None

    def merge(self, other: "Candidate") -> None:
        """Two keyword searches can find the same manufacturer. Keep the richer view."""
        self.country = self.country or other.country
        self.highest_risk_class = _worst(self.highest_risk_class, other.highest_risk_class)
        self.keywords |= other.keywords
        for example in other.examples:
            if example not in self.examples and len(self.examples) < 8:
                self.examples.append(example)
        if other.device_count is not None:
            if self.device_count is None or other.device_count > self.device_count:
                self.device_count = other.device_count
                self.device_count_is_floor = other.device_count_is_floor


@dataclass
class BuildReport:
    dry_run: bool = False
    queries_made: int = 0
    candidates_found: int = 0
    entries_added: int = 0
    entries_updated: int = 0
    skipped_out_of_territory: int = 0
    skipped_wrong_size: int = 0
    skipped_already_a_company: int = 0
    errors: list[SourceError] = field(default_factory=list)

    def render(self) -> str:
        head = "DRY RUN — nothing was written." if self.dry_run else "Directory refreshed."
        lines = [
            head,
            "",
            f"Queries made:      {self.queries_made}",
            f"Companies seen:    {self.candidates_found}",
            "",
            f"  {self.entries_added:>4}  added to the directory",
            f"  {self.entries_updated:>4}  already listed, details refreshed",
            f"  {self.skipped_already_a_company:>4}  already in your pipeline (skipped)",
            f"  {self.skipped_out_of_territory:>4}  outside your territories",
            f"  {self.skipped_wrong_size:>4}  outside your target size bands",
        ]
        if self.errors:
            lines += ["", "Sources that failed (the rest of the run continued):"]
            for e in self.errors[:10]:
                lines.append(f"  ! {e.source}: {e.message}")
        lines += [
            "",
            "This is a research directory, not a queue. Nothing here is scored or",
            "ranked, and nothing here reaches your priority list until a signal fires.",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build_directory(
    session: Session,
    *,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
    dry_run: bool = False,
    tenant_id: int | None = None,
) -> BuildReport:
    cfg = get_config()
    icp = cfg.icp
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id

    # None means "use the configured defaults"; an explicit [] means "none at all".
    # `or` would collapse the two and silently run nine searches you asked to skip.
    keywords = (
        list(icp.get("device_keywords", []) or []) if keywords is None else list(keywords)
    )
    sources = (
        list(icp.get("sources", ["eudamed", "mhra"])) if sources is None else list(sources)
    )
    report = BuildReport(dry_run=dry_run)

    candidates: dict[str, Candidate] = {}

    # The risk-class sweep is what actually finds companies: IVDR classes are the
    # only working "these are in-vitro diagnostics" filter EUDAMED offers.
    if "eudamed" in sources:
        for risk_class in icp.get("eudamed_risk_classes", []) or []:
            _sweep_eudamed_class(risk_class, candidates, report, icp)

    # Keyword search adds labels, and occasionally a company the sweep missed.
    for keyword in keywords:
        if "eudamed" in sources:
            _collect_eudamed(keyword, candidates, report, icp)
        if "mhra" in sources:
            _collect_mhra(keyword, candidates, report, icp)

    report.candidates_found = len(candidates)

    territories = {t.upper() for t in icp.get("territories", []) or []}
    target_bands = set(icp.get("target_size_bands", []) or [])

    for candidate in candidates.values():
        if territories and candidate.country and candidate.country.upper() not in territories:
            report.skipped_out_of_territory += 1
            continue
        # An unknown country is NOT grounds for exclusion — absence of evidence.

        band = size_band(
            candidate.device_count, is_floor=candidate.device_count_is_floor
        )
        if target_bands and (band or "unknown") not in target_bands:
            report.skipped_wrong_size += 1
            continue

        if _is_already_a_company(session, candidate.name, tenant_id):
            report.skipped_already_a_company += 1
            continue

        _upsert(session, candidate, band, report, tenant_id)

    if dry_run:
        session.rollback()
    else:
        session.flush()
    return report


def size_band(device_count: int | None, *, is_floor: bool = False) -> str | None:
    """Coarse size from the register. None means 'unknown', which is a real answer.

    `is_floor` matters more than it looks. A risk-class sweep counts the devices we
    walked past, not the manufacturer's catalogue — so "3" means "at least 3",
    which is equally consistent with a micro business and with bioMérieux. Calling
    that "micro" would be inventing a fact.

    So a floor can only ever confirm the TOP band, the one with no ceiling: once
    the count passes every other band's ceiling, size is established no matter what
    we did not see. Everything below that stays unknown until a real total arrives.
    """
    if device_count is None:
        return None

    bands = list(get_config().icp.get("size_bands", []) or [])
    for rule in bands:
        ceiling = rule.get("max_devices")
        if ceiling is None:
            return str(rule.get("name"))
        if device_count <= int(ceiling):
            # A bounded band is only trustworthy from a true total.
            return None if is_floor else str(rule.get("name"))
    return None


def _is_already_a_company(session: Session, name: str, tenant_id: int) -> bool:
    """A company with a signal belongs in the pipeline, not in the research list."""
    target = normalise_name(name)
    if not target:
        return False
    for existing in session.scalars(
        select(Company.name).where(Company.tenant_id == tenant_id)
    ):
        if normalise_name(existing) == target:
            return True
    return False


def _upsert(
    session: Session,
    candidate: Candidate,
    band: str | None,
    report: BuildReport,
    tenant_id: int,
) -> None:
    today = _today()
    entry = session.scalar(
        select(DirectoryEntry).where(
            DirectoryEntry.tenant_id == tenant_id,
            DirectoryEntry.source == candidate.source,
            DirectoryEntry.source_ref == candidate.source_ref,
        )
    )
    keywords = ", ".join(sorted(candidate.keywords)) or None
    examples = "; ".join(candidate.examples[:8]) or None

    if entry is None:
        session.add(
            DirectoryEntry(
                tenant_id=tenant_id,
                name=candidate.name,
                country=candidate.country,
                size_band=band,
                device_count=candidate.device_count,
                device_count_is_floor=candidate.device_count_is_floor,
                highest_risk_class=candidate.highest_risk_class,
                device_keywords=keywords,
                example_devices=examples,
                source=candidate.source,
                source_ref=candidate.source_ref,
                detail_url=candidate.detail_url,
                first_seen=today,
                last_seen=today,
                created_by="directory",
            )
        )
        report.entries_added += 1
        return

    # Refresh what we know without resetting your own review state.
    entry.name = candidate.name or entry.name
    entry.country = candidate.country or entry.country
    entry.size_band = band or entry.size_band
    if candidate.device_count is not None:
        entry.device_count = candidate.device_count
        entry.device_count_is_floor = candidate.device_count_is_floor
    entry.highest_risk_class = _worst(entry.highest_risk_class, candidate.highest_risk_class)
    entry.device_keywords = keywords or entry.device_keywords
    entry.example_devices = examples or entry.example_devices
    entry.last_seen = today
    report.entries_updated += 1


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _sweep_eudamed_class(
    risk_class: str, out: dict[str, Candidate], report: BuildReport, icp: dict
) -> None:
    """Walk one IVDR risk class, collecting distinct manufacturers.

    Device counts from this sweep are always a FLOOR: we count the devices we
    happened to walk past, not the manufacturer's true total. Marked as such so
    the size band is never mistaken for a measurement.
    """
    page_size = int(icp.get("page_size", 20))
    max_pages = int(icp.get("max_pages_per_class", 150))
    territories = {t.upper() for t in icp.get("territories", []) or []}

    for page in range(max_pages):
        payload, error = fetch(
            "eudamed", EUDAMED_URL,
            params={
                "riskClassCode": f"refdata.risk-class.{risk_class}",
                "page": page,
                "pageSize": page_size,
            },
            timeout=float(icp.get("timeout_seconds", 90)),
        )
        report.queries_made += 1
        if error:
            report.errors.append(error)
            return
        if not payload:
            return

        rows = [r for r in payload.get("content", []) if isinstance(r, dict)]
        if not rows:
            return

        for row in rows:
            name = (row.get("manufacturerName") or "").strip()
            srn = (row.get("manufacturerSrn") or "").strip()
            if not name:
                continue

            country = None
            match = _SRN_COUNTRY.match(srn)
            if match:
                country = match.group(1)
            # Skip out-of-territory rows here rather than accumulating tens of
            # thousands of Chinese manufacturers just to discard them later.
            if territories and country and country.upper() not in territories:
                continue

            key = f"eudamed:{srn or normalise_name(name)}"
            existing = out.get(key)
            if existing is None:
                out[key] = Candidate(
                    name=name,
                    source="eudamed",
                    source_ref=srn or normalise_name(name),
                    country=country,
                    device_count=1,
                    device_count_is_floor=True,
                    highest_risk_class=_risk_of(row),
                    keywords={f"IVDR {risk_class.replace('class-', '').upper()}"},
                    examples=[t for t in [(row.get("tradeName") or "").strip()] if t],
                    detail_url=EUDAMED_UI + name.replace(" ", "%20"),
                )
            else:
                existing.device_count = (existing.device_count or 0) + 1
                existing.device_count_is_floor = True
                existing.highest_risk_class = _worst(
                    existing.highest_risk_class, _risk_of(row)
                )
                existing.keywords.add(f"IVDR {risk_class.replace('class-', '').upper()}")
                trade = (row.get("tradeName") or "").strip()
                if trade and trade not in existing.examples and len(existing.examples) < 8:
                    existing.examples.append(trade)

        if payload.get("last") or len(rows) < page_size:
            return


def _collect_eudamed(
    keyword: str, out: dict[str, Candidate], report: BuildReport, icp: dict
) -> None:
    page_size = int(icp.get("page_size", 20))
    max_pages = int(icp.get("max_pages_per_keyword", 8))

    for page in range(max_pages):
        payload, error = fetch(
            "eudamed", EUDAMED_URL,
            params={"name": keyword, "page": page, "pageSize": page_size},
            timeout=float(icp.get("timeout_seconds", 90)),
        )
        report.queries_made += 1
        if error:
            report.errors.append(error)
            return
        if not payload:
            return

        rows = [r for r in payload.get("content", []) if isinstance(r, dict)]
        if not rows:
            return

        for row in rows:
            name = (row.get("manufacturerName") or "").strip()
            if not name:
                continue
            srn = (row.get("manufacturerSrn") or "").strip()
            key = f"eudamed:{srn or normalise_name(name)}"
            country = None
            match = _SRN_COUNTRY.match(srn)
            if match:
                country = match.group(1)

            candidate = Candidate(
                name=name,
                source="eudamed",
                source_ref=srn or normalise_name(name),
                country=country,
                highest_risk_class=_risk_of(row),
                keywords={keyword},
                examples=[t for t in [(row.get("tradeName") or "").strip()] if t],
                detail_url=EUDAMED_UI + name.replace(" ", "%20"),
            )
            if key in out:
                out[key].merge(candidate)
            else:
                out[key] = candidate

        if payload.get("last") or len(rows) < page_size:
            return


def _collect_mhra(
    keyword: str, out: dict[str, Candidate], report: BuildReport, icp: dict
) -> None:
    payload, error = fetch("mhra", MHRA_URL, json_body={"searchTerm": keyword})
    report.queries_made += 1
    if error:
        report.errors.append(error)
        return
    if not isinstance(payload, list):
        return

    for row in payload:
        if not isinstance(row, dict):
            continue
        name = (row.get("MAN_ORGANISATION_NAME") or "").strip()
        if not name:
            continue
        org_id = row.get("MAN_ORGANISATION_ID")
        key = f"mhra:{org_id or normalise_name(name)}"

        candidate = Candidate(
            name=name,
            source="mhra",
            source_ref=str(org_id) if org_id else normalise_name(name),
            country=_country_code(row.get("MAN_COUNTRY")),
            keywords={keyword},
            detail_url=f"{MHRA_UI}{org_id}" if org_id else None,
        )
        if key in out:
            out[key].merge(candidate)
        else:
            out[key] = candidate


def _risk_of(row: dict) -> str | None:
    code = (row.get("riskClass") or {}).get("code", "")
    for name in _RISK_ORDER:
        if code.endswith(name):
            return name
    return None


def _worst(a: str | None, b: str | None) -> str | None:
    ranked = [x for x in (a, b) if x in _RISK_ORDER]
    if not ranked:
        return a or b
    return max(ranked, key=_RISK_ORDER.index)


# Only what the registers actually emit. Anything unrecognised stays unknown
# rather than being guessed at.
_COUNTRY_NAMES = {
    "united kingdom": "GB", "england, united kingdom": "GB",
    "scotland, united kingdom": "GB", "wales, united kingdom": "GB",
    "northern ireland, united kingdom": "GB",
    "ireland": "IE", "germany": "DE", "france": "FR", "netherlands": "NL",
    "belgium": "BE", "switzerland": "CH", "sweden": "SE", "denmark": "DK",
    "spain": "ES", "italy": "IT", "austria": "AT", "norway": "NO",
    "finland": "FI", "poland": "PL", "portugal": "PT", "czech republic": "CZ",
    "united states": "US", "china": "CN", "singapore": "SG", "japan": "JP",
}


def _country_code(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split()).strip().casefold()
    if len(text) == 2:
        return text.upper()
    return _COUNTRY_NAMES.get(text)


# ---------------------------------------------------------------------------
# Reading — filters, never a rank
# ---------------------------------------------------------------------------


@dataclass
class DirectoryFilters:
    territory: str | None = None
    keyword: str | None = None
    size: str | None = None
    include_dismissed: bool = False
    page: int = 1


@dataclass
class DirectoryPage:
    entries: list[DirectoryEntry]
    total: int
    page: int
    pages: int
    per_page: int
    territories: list[str]
    keywords: list[str]
    sizes: list[str]


def browse(
    session: Session,
    filters: DirectoryFilters | None = None,
    *,
    tenant_id: int | None = None,
) -> DirectoryPage:
    """Filtered, alphabetical, paginated. Deliberately not ranked."""
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    filters = filters or DirectoryFilters()
    per_page = int(cfg.icp.get("page_size_display", 50))

    where = [DirectoryEntry.tenant_id == tenant_id]
    if not filters.include_dismissed:
        where.append(DirectoryEntry.dismissed.is_(False))
    if filters.territory:
        where.append(DirectoryEntry.country == filters.territory.upper())
    if filters.keyword:
        where.append(DirectoryEntry.device_keywords.contains(filters.keyword))
    if filters.size:
        if filters.size == "unknown":
            where.append(DirectoryEntry.size_band.is_(None))
        else:
            where.append(DirectoryEntry.size_band == filters.size)

    total = int(session.scalar(
        select(func.count()).select_from(DirectoryEntry).where(*where)
    ) or 0)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, filters.page), pages)

    entries = list(session.scalars(
        select(DirectoryEntry)
        .where(*where)
        # Alphabetical, because there is no honest ranking to apply.
        .order_by(func.lower(DirectoryEntry.name))
        .offset((page - 1) * per_page)
        .limit(per_page)
    ))

    return DirectoryPage(
        entries=entries,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        territories=_distinct_territories(session, tenant_id),
        keywords=list(cfg.icp.get("device_keywords", []) or []),
        sizes=[str(b.get("name")) for b in cfg.icp.get("size_bands", []) or []] + ["unknown"],
    )


def _distinct_territories(session: Session, tenant_id: int) -> list[str]:
    rows = session.scalars(
        select(DirectoryEntry.country)
        .where(DirectoryEntry.tenant_id == tenant_id, DirectoryEntry.country.is_not(None))
        .distinct()
        .order_by(DirectoryEntry.country)
    )
    return [r for r in rows if r]


def directory_total(session: Session, *, tenant_id: int | None = None) -> int:
    tenant_id = tenant_id if tenant_id is not None else get_config().tenant_id
    return int(session.scalar(
        select(func.count()).select_from(DirectoryEntry).where(
            DirectoryEntry.tenant_id == tenant_id,
            DirectoryEntry.dismissed.is_(False),
        )
    ) or 0)


def set_entry_state(
    session: Session, entry_id: int, *, reviewed: bool | None = None,
    dismissed: bool | None = None, notes: str | None = None,
) -> DirectoryEntry | None:
    entry = session.get(DirectoryEntry, entry_id)
    if entry is None:
        return None
    if reviewed is not None:
        entry.reviewed = reviewed
    if dismissed is not None:
        entry.dismissed = dismissed
    if notes is not None:
        entry.notes = notes.strip() or None
    session.flush()
    return entry
