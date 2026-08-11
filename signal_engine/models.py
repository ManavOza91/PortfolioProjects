"""Database schema.

Design constraints this schema enforces rather than merely documents:

1. THE TAXONOMY IS DATA. `signal_types` holds the weights and product fits.
   No application code names a signal type.

2. PERSONAL DATA IS A DETACHABLE LIMB. `activity.contact_id` is nullable with
   ON DELETE SET NULL, while `activity.company_id` is NOT NULL. Deleting every
   contact therefore erases all personal data while leaving scoring, Buddy Score
   and reporting fully intact. That is the contacts-free commercial edition, and
   it is a property of the schema rather than a promise in a README.

3. SCORE IS DERIVED. `companies.current_score` is a cache. Recomputing it from
   `signals` is idempotent. `score_snapshots` preserves the trend.

4. HISTORY IS NOT REWRITTEN. `activity` records the company's tier and the
   governing signal AT THE TIME OF CONTACT, so later rescoring cannot retroactively
   flatter or damn past outreach.

5. MULTI-TENANT READY. Every table carries tenant_id and every authored row
   carries created_by, unused in v1 but present so the SaaS version is not a migration.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Taxonomy — seeded from data/taxonomy.yaml, editable without touching code
# ---------------------------------------------------------------------------


class SignalType(Base):
    __tablename__ = "signal_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    product_fit: Mapped[str] = mapped_column(String(10), nullable=False, default="both")
    base_weight: Mapped[float] = mapped_column(Float, nullable=False)
    sources: Mapped[str] = mapped_column(String(20), nullable=False, default="both")
    decays: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
    examples: Mapped[str] = mapped_column(Text, default="")  # newline-separated
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_signal_types_tenant_key"),
        CheckConstraint("product_fit IN ('A','B','both')", name="ck_signal_types_product_fit"),
        CheckConstraint("sources IN ('manual','auto','both')", name="ck_signal_types_sources"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SignalType {self.key} w={self.base_weight}>"


# ---------------------------------------------------------------------------
# Core: companies and signals. The system runs on these two tables alone.
# ---------------------------------------------------------------------------

COMPANY_STATUSES = ("watchlist", "active", "engaged", "won", "lost", "dormant")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String(200))
    country: Mapped[Optional[str]] = mapped_column(String(100))
    segment: Mapped[Optional[str]] = mapped_column(String(100))
    size_band: Mapped[Optional[str]] = mapped_column(String(50))
    product_fit: Mapped[Optional[str]] = mapped_column(String(10))

    # Derived cache — recomputed from signals, never authoritative.
    current_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tier: Mapped[Optional[str]] = mapped_column(String(10))
    score_computed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)

    first_signal_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    last_signal_date: Mapped[Optional[dt.date]] = mapped_column(Date)

    # 'watchlist' means imported/monitored but never entered the pipeline.
    # A company earns 'active' only when a signal fires. No signal, no entry.
    status: Mapped[str] = mapped_column(String(30), default="watchlist", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_by: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    signals: Mapped[list["Signal"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_companies_tenant_name", "tenant_id", "name"),
        Index("ix_companies_tenant_score", "tenant_id", "current_score"),
        CheckConstraint(
            "status IN ('watchlist','active','engaged','won','lost','dormant')",
            name="ck_companies_status",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Company {self.name!r} score={self.current_score:.1f} tier={self.tier}>"


SIGNAL_SOURCES = ("auto", "manual", "manager_insight", "inbound", "event")
SIGNAL_STATUSES = ("scored", "review", "unscored", "rejected")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # References signal_types.key. Nullable so an unscoreable note can still be
    # stored: raw human observation is never discarded just because it did not parse.
    type_key: Mapped[Optional[str]] = mapped_column(String(80), index=True)

    # Snapshot of the type's base_weight at creation time. Reweighting the taxonomy
    # changes future signals; it does not silently rewrite the past.
    weight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    product_fit: Mapped[Optional[str]] = mapped_column(String(10))

    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)

    # Raw human text is stored alongside every parsed output, always. You will want
    # to audit which observations actually predicted outcomes.
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    parsed_summary: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    detected_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[Optional[dt.date]] = mapped_column(Date)

    # 'scored'   contributes to the company score
    # 'review'   detected automatically but below the confidence threshold
    # 'unscored' stored, nothing scoreable found — no fabricated number
    # 'rejected' reviewed and dismissed
    status: Mapped[str] = mapped_column(String(20), default="scored", nullable=False, index=True)

    outcome_tag: Mapped[Optional[str]] = mapped_column(String(60))
    detail_url: Mapped[Optional[str]] = mapped_column(String(500))

    created_by: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    company: Mapped["Company"] = relationship(back_populates="signals")

    __table_args__ = (
        Index("ix_signals_company_detected", "company_id", "detected_date"),
        CheckConstraint(
            "source IN ('auto','manual','manager_insight','inbound','event')",
            name="ck_signals_source",
        ),
        CheckConstraint(
            "status IN ('scored','review','unscored','rejected')", name="ck_signals_status"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_signals_confidence"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Signal {self.type_key} w={self.weight} {self.detected_date}>"


# ---------------------------------------------------------------------------
# Personal data layer. Entirely optional. Deletable wholesale.
# ---------------------------------------------------------------------------


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    full_name: Mapped[Optional[str]] = mapped_column(String(200))
    job_title: Mapped[Optional[str]] = mapped_column(String(250))
    email: Mapped[Optional[str]] = mapped_column(String(250))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(500))
    country: Mapped[Optional[str]] = mapped_column(String(100))
    persona: Mapped[Optional[str]] = mapped_column(String(100))
    source: Mapped[Optional[str]] = mapped_column(String(60))
    date_added: Mapped[dt.date] = mapped_column(Date, default=lambda: _now().date())
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_by: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    company: Mapped["Company"] = relationship(back_populates="contacts")

    __table_args__ = (Index("ix_contacts_tenant_company", "tenant_id", "company_id"),)


ACTIVITY_TYPES = ("email", "call", "linkedin", "meeting", "reply", "note")
ACTIVITY_DIRECTIONS = ("out", "in")


class Activity(Base):
    """One outbound touch or inbound response.

    company_id is NOT NULL; contact_id is nullable and set to NULL on contact
    deletion. Erasing every contact therefore leaves the activity history — and so
    the Buddy Score and the conversion reporting — completely intact.
    """

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    type: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), default="out", nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(60))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # What you knew when you acted. Rescoring must not rewrite the record of
    # whether an outreach was signal-driven at the time it was sent.
    signal_id_at_time_of_contact: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    company_tier_at_time_of_contact: Mapped[Optional[str]] = mapped_column(String(10))
    company_score_at_time_of_contact: Mapped[Optional[float]] = mapped_column(Float)

    created_by: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_activity_tenant_date", "tenant_id", "date"),
        CheckConstraint(
            "type IN ('email','call','linkedin','meeting','reply','note')",
            name="ck_activity_type",
        ),
        CheckConstraint("direction IN ('out','in')", name="ck_activity_direction"),
    )


# ---------------------------------------------------------------------------
# Manager insight layer (schema now; the scoring loop is phase 8)
# ---------------------------------------------------------------------------


class ManagerInsight(Base):
    __tablename__ = "manager_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_segment: Mapped[Optional[str]] = mapped_column(String(200))
    parsed_geography: Mapped[Optional[str]] = mapped_column(String(200))
    parsed_signal_type: Mapped[Optional[str]] = mapped_column(String(80))
    parsed_summary: Mapped[Optional[str]] = mapped_column(Text)
    weight_adjustment: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    author: Mapped[Optional[str]] = mapped_column(String(120))
    created_date: Mapped[dt.date] = mapped_column(Date, default=lambda: _now().date())

    # Rules expire by default so stale intuitions age out instead of silently
    # distorting scores.
    expiry_date: Mapped[Optional[dt.date]] = mapped_column(Date)

    # Set by the outcome-scoring loop: rises if companies surfaced under this rule
    # convert above baseline, fades toward zero if they do not.
    performance_score: Mapped[Optional[float]] = mapped_column(Float)
    surfaced_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    converted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ---------------------------------------------------------------------------
# Derived history
# ---------------------------------------------------------------------------


class ScoreSnapshot(Base):
    """Point-in-time company score, so 'why was this Tier A in March?' is answerable."""

    __tablename__ = "score_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    score: Mapped[float] = mapped_column(Float, nullable=False)
    tier: Mapped[Optional[str]] = mapped_column(String(10))
    signal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    compounding_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    breakdown: Mapped[Optional[str]] = mapped_column(Text)  # JSON

    snapshot_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "snapshot_date", name="uq_score_snapshot_company_date"
        ),
    )


class BuddySnapshot(Base):
    """Buddy Score for one period. Weekly is the base grain; rollups aggregate it."""

    __tablename__ = "buddy_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)

    period_type: Mapped[str] = mapped_column(String(20), nullable=False)  # week/month/quarter/year
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False)
    progression: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    signal_quality: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    consistency: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text)  # JSON of raw counts

    owner: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner", "period_type", "period_start",
            name="uq_buddy_snapshot_period",
        ),
    )


# Convenience: tables that hold personal data, for the purge operation.
PERSONAL_DATA_TABLES = (Contact.__tablename__,)
