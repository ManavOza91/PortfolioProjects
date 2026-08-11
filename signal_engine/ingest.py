"""Write-side services: turn parsed notes into companies, signals, contacts, activity.

Two rules govern everything here:

  * The raw human text is stored alongside every parsed output, always — including
    when parsing fails or finds nothing. You will want to audit which observations
    actually predicted outcomes, and that is impossible if only the parsed form survives.

  * A note with nothing scoreable in it produces an `unscored` signal record, not a
    fabricated number. The observation is kept; the score is not invented.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_config
from .llm.client import ParseOutcome
from .llm.parser import clamp_confidence, coerce_date, parse_entry, parse_manager_insight
from .models import Activity, Company, Contact, ManagerInsight, Signal, SignalType
from .scoring import rescore_company

_LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc", "corp",
    "corporation", "co", "company", "gmbh", "ag", "bv", "nv", "sa", "sas",
    "srl", "spa", "ab", "as", "oy", "aps", "pty", "kk", "kg", "sarl", "sl",
    "holdings", "group", "the",
}


def normalise_name(name: str | None) -> str:
    """Match 'Acme Diagnostics Ltd.' to 'ACME diagnostics' without a fuzzy library."""
    if not name:
        return ""
    text = re.sub(r"[^\w\s]", " ", name.lower())
    tokens = [t for t in text.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def normalise_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].strip()
    return d or None


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def find_company(
    session: Session,
    *,
    name: str | None = None,
    domain: str | None = None,
    tenant_id: int | None = None,
) -> Company | None:
    tenant_id = tenant_id if tenant_id is not None else get_config().tenant_id

    dom = normalise_domain(domain)
    if dom:
        hit = session.scalar(
            select(Company).where(Company.tenant_id == tenant_id, Company.domain == dom)
        )
        if hit:
            return hit

    key = normalise_name(name)
    if not key:
        return None
    for company in session.scalars(select(Company).where(Company.tenant_id == tenant_id)):
        if normalise_name(company.name) == key:
            return company
    return None


def find_or_create_company(
    session: Session,
    *,
    name: str,
    domain: str | None = None,
    country: str | None = None,
    segment: str | None = None,
    size_band: str | None = None,
    status: str = "watchlist",
    created_by: str | None = None,
    tenant_id: int | None = None,
) -> tuple[Company, bool]:
    """Return (company, created). Enriches an existing record with new detail."""
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id

    company = find_company(session, name=name, domain=domain, tenant_id=tenant_id)
    if company is not None:
        # Fill blanks without overwriting anything already known.
        company.domain = company.domain or normalise_domain(domain)
        company.country = company.country or country
        company.segment = company.segment or segment
        company.size_band = company.size_band or size_band
        return company, False

    company = Company(
        tenant_id=tenant_id,
        name=name.strip(),
        domain=normalise_domain(domain),
        country=country,
        segment=segment,
        size_band=size_band,
        status=status,
        created_by=created_by or cfg.user_name,
    )
    session.add(company)
    session.flush()
    return company, True


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _status_for(source: str, confidence: float) -> str:
    """Where a signal lands: scored, or held for review.

    Automatically detected signals face a higher bar than things you saw yourself.
    Ten confirmed signals beat a hundred uncertain ones.
    """
    cfg = get_config()
    if source == "auto":
        threshold = cfg.auto_review_threshold
    else:
        threshold = float(cfg.scoring.get("manual_review_threshold", 0.45))
    return "scored" if confidence >= threshold else "review"


def create_signal(
    session: Session,
    company: Company,
    *,
    type_key: str | None,
    source: str = "manual",
    raw_text: str | None = None,
    parsed_summary: str | None = None,
    confidence: float = 1.0,
    detected_date: dt.date | None = None,
    expiry_date: dt.date | None = None,
    product_fit: str | None = None,
    weight: float | None = None,
    status: str | None = None,
    created_by: str | None = None,
    detail_url: str | None = None,
    timeline: str | None = None,
) -> Signal:
    cfg = get_config()
    confidence = clamp_confidence(confidence)
    detected_date = detected_date or _today()

    spec = None
    if type_key:
        spec = session.scalar(
            select(SignalType).where(
                SignalType.tenant_id == company.tenant_id, SignalType.key == type_key
            )
        )

    if status is None:
        # No recognised type means nothing scoreable was found. Store it, do not
        # score it, and never invent a number for it.
        status = "unscored" if spec is None else _status_for(source, confidence)

    signal = Signal(
        tenant_id=company.tenant_id,
        company_id=company.id,
        type_key=type_key if spec else None,
        weight=weight if weight is not None else (spec.base_weight if spec else 0.0),
        product_fit=product_fit or (spec.product_fit if spec else None),
        source=source,
        raw_text=raw_text,
        parsed_summary=parsed_summary,
        confidence=confidence,
        detected_date=detected_date,
        expiry_date=expiry_date,
        status=status,
        created_by=created_by or cfg.user_name,
        detail_url=detail_url,
        timeline=timeline,
    )
    session.add(signal)
    session.flush()
    return signal


# ---------------------------------------------------------------------------
# The three entry points
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    company: Company | None = None
    company_created: bool = False
    contact: Contact | None = None
    signals: list[Signal] = field(default_factory=list)
    unscored: bool = False
    parse_ok: bool = True
    parse_error: str | None = None
    reasoning: str | None = None
    score_before: float = 0.0
    score_after: float = 0.0
    tier: str | None = None

    @property
    def scored_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.status == "scored"]

    @property
    def review_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.status == "review"]

    def message(self) -> str:
        if not self.parse_ok:
            return (
                "Saved your note in full, but could not parse it: "
                f"{self.parse_error} You can re-parse it later from the company page."
            )
        if self.unscored:
            return (
                "Saved. Nothing scoreable found in this note, so no score was "
                "assigned. " + (self.reasoning or "")
            ).strip()
        bits = []
        if self.scored_signals:
            bits.append(f"{len(self.scored_signals)} signal(s) scored")
        if self.review_signals:
            bits.append(f"{len(self.review_signals)} held for review")
        delta = self.score_after - self.score_before
        if delta:
            bits.append(f"score {self.score_before:.0f} → {self.score_after:.0f}")
        if self.tier:
            bits.append(f"Tier {self.tier}")
        return "Saved. " + ", ".join(bits) + "."


def ingest_note(
    session: Session,
    *,
    note: str,
    company_name: str | None = None,
    company_domain: str | None = None,
    contact_name: str | None = None,
    contact_linkedin: str | None = None,
    contact_email: str | None = None,
    contact_title: str | None = None,
    source: str = "manual",
    detected_date: dt.date | None = None,
    created_by: str | None = None,
    outcome: ParseOutcome | None = None,
) -> IngestResult:
    """Entry points 1 and 2: a company/signal note, or a contact plus a note.

    `outcome` lets callers (and tests) inject a parse result instead of calling out.
    """
    cfg = get_config()
    result = IngestResult()
    note = (note or "").strip()

    hint_bits = [b for b in (contact_name, contact_title, contact_email, contact_linkedin) if b]
    if outcome is None:
        outcome = parse_entry(
            note,
            company_hint=company_name,
            contact_hint=", ".join(hint_bits) or None,
        )

    parsed = outcome.data
    result.parse_ok = outcome.ok
    result.parse_error = outcome.error
    if parsed is not None:
        result.reasoning = getattr(parsed, "reasoning", None)

    # --- company ---------------------------------------------------------------
    resolved_name = company_name or (getattr(parsed, "company_name", None) if parsed else None)
    if not resolved_name:
        result.unscored = True
        result.reasoning = (
            result.reasoning
            or "No company could be identified in this note, so nothing was created."
        )
        return result

    company, created = find_or_create_company(
        session,
        name=resolved_name,
        domain=company_domain or (getattr(parsed, "company_domain", None) if parsed else None),
        country=getattr(parsed, "company_country", None) if parsed else None,
        segment=getattr(parsed, "company_segment", None) if parsed else None,
        created_by=created_by,
    )
    result.company = company
    result.company_created = created
    result.score_before = company.current_score

    # --- contact (optional layer, never required) ------------------------------
    contact_fields = {
        "full_name": contact_name or (getattr(parsed, "contact_full_name", None) if parsed else None),
        "job_title": contact_title or (getattr(parsed, "contact_job_title", None) if parsed else None),
        "email": contact_email or (getattr(parsed, "contact_email", None) if parsed else None),
        "linkedin_url": contact_linkedin
        or (getattr(parsed, "contact_linkedin_url", None) if parsed else None),
        "persona": getattr(parsed, "contact_persona", None) if parsed else None,
    }
    if any(contact_fields.values()):
        result.contact = upsert_contact(
            session, company, created_by=created_by, notes=note, **contact_fields
        )

    # --- signals ---------------------------------------------------------------
    parsed_signals = list(getattr(parsed, "signals", []) or []) if parsed else []

    if not outcome.ok:
        # Parsing was unavailable. Keep the observation whole and flag it so it can
        # be re-parsed; never discard a human observation because of a network error.
        result.signals.append(
            create_signal(
                session,
                company,
                type_key=None,
                source=source,
                raw_text=note,
                parsed_summary=None,
                confidence=0.0,
                detected_date=detected_date,
                status="unscored",
                created_by=created_by,
            )
        )
        result.unscored = True
    elif not parsed_signals:
        result.signals.append(
            create_signal(
                session,
                company,
                type_key=None,
                source=source,
                raw_text=note,
                parsed_summary=result.reasoning,
                confidence=0.0,
                detected_date=detected_date,
                status="unscored",
                created_by=created_by,
            )
        )
        result.unscored = True
    else:
        for ps in parsed_signals:
            result.signals.append(
                create_signal(
                    session,
                    company,
                    type_key=getattr(ps, "type_key", None),
                    source=source,
                    raw_text=note,
                    parsed_summary=getattr(ps, "summary", None),
                    confidence=clamp_confidence(
                        getattr(ps, "confidence", cfg.manual_default_confidence)
                    ),
                    detected_date=detected_date
                    or coerce_date(getattr(ps, "detected_date", None)),
                    product_fit=getattr(ps, "product_fit", None),
                    timeline=getattr(ps, "timeline", None),
                    created_by=created_by,
                )
            )

    breakdown = rescore_company(session, company)
    result.score_after = company.current_score
    result.tier = breakdown.tier
    session.flush()
    return result


def upsert_contact(
    session: Session,
    company: Company,
    *,
    full_name: str | None = None,
    job_title: str | None = None,
    email: str | None = None,
    linkedin_url: str | None = None,
    country: str | None = None,
    persona: str | None = None,
    source: str | None = "manual",
    notes: str | None = None,
    created_by: str | None = None,
) -> Contact:
    """Contacts are a thin, optional layer. Matched on email, then LinkedIn, then name."""
    cfg = get_config()
    existing = None
    candidates = list(
        session.scalars(select(Contact).where(Contact.company_id == company.id))
    )
    for c in candidates:
        if email and c.email and c.email.lower() == email.lower():
            existing = c
            break
        if linkedin_url and c.linkedin_url and c.linkedin_url.rstrip("/") == linkedin_url.rstrip("/"):
            existing = c
            break
        if full_name and c.full_name and normalise_name(c.full_name) == normalise_name(full_name):
            existing = c
            break

    if existing is None:
        existing = Contact(
            tenant_id=company.tenant_id,
            company_id=company.id,
            date_added=_today(),
            source=source,
            created_by=created_by or cfg.user_name,
        )
        session.add(existing)

    existing.full_name = full_name or existing.full_name
    existing.job_title = job_title or existing.job_title
    existing.email = email or existing.email
    existing.linkedin_url = linkedin_url or existing.linkedin_url
    existing.country = country or existing.country or company.country
    existing.persona = persona or existing.persona
    if notes:
        existing.notes = f"{existing.notes}\n\n{notes}".strip() if existing.notes else notes
    session.flush()
    return existing


def ingest_manager_insight(
    session: Session,
    *,
    text: str,
    author: str | None = None,
    outcome: ParseOutcome | None = None,
) -> tuple[ManagerInsight, ParseOutcome]:
    """Entry point 3. Rules expire by default so stale intuitions age out."""
    cfg = get_config()
    text = (text or "").strip()
    if outcome is None:
        outcome = parse_manager_insight(text)

    parsed = outcome.data
    expiry_days = int(getattr(parsed, "expiry_days", 90) or 90) if parsed else 90
    nothing = bool(getattr(parsed, "nothing_scoreable", False)) if parsed else True

    insight = ManagerInsight(
        tenant_id=cfg.tenant_id,
        raw_text=text,
        parsed_segment=getattr(parsed, "segment", None) if parsed else None,
        parsed_geography=getattr(parsed, "geography", None) if parsed else None,
        parsed_signal_type=getattr(parsed, "signal_type", None) if parsed else None,
        parsed_summary=getattr(parsed, "summary", None) if parsed else None,
        weight_adjustment=(
            0.0 if nothing else float(getattr(parsed, "weight_adjustment", 0.0) or 0.0)
        ),
        author=author or cfg.user_name,
        created_date=_today(),
        expiry_date=_today() + dt.timedelta(days=max(1, expiry_days)),
        active=bool(outcome.ok and not nothing),
    )
    session.add(insight)
    session.flush()
    return insight, outcome


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def log_activity(
    session: Session,
    *,
    company: Company,
    type: str,
    direction: str = "out",
    date: dt.date | None = None,
    outcome: str | None = None,
    notes: str | None = None,
    contact: Contact | None = None,
    signal_id: int | None = None,
    created_by: str | None = None,
) -> Activity:
    """Record one touch.

    The company's tier and governing signal are captured AT THE TIME OF CONTACT.
    Rescoring later must never rewrite the record of whether an outreach was
    signal-driven when it was sent.
    """
    cfg = get_config()
    date = date or _today()

    if signal_id is None:
        strongest = session.scalar(
            select(Signal)
            .where(Signal.company_id == company.id, Signal.status == "scored")
            .order_by(Signal.weight.desc(), Signal.detected_date.desc())
            .limit(1)
        )
        signal_id = strongest.id if strongest else None

    activity = Activity(
        tenant_id=company.tenant_id,
        company_id=company.id,
        contact_id=contact.id if contact else None,
        type=type,
        direction=direction,
        date=date,
        outcome=outcome,
        notes=notes,
        signal_id_at_time_of_contact=signal_id,
        company_tier_at_time_of_contact=company.tier,
        company_score_at_time_of_contact=company.current_score,
        created_by=created_by or cfg.user_name,
    )
    session.add(activity)
    session.flush()
    return activity


# ---------------------------------------------------------------------------
# Personal data removal
# ---------------------------------------------------------------------------


@dataclass
class PurgeResult:
    contacts_deleted: int
    activity_rows_kept: int
    activity_rows_detached: int


def purge_personal_data(session: Session, *, tenant_id: int | None = None) -> PurgeResult:
    """Delete every contact. Scoring, Buddy Score and reporting keep working.

    This is the operation that makes a contacts-free commercial edition viable, and
    it works because activity.company_id is NOT NULL while activity.contact_id is
    nullable with ON DELETE SET NULL.
    """
    tenant_id = tenant_id if tenant_id is not None else get_config().tenant_id

    contact_count = session.scalar(
        select(func.count(Contact.id)).where(Contact.tenant_id == tenant_id)
    ) or 0
    attached = session.scalar(
        select(func.count(Activity.id)).where(
            Activity.tenant_id == tenant_id, Activity.contact_id.is_not(None)
        )
    ) or 0

    session.execute(delete(Contact).where(Contact.tenant_id == tenant_id))
    session.flush()

    total_activity = session.scalar(
        select(func.count(Activity.id)).where(Activity.tenant_id == tenant_id)
    ) or 0

    return PurgeResult(
        contacts_deleted=contact_count,
        activity_rows_kept=total_activity,
        activity_rows_detached=attached,
    )
