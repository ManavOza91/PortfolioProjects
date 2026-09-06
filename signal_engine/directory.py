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
from .detect.base import SourceError, country_code, fetch
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

# The registers this module can walk, and what to call them on screen.
DIRECTORY_SOURCES = {
    "eudamed": "EUDAMED (EU device registrations)",
    "mhra": "MHRA PARD (UK device registrations)",
    "openfda": "openFDA (US device registrations)",
}

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

    # openFDA's owner/operator, the only key here that tracks corporate scale.
    # A registration number counts one SITE, so a multi-site giant fragments into
    # many small-looking establishments — Stryker's Puerto Rico site lists 67
    # while TECO Diagnostics lists 190. Per owner they separate: 3364 vs 207.
    owner_ref: str | None = None

    # A band the source worked out for itself, on its own scale. openFDA counts
    # listings; EUDAMED counts devices. Set here rather than derived by size_band()
    # so the two units are never silently compared.
    size_band_hint: str | None = None

    def merge(self, other: "Candidate") -> None:
        """Two keyword searches can find the same manufacturer. Keep the richer view."""
        self.country = self.country or other.country
        self.owner_ref = self.owner_ref or other.owner_ref
        self.size_band_hint = self.size_band_hint or other.size_band_hint
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
            f"  {self.skipped_out_of_territory:>4}  outside your territories, or excluded",
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

    # openFDA filters by country server-side, so this leg is fast and targeted
    # rather than a walk-and-discard.
    if "openfda" in sources:
        _collect_openfda(candidates, report, icp)

    # Keyword search adds labels, and occasionally a company the sweep missed.
    for keyword in keywords:
        if "eudamed" in sources:
            _collect_eudamed(keyword, candidates, report, icp)
        if "mhra" in sources:
            _collect_mhra(keyword, candidates, report, icp)

    report.candidates_found = len(candidates)

    territories = {t.upper() for t in icp.get("territories", []) or []}
    excluded = {t.upper() for t in icp.get("exclude_territories", []) or []}
    target_bands = set(icp.get("target_size_bands", []) or [])

    for candidate in candidates.values():
        if not _in_territory(candidate.country, territories, excluded):
            report.skipped_out_of_territory += 1
            continue
        # An unknown country is NOT grounds for exclusion — absence of evidence.

        # A source that worked out its own band on its own scale wins; only the
        # EUDAMED device-count path falls through to size_band().
        band = candidate.size_band_hint or size_band(
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


def regions() -> list[dict]:
    return list(get_config().icp.get("regions", []) or [])


def region_names() -> list[str]:
    return [str(r.get("name")) for r in regions() if r.get("name")]


def catch_all_region() -> str | None:
    for rule in regions():
        if rule.get("catch_all"):
            return str(rule.get("name"))
    return None


def region_for(country: str | None) -> str | None:
    """Which working list a company belongs in.

    Derived at read time rather than stored, so editing the region config takes
    effect immediately instead of needing a re-sweep of every register.

    An unknown country lands in the catch-all region. That keeps it reachable —
    the alternative is a company that exists in the database and appears on no
    tab at all. The row still displays its territory as "unknown"; being in
    "Rest of world" is a filing decision, not a claim about where it is.
    """
    code = (country or "").upper()
    if code:
        for rule in regions():
            if code in {c.upper() for c in rule.get("countries", []) or []}:
                return str(rule.get("name"))
    return catch_all_region()


def countries_in_region(name: str) -> tuple[list[str], bool]:
    """(country codes, is_catch_all) for a region name."""
    for rule in regions():
        if str(rule.get("name")) == name:
            return (
                [str(c).upper() for c in rule.get("countries", []) or []],
                bool(rule.get("catch_all")),
            )
    return [], False


def _region_clause(name: str):
    """A SQL condition selecting one region, including the catch-all's leftovers."""
    codes, is_catch_all = countries_in_region(name)
    if not is_catch_all:
        return DirectoryEntry.country.in_(codes)

    claimed: set[str] = set()
    for rule in regions():
        if not rule.get("catch_all"):
            claimed |= {str(c).upper() for c in rule.get("countries", []) or []}
    return or_(
        DirectoryEntry.country.is_(None),
        DirectoryEntry.country.not_in(sorted(claimed)),
    )


def _in_territory(
    country: str | None, allowed: set[str], excluded: set[str]
) -> bool:
    """One rule, used by both the sweep and the write path so they can't diverge.

    An unknown country always passes. We cannot prove it is somewhere you don't
    sell, and dropping it would quietly turn "unknown" into a reason to exclude.
    """
    if not country:
        return True
    code = country.upper()
    if allowed and code not in allowed:
        return False
    return code not in excluded


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
    excluded = {t.upper() for t in icp.get("exclude_territories", []) or []}

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
            # Apply the territory rule here rather than accumulating tens of
            # thousands of rows just to discard them at the end. Note this does not
            # make the run faster — the register is still paged through in full —
            # it just raises how much of each page is worth keeping.
            if not _in_territory(country, territories, excluded):
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


def _collect_openfda(
    out: dict[str, Candidate], report: BuildReport, icp: dict
) -> None:
    """US (and any other) device registrations, filtered to the IVD regulation parts.

    21 CFR 862 / 864 / 866 are the in-vitro diagnostic parts, so asking for them is
    asking for diagnostics — the same move as IVDR risk class in EUDAMED.

    Unlike EUDAMED, this endpoint has a working country filter, so the sweep asks
    for the territories you want rather than walking everything and discarding it.
    """
    src = icp.get("openfda", {}) or {}
    if not src.get("enabled", True):
        return

    url = str(src.get("url", "https://api.fda.gov/device/registrationlisting.json"))
    parts = [str(p) for p in src.get("ivd_regulation_parts", ["862", "864", "866"])]
    page_size = int(src.get("page_size", 100))
    max_pages = int(src.get("max_pages_per_part", 20))

    territories = [t.upper() for t in icp.get("territories", []) or []]
    excluded = {t.upper() for t in icp.get("exclude_territories", []) or []}
    wanted = [t for t in territories if t not in excluded]

    for part in parts:
        query = f"products.openfda.regulation_number:{part}*"
        if wanted:
            joined = "+OR+".join(f'registration.iso_country_code:"{c}"' for c in wanted)
            query = f"{query}+AND+({joined})"

        for page in range(max_pages):
            payload, error = fetch(
                "openfda", url,
                params={"search": query, "limit": page_size, "skip": page * page_size},
            )
            report.queries_made += 1
            if error:
                report.errors.append(error)
                break
            if not payload:
                break

            rows = payload.get("results", []) or []
            if not rows:
                break

            wanted_types = [
                str(t) for t in src.get("establishment_types", []) or []
            ]
            for row in rows:
                if not _is_a_maker(row, wanted_types):
                    continue
                _absorb_openfda_row(row, part, out, excluded)

            if len(rows) < page_size:
                break

    # Sizes last, once every establishment is known, so each owner is asked about
    # exactly once however many of its sites turned up.
    _enrich_openfda_sizes(out, report, src)


def listing_size_band(listings: int | None) -> str | None:
    """Size from an openFDA OWNER's listing count. A separate scale from size_band().

    size_band() counts EUDAMED device registrations; this counts US product
    listings. Same words, different units — so they get different tables and are
    never compared.
    """
    if listings is None:
        return None
    bands = (
        get_config().icp.get("openfda", {}).get("size_bands", []) or []
    )
    for rule in bands:
        ceiling = rule.get("max_listings")
        if ceiling is None or listings <= int(ceiling):
            return str(rule.get("name"))
    return None


def _enrich_openfda_sizes(
    out: dict[str, Candidate], report: BuildReport, src: dict
) -> None:
    """Ask openFDA how many listings each OWNER holds, and set a real size band.

    This is the one place any source can give a true total rather than a floor, so
    it is the one place a size band below `large` can be trusted. One query per
    distinct owner, not per company — a giant's many sites share one owner.

    Everything it cannot resolve is left alone and stays unknown.
    """
    if not src.get("fetch_true_counts", True):
        return

    url = str(src.get("url", ""))
    budget = int(src.get("max_count_lookups", 400))

    owners: dict[str, list[Candidate]] = {}
    for candidate in out.values():
        if candidate.source == "openfda" and candidate.owner_ref:
            owners.setdefault(candidate.owner_ref, []).append(candidate)

    for owner, candidates in list(owners.items())[:budget]:
        payload, error = fetch(
            "openfda", url,
            params={
                "search": f'products.owner_operator_number:"{owner}"',
                "limit": 1,
            },
        )
        report.queries_made += 1
        if error:
            report.errors.append(error)
            return
        if not payload:
            continue

        total = (payload.get("meta", {}).get("results", {}) or {}).get("total")
        if not total:
            continue

        band = listing_size_band(int(total))
        for candidate in candidates:
            candidate.device_count = int(total)
            # A real total, not a floor — so the band below `large` is trustworthy.
            candidate.device_count_is_floor = False
            candidate.size_band_hint = band


def _is_a_maker(row: dict, wanted_types: list[str]) -> bool:
    """Does this establishment actually make or design the device?

    The register lists everyone who touches a device — repackagers, importers,
    exporters, and addresses that merely hold complaint files. Without this filter
    a US sweep returns logistics firms and dental labs alongside assay makers.

    No configured types, or a row with no establishment type at all, passes. The
    first is "the customer hasn't asked to filter"; the second is unknown, and
    unknown is not grounds for exclusion.
    """
    if not wanted_types:
        return True

    declared = row.get("establishment_type") or []
    if isinstance(declared, str):
        declared = [declared]
    # openFDA sends [None], not [], when it has nothing to say. Stringifying that
    # gives "none", which matches no wanted type and silently drops the row —
    # turning "we don't know" into "excluded". Strip the empties first.
    declared = [d for d in declared if d]
    if not declared:
        return True

    haystack = " | ".join(str(d) for d in declared).casefold()
    return any(str(t).casefold() in haystack for t in wanted_types)


def _absorb_openfda_row(
    row: dict, part: str, out: dict[str, Candidate], excluded: set[str]
) -> None:
    registration = row.get("registration") or {}
    name = (registration.get("name") or "").strip()
    if not name:
        return

    country = (registration.get("iso_country_code") or "").strip().upper() or None
    if country and country in excluded:
        return

    reg_number = (registration.get("registration_number") or "").strip()

    proprietary = row.get("proprietary_name") or []
    if isinstance(proprietary, str):
        proprietary = [proprietary]
    examples = [str(p).strip() for p in proprietary[:3] if str(p).strip()]

    products = row.get("products") or []
    owner_ref = None
    for product in products:
        owner_ref = (product or {}).get("owner_operator_number")
        if owner_ref:
            owner_ref = str(owner_ref).strip() or None
            break

    # Identity is the OWNER, not the site. Sterigenics registers eight US
    # establishments; the directory is one row per company, not per address.
    identity = owner_ref or reg_number or normalise_name(name)
    key = f"openfda:{identity}"

    candidate = Candidate(
        name=name,
        source="openfda",
        source_ref=identity,
        country=country,
        device_count=1,
        device_count_is_floor=True,
        owner_ref=owner_ref,
        keywords={f"21 CFR {part}"},
        examples=examples,
        detail_url=(
            "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRL/rl.cfm?lid="
            f"{reg_number}" if reg_number else None
        ),
    )
    existing = out.get(key)
    if existing is None:
        out[key] = candidate
    else:
        existing.device_count = (existing.device_count or 0) + 1
        existing.device_count_is_floor = True
        existing.merge(candidate)


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
            country=country_code(row.get("MAN_COUNTRY")),
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


# ---------------------------------------------------------------------------
# Reading — filters, never a rank
# ---------------------------------------------------------------------------


@dataclass
class DirectoryFilters:
    region: str | None = None
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
    region: str | None = None
    region_counts: dict[str, int] = field(default_factory=dict)


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

    # Counts for the tab bar are taken BEFORE the region filter, so every tab
    # shows its own size regardless of which one you're looking at.
    base = list(where)
    region_counts = {
        name: int(session.scalar(
            select(func.count()).select_from(DirectoryEntry)
            .where(*base, _region_clause(name))
        ) or 0)
        for name in region_names()
    }

    if filters.region:
        where.append(_region_clause(filters.region))
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
        region=filters.region,
        region_counts=region_counts,
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
