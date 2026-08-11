"""FastAPI application.

Server-rendered HTML with plain forms. No build step, no npm, no CDN — it works
with the network off, which matters for a tool that is supposed to start with one
command on someone else's laptop.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..buddy import compute_buddy_score, compute_trend, rollups
from ..config import get_config, get_taxonomy
from ..db import get_db
from ..ingest import (
    create_signal,
    find_or_create_company,
    ingest_manager_insight,
    ingest_note,
    log_activity,
    upsert_contact,
)
from ..llm.client import api_key_present
from ..models import Activity, Company, Contact, ManagerInsight, Signal, SignalType
from ..reporting import (
    activity_summary,
    company_breakdown,
    ranked_queue,
    review_queue,
    signal_conversion,
    totals,
)
from ..scoring import rescore_company

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="Signal Engine", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

DbSession = Annotated[Session, Depends(get_db)]


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


@app.middleware("http")
async def add_globals(request: Request, call_next):
    return await call_next(request)


def _ctx(request: Request, **kw) -> dict:
    cfg = get_config()
    base = {
        "request": request,
        "cfg": cfg,
        "user_name": cfg.user_name,
        "today": _today(),
        "llm_ready": api_key_present() and cfg.llm.get("enabled", True),
        "flash": request.query_params.get("msg"),
        "flash_kind": request.query_params.get("kind", "ok"),
    }
    base.update(kw)
    return base


def _redirect(path: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    if msg:
        from urllib.parse import quote

        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}msg={quote(msg)}&kind={kind}"
    return RedirectResponse(path, status_code=303)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession):
    week = compute_buddy_score(db, period_type="week")
    trend = compute_trend(db, period_type="week")
    periods = rollups(db)
    queue = ranked_queue(db, limit=25)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            week=week,
            trend=trend,
            periods=periods,
            queue=queue,
            conversion=signal_conversion(db),
            activity=activity_summary(db, days=30),
            totals=totals(db),
            review_count=len(review_queue(db, limit=200)),
        ),
    )


@app.get("/companies", response_class=HTMLResponse)
def companies(
    request: Request,
    db: DbSession,
    tier: str | None = None,
    product: str | None = None,
    show_all: int = 0,
):
    queue = ranked_queue(
        db,
        limit=None,
        tiers={tier} if tier else None,
        product_fit=product or None,
        include_unsignalled=bool(show_all),
    )
    return templates.TemplateResponse(
        request,
        "companies.html",
        _ctx(
            request,
            queue=queue,
            tier=tier,
            product=product,
            show_all=show_all,
            totals=totals(db),
        ),
    )


@app.get("/company/{company_id}", response_class=HTMLResponse)
def company_detail(request: Request, db: DbSession, company_id: int):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")

    breakdown = company_breakdown(db, company)
    signals = list(
        db.scalars(
            select(Signal)
            .where(Signal.company_id == company_id)
            .order_by(Signal.detected_date.desc(), Signal.id.desc())
        )
    )
    contacts = list(db.scalars(select(Contact).where(Contact.company_id == company_id)))
    activities = list(
        db.scalars(
            select(Activity)
            .where(Activity.company_id == company_id)
            .order_by(Activity.date.desc(), Activity.id.desc())
        )
    )
    return templates.TemplateResponse(
        request,
        "company.html",
        _ctx(
            request,
            company=company,
            breakdown=breakdown,
            signals=signals,
            contacts=contacts,
            activities=activities,
            taxonomy=get_taxonomy(),
        ),
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, db: DbSession, company: str | None = None):
    recent = list(
        db.scalars(select(Company).order_by(Company.updated_at.desc()).limit(200))
    )
    insights = list(
        db.scalars(select(ManagerInsight).order_by(ManagerInsight.created_date.desc()).limit(10))
    )
    return templates.TemplateResponse(
        request,
        "add.html",
        _ctx(
            request,
            companies=recent,
            prefill_company=company or "",
            insights=insights,
            taxonomy=get_taxonomy(),
        ),
    )


@app.post("/add/note")
def add_note(
    db: DbSession,
    note: Annotated[str, Form()],
    company_name: Annotated[str, Form()] = "",
    company_domain: Annotated[str, Form()] = "",
    contact_name: Annotated[str, Form()] = "",
    contact_title: Annotated[str, Form()] = "",
    contact_email: Annotated[str, Form()] = "",
    contact_linkedin: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "manual",
    detected_date: Annotated[str, Form()] = "",
):
    if not note.strip():
        return _redirect("/add", "Nothing to save — the note was empty.", "warn")

    result = ingest_note(
        db,
        note=note,
        company_name=company_name.strip() or None,
        company_domain=company_domain.strip() or None,
        contact_name=contact_name.strip() or None,
        contact_title=contact_title.strip() or None,
        contact_email=contact_email.strip() or None,
        contact_linkedin=contact_linkedin.strip() or None,
        source=source,
        detected_date=_parse_date(detected_date),
    )
    if result.company is None:
        return _redirect("/add", result.message(), "warn")

    kind = "warn" if (result.unscored or not result.parse_ok) else "ok"
    return _redirect(f"/company/{result.company.id}", result.message(), kind)


@app.post("/add/signal")
def add_signal(
    db: DbSession,
    type_key: Annotated[str, Form()],
    company_name: Annotated[str, Form()],
    company_domain: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    segment: Annotated[str, Form()] = "",
    product_fit: Annotated[str, Form()] = "",
    detected_date: Annotated[str, Form()] = "",
    timeline: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "manual",
    notes: Annotated[str, Form()] = "",
):
    """Record a signal you have already worked out yourself. No parsing, no API call.

    This is the primary entry path. The taxonomy supplies the weight and the default
    product fit; everything the scoring engine does downstream is identical to a
    parsed signal.
    """
    company_name = company_name.strip()
    if not company_name:
        return _redirect("/add", "A signal needs a company.", "warn")

    spec = db.scalar(
        select(SignalType).where(
            SignalType.tenant_id == get_config().tenant_id,
            SignalType.key == type_key.strip(),
            SignalType.active.is_(True),
        )
    )
    if spec is None:
        return _redirect("/add", "Pick a signal type from the list.", "warn")

    company, created = find_or_create_company(
        db,
        name=company_name,
        domain=company_domain.strip() or None,
        country=country.strip() or None,
        segment=segment.strip() or None,
    )

    signal = create_signal(
        db,
        company,
        type_key=spec.key,
        source=source or "manual",
        raw_text=notes.strip() or None,
        parsed_summary=notes.strip() or spec.label,
        confidence=get_config().manual_default_confidence,
        detected_date=_parse_date(detected_date) or _today(),
        product_fit=product_fit.strip() or spec.product_fit,
        timeline=timeline.strip() or None,
    )
    breakdown = rescore_company(db, company)

    bits = [f"{spec.label} recorded ({signal.weight:g} pts)"]
    if created:
        bits.append("new company")
    bits.append(f"score now {breakdown.score:.0f}")
    if breakdown.tier:
        bits.append(f"Tier {breakdown.tier}")
    if breakdown.compounding_applied:
        bits.append(f"compounding ×{breakdown.multiplier:.2f}")

    return _redirect(f"/company/{company.id}", ", ".join(bits) + ".")


@app.post("/add/company")
def add_company(
    db: DbSession,
    name: Annotated[str, Form()],
    domain: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    segment: Annotated[str, Form()] = "",
):
    if not name.strip():
        return _redirect("/add", "A company needs a name.", "warn")
    company, created = find_or_create_company(
        db,
        name=name.strip(),
        domain=domain.strip() or None,
        country=country.strip() or None,
        segment=segment.strip() or None,
        status="watchlist",
    )
    msg = (
        f"Added {company.name} to the watchlist. It has no score — it enters the "
        "pipeline when a signal fires."
        if created
        else f"{company.name} was already known."
    )
    return _redirect(f"/company/{company.id}", msg)


@app.post("/add/insight")
def add_insight(db: DbSession, text: Annotated[str, Form()]):
    if not text.strip():
        return _redirect("/add", "Nothing to save — the insight was empty.", "warn")
    insight, outcome = ingest_manager_insight(db, text=text)
    if not outcome.ok:
        msg = f"Saved the text, but could not parse it: {outcome.error}"
        kind = "warn"
    elif not insight.active:
        msg = "Saved. No actionable segment rule found, so nothing was applied."
        kind = "warn"
    else:
        expiry = insight.expiry_date.isoformat() if insight.expiry_date else "n/a"
        msg = (
            f"Rule saved: {insight.parsed_summary} "
            f"(+{insight.weight_adjustment:g}, expires {expiry})"
        )
        kind = "ok"
    return _redirect("/add", msg, kind)


@app.post("/company/{company_id}/contact")
def add_contact(
    db: DbSession,
    company_id: int,
    full_name: Annotated[str, Form()] = "",
    job_title: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    linkedin_url: Annotated[str, Form()] = "",
    persona: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")
    if not any([full_name.strip(), email.strip(), linkedin_url.strip()]):
        return _redirect(
            f"/company/{company_id}", "A contact needs a name, email or LinkedIn URL.", "warn"
        )
    upsert_contact(
        db,
        company,
        full_name=full_name.strip() or None,
        job_title=job_title.strip() or None,
        email=email.strip() or None,
        linkedin_url=linkedin_url.strip() or None,
        persona=persona.strip() or None,
        notes=notes.strip() or None,
    )
    return _redirect(f"/company/{company_id}", "Contact saved.")


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@app.post("/company/{company_id}/activity")
def add_activity(
    db: DbSession,
    company_id: int,
    type: Annotated[str, Form()],
    direction: Annotated[str, Form()] = "out",
    date: Annotated[str, Form()] = "",
    outcome: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    contact_id: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")

    contact = None
    if contact_id.strip().isdigit():
        contact = db.get(Contact, int(contact_id))

    log_activity(
        db,
        company=company,
        type=type,
        direction=direction,
        date=_parse_date(date) or _today(),
        outcome=outcome.strip() or None,
        notes=notes.strip() or None,
        contact=contact,
    )
    tier = company.tier or "no tier"
    return _redirect(
        f"/company/{company_id}",
        f"Activity logged against Tier {tier} as it stands today.",
    )


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, db: DbSession):
    return templates.TemplateResponse(
        request,
        "review.html",
        _ctx(request, items=review_queue(db, limit=200), taxonomy=get_taxonomy()),
    )


@app.post("/review/{signal_id}")
def review_action(
    db: DbSession,
    signal_id: int,
    action: Annotated[str, Form()],
    type_key: Annotated[str, Form()] = "",
):
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(404, "Signal not found")
    company = db.get(Company, signal.company_id)

    if action == "reject":
        signal.status = "rejected"
        msg = "Signal rejected."
    elif action == "accept":
        chosen = type_key.strip() or signal.type_key
        if not chosen:
            return _redirect(
                "/review", "Pick a signal type before accepting this one.", "warn"
            )
        spec = db.scalar(
            select(SignalType).where(
                SignalType.tenant_id == signal.tenant_id, SignalType.key == chosen
            )
        )
        if spec is None:
            return _redirect("/review", f"Unknown signal type {chosen!r}.", "warn")
        signal.type_key = spec.key
        signal.weight = spec.base_weight
        signal.product_fit = signal.product_fit or spec.product_fit
        signal.confidence = max(signal.confidence, get_config().manual_default_confidence)
        signal.status = "scored"
        msg = f"Confirmed as {spec.label}."
    else:
        return _redirect("/review", f"Unknown action {action!r}.", "warn")

    if company is not None:
        rescore_company(db, company)
    return _redirect("/review", msg)


@app.post("/company/{company_id}/reparse/{signal_id}")
def reparse(db: DbSession, company_id: int, signal_id: int):
    """Retry parsing a note that was saved while the API was unavailable."""
    company = db.get(Company, company_id)
    signal = db.get(Signal, signal_id)
    if company is None or signal is None:
        raise HTTPException(404, "Not found")
    if not signal.raw_text:
        return _redirect(f"/company/{company_id}", "That record has no raw text.", "warn")

    # Imported lazily: the `anthropic` package is an optional install, so the app
    # must be importable and fully usable without it.
    from ..llm.parser import parse_entry

    outcome = parse_entry(signal.raw_text, company_hint=company.name)
    if not outcome.ok:
        return _redirect(f"/company/{company_id}", f"Still unavailable: {outcome.error}", "warn")

    parsed_signals = list(getattr(outcome.data, "signals", []) or [])
    if not parsed_signals:
        signal.parsed_summary = getattr(outcome.data, "reasoning", None)
        return _redirect(
            f"/company/{company_id}",
            "Parsed — still nothing scoreable in this note.",
            "warn",
        )

    result = ingest_note(
        db,
        note=signal.raw_text,
        company_name=company.name,
        source=signal.source,
        detected_date=signal.detected_date,
        outcome=outcome,
    )
    db.delete(signal)
    rescore_company(db, company)
    return _redirect(f"/company/{company_id}", result.message())


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@app.post("/rescore")
def rescore(db: DbSession):
    from ..scoring import rescore_all

    stats = rescore_all(db)
    return _redirect("/", f"Rescored {stats['companies']} companies against today's date.")


@app.get("/taxonomy", response_class=HTMLResponse)
def taxonomy_view(request: Request, db: DbSession):
    rows = list(
        db.scalars(select(SignalType).order_by(SignalType.sort_order, SignalType.id))
    )
    counts = {}
    for s in db.scalars(select(Signal)):
        if s.type_key:
            counts[s.type_key] = counts.get(s.type_key, 0) + 1
    return templates.TemplateResponse(
        request,
        "taxonomy.html", _ctx(request, rows=rows, counts=counts)
    )
