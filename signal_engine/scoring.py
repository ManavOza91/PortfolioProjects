"""The scoring engine.

Three rules, all parameterised in config.yaml:

SLOW DECAY. 10% weight loss per quarter, floored at 40% of the original, never zero.
Capital equipment cycles run six months to two years. A company lost to a competitor
in 2024 re-engaged in 2026 wanting a second unit; another raised in 2024 and is buying
now. Standard sales-tool decay logic — a six-month-old signal is dead — is simply wrong
for this market.

STRUCTURAL SIGNALS DO NOT DECAY. A disclosed multi-product pipeline does not stop being
multi-product. `decays: false` in the taxonomy.

COMPOUNDING. Two independent signals within 90 days multiply the combined score by 1.3.
Independence means different signal types — two job postings are one story, a regulatory
submission plus a funding round are two.

There are no category exclusions anywhere in this module. Entry is earned by evidence,
never denied by category.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config, get_config
from .models import Company, ScoreSnapshot, Signal, SignalType


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


@dataclass
class Contribution:
    """One signal's contribution to a company's score, fully explainable."""

    signal_id: int | None
    type_key: str | None
    label: str
    base_weight: float
    decay_factor: float
    effective_weight: float
    detected_date: dt.date
    age_days: int
    decays: bool
    expired: bool
    source: str
    product_fit: str | None
    summary: str | None

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "type_key": self.type_key,
            "label": self.label,
            "base_weight": round(self.base_weight, 2),
            "decay_factor": round(self.decay_factor, 4),
            "effective_weight": round(self.effective_weight, 2),
            "detected_date": self.detected_date.isoformat(),
            "age_days": self.age_days,
            "decays": self.decays,
            "expired": self.expired,
            "source": self.source,
        }


@dataclass
class ScoreBreakdown:
    raw_sum: float = 0.0
    multiplier: float = 1.0
    score: float = 0.0
    tier: str | None = None
    contributions: list[Contribution] = field(default_factory=list)
    compounding_applied: bool = False
    compounding_reason: str | None = None
    product_fit: str | None = None
    first_signal_date: dt.date | None = None
    last_signal_date: dt.date | None = None

    @property
    def signal_count(self) -> int:
        return len(self.contributions)

    @property
    def top_reason(self) -> str:
        """The single strongest live signal — what goes on the queue row."""
        if not self.contributions:
            return "No active signal"
        best = max(self.contributions, key=lambda c: c.effective_weight)
        return best.summary or best.label

    def to_json(self) -> str:
        return json.dumps(
            {
                "raw_sum": round(self.raw_sum, 2),
                "multiplier": self.multiplier,
                "score": round(self.score, 2),
                "tier": self.tier,
                "compounding_applied": self.compounding_applied,
                "compounding_reason": self.compounding_reason,
                "contributions": [c.to_dict() for c in self.contributions],
            }
        )


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def decay_factor(
    detected_date: dt.date,
    *,
    decays: bool,
    expiry_date: dt.date | None = None,
    as_of: dt.date | None = None,
    cfg: Config | None = None,
) -> tuple[float, bool]:
    """Return (factor, expired).

    A structural signal (`decays=False`) always returns 1.0 — it never expires
    and never decays, regardless of any expiry_date set on it.

    A decaying signal loses `rate_per_quarter` of its weight each quarter and
    stops at `floor`. It never reaches zero: a stale signal is weaker evidence,
    not absent evidence.

    If an expiry_date is set and has passed, the signal drops straight to the
    floor rather than gliding there. Still not zero.
    """
    cfg = cfg or get_config()
    as_of = as_of or _today()
    decay_cfg = cfg.decay

    floor = float(decay_cfg.get("floor", 0.40))
    rate = float(decay_cfg.get("rate_per_quarter", 0.90))
    days_per_quarter = float(decay_cfg.get("days_per_quarter", 91.31))

    expired = expiry_date is not None and as_of > expiry_date

    if not decays:
        return 1.0, False

    age_days = (as_of - detected_date).days
    if age_days <= 0:
        # Future-dated or same-day: full weight, no negative-exponent surprises.
        factor = 1.0
    else:
        quarters = age_days / days_per_quarter
        factor = rate**quarters

    factor = max(factor, floor)

    if expired and bool(decay_cfg.get("expiry_drops_to_floor", True)):
        factor = floor

    return factor, expired


# ---------------------------------------------------------------------------
# Compounding
# ---------------------------------------------------------------------------


def compounding_multiplier(
    contributions: list[Contribution], cfg: Config | None = None
) -> tuple[float, str | None]:
    """Two independent signals close together mean more than their sum.

    Independence = distinct signal type. Two job postings tell one story; a
    regulatory submission plus a funding round tell two.
    """
    cfg = cfg or get_config()
    comp = cfg.compounding
    window = int(comp.get("window_days", 90))
    multiplier = float(comp.get("multiplier", 1.30))
    require_distinct = bool(comp.get("require_distinct_types", True))
    stack = bool(comp.get("stack", False))

    if len(contributions) < 2 or multiplier <= 1.0:
        return 1.0, None

    # Densest window: for each signal, how many distinct types fall within
    # `window` days of it.
    best_count = 1
    best_pair: tuple[Contribution, Contribution] | None = None

    for anchor in contributions:
        in_window = [
            c
            for c in contributions
            if abs((c.detected_date - anchor.detected_date).days) <= window
        ]
        if require_distinct:
            distinct = {c.type_key for c in in_window if c.type_key}
            count = len(distinct)
        else:
            count = len(in_window)
        if count > best_count:
            best_count = count
            pair = sorted(in_window, key=lambda c: c.effective_weight, reverse=True)[:2]
            if len(pair) == 2:
                best_pair = (pair[0], pair[1])

    if best_count < 2:
        return 1.0, None

    factor = multiplier ** (best_count - 1) if stack else multiplier

    reason = None
    if best_pair:
        a, b = best_pair
        gap = abs((a.detected_date - b.detected_date).days)
        reason = (
            f"{best_count} independent signals within {window} days "
            f"({a.label} and {b.label}, {gap} days apart) — x{factor:.2f}"
        )
    return factor, reason


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def assign_tier(score: float, cfg: Config | None = None) -> str | None:
    cfg = cfg or get_config()
    for rule in cfg.tiers:  # already sorted highest-threshold-first
        if score >= rule.min_score:
            return rule.name
    return None


# ---------------------------------------------------------------------------
# Product fit
# ---------------------------------------------------------------------------


def derive_product_fit(contributions: list[Contribution]) -> str | None:
    """Which product the evidence points at, weighted by live signal strength."""
    if not contributions:
        return None
    totals = {"A": 0.0, "B": 0.0}
    for c in contributions:
        fit = (c.product_fit or "both").lower()
        if fit == "both":
            totals["A"] += c.effective_weight
            totals["B"] += c.effective_weight
        elif fit in totals:
            totals[fit] += c.effective_weight

    a, b = totals["A"], totals["B"]
    if a == 0 and b == 0:
        return None
    stronger, weaker = (a, b) if a >= b else (b, a)
    if stronger > 0 and weaker / stronger >= 0.75:
        return "both"
    return "A" if a > b else "B"


# ---------------------------------------------------------------------------
# Company scoring
# ---------------------------------------------------------------------------


def _type_index(session: Session, tenant_id: int) -> dict[str, SignalType]:
    return {
        t.key: t
        for t in session.scalars(
            select(SignalType).where(SignalType.tenant_id == tenant_id)
        )
    }


def build_contributions(
    signals: list[Signal],
    types: dict[str, SignalType],
    *,
    as_of: dt.date | None = None,
    cfg: Config | None = None,
) -> list[Contribution]:
    """Turn scored signals into explainable weight contributions."""
    cfg = cfg or get_config()
    as_of = as_of or _today()
    scale_by_confidence = bool(cfg.scoring.get("confidence_scales_weight", False))

    out: list[Contribution] = []
    for s in signals:
        if s.status != "scored":
            # 'review' and 'unscored' are stored but contribute nothing. A note
            # with nothing scoreable in it never receives a fabricated number.
            continue

        spec = types.get(s.type_key or "")
        decays = spec.decays if spec else True
        label = spec.label if spec else (s.type_key or "Unclassified observation")
        base = s.weight if s.weight else (spec.base_weight if spec else 0.0)

        factor, expired = decay_factor(
            s.detected_date,
            decays=decays,
            expiry_date=s.expiry_date,
            as_of=as_of,
            cfg=cfg,
        )
        effective = base * factor
        if scale_by_confidence:
            effective *= s.confidence

        out.append(
            Contribution(
                signal_id=s.id,
                type_key=s.type_key,
                label=label,
                base_weight=base,
                decay_factor=factor,
                effective_weight=effective,
                detected_date=s.detected_date,
                age_days=max(0, (as_of - s.detected_date).days),
                decays=decays,
                expired=expired,
                source=s.source,
                product_fit=s.product_fit or (spec.product_fit if spec else None),
                summary=s.parsed_summary,
            )
        )
    return out


def score_from_contributions(
    contributions: list[Contribution], cfg: Config | None = None
) -> ScoreBreakdown:
    cfg = cfg or get_config()
    breakdown = ScoreBreakdown(contributions=contributions)
    if not contributions:
        breakdown.tier = assign_tier(0.0, cfg)
        return breakdown

    breakdown.raw_sum = sum(c.effective_weight for c in contributions)
    multiplier, reason = compounding_multiplier(contributions, cfg)
    breakdown.multiplier = multiplier
    breakdown.compounding_applied = multiplier > 1.0
    breakdown.compounding_reason = reason
    breakdown.score = breakdown.raw_sum * multiplier
    breakdown.tier = assign_tier(breakdown.score, cfg)
    breakdown.product_fit = derive_product_fit(contributions)
    dates = [c.detected_date for c in contributions]
    breakdown.first_signal_date = min(dates)
    breakdown.last_signal_date = max(dates)
    return breakdown


def score_company(
    session: Session,
    company: Company,
    *,
    as_of: dt.date | None = None,
    cfg: Config | None = None,
    types: dict[str, SignalType] | None = None,
) -> ScoreBreakdown:
    """Compute — but do not persist — a company's score."""
    cfg = cfg or get_config()
    types = types if types is not None else _type_index(session, company.tenant_id)
    signals = list(
        session.scalars(select(Signal).where(Signal.company_id == company.id))
    )
    contributions = build_contributions(signals, types, as_of=as_of, cfg=cfg)
    return score_from_contributions(contributions, cfg)


def apply_score(
    session: Session,
    company: Company,
    breakdown: ScoreBreakdown,
    *,
    as_of: dt.date | None = None,
    snapshot: bool = True,
) -> None:
    """Persist a computed score onto the company, plus a dated snapshot.

    Idempotent — running it twice on unchanged signals produces the same result.
    """
    as_of = as_of or _today()

    company.current_score = round(breakdown.score, 2)
    company.tier = breakdown.tier
    company.score_computed_at = dt.datetime.now(dt.timezone.utc)
    company.first_signal_date = breakdown.first_signal_date
    company.last_signal_date = breakdown.last_signal_date
    if breakdown.product_fit:
        company.product_fit = breakdown.product_fit

    # A company earns its way out of the watchlist by having a signal fire.
    # It is never promoted for being on a list.
    if breakdown.signal_count > 0 and company.status == "watchlist":
        company.status = "active"

    if not snapshot:
        return

    existing = session.scalar(
        select(ScoreSnapshot).where(
            ScoreSnapshot.company_id == company.id,
            ScoreSnapshot.snapshot_date == as_of,
        )
    )
    if existing is None:
        existing = ScoreSnapshot(
            tenant_id=company.tenant_id,
            company_id=company.id,
            snapshot_date=as_of,
        )
        session.add(existing)

    existing.score = round(breakdown.score, 2)
    existing.tier = breakdown.tier
    existing.signal_count = breakdown.signal_count
    existing.compounding_applied = breakdown.compounding_applied
    existing.breakdown = breakdown.to_json()


def rescore_company(
    session: Session, company: Company, *, as_of: dt.date | None = None
) -> ScoreBreakdown:
    breakdown = score_company(session, company, as_of=as_of)
    apply_score(session, company, breakdown, as_of=as_of)
    return breakdown


def rescore_all(
    session: Session, *, tenant_id: int | None = None, as_of: dt.date | None = None
) -> dict[str, int]:
    """Recompute every company. Safe to run on a schedule — decay makes yesterday's
    scores stale, and this is how the queue stays honest."""
    cfg = get_config()
    tenant_id = tenant_id if tenant_id is not None else cfg.tenant_id
    as_of = as_of or _today()
    types = _type_index(session, tenant_id)

    companies = list(
        session.scalars(select(Company).where(Company.tenant_id == tenant_id))
    )
    tiers: dict[str, int] = {}
    for company in companies:
        breakdown = score_company(session, company, as_of=as_of, cfg=cfg, types=types)
        apply_score(session, company, breakdown, as_of=as_of)
        key = breakdown.tier or "-"
        tiers[key] = tiers.get(key, 0) + 1

    session.flush()
    return {"companies": len(companies), **{f"tier_{k}": v for k, v in sorted(tiers.items())}}
