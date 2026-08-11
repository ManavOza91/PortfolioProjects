"""Load data/taxonomy.yaml into the database.

Idempotent: matches on `key`, updates in place, adds what is new. It never deletes
a type that already has signals attached — retire one by setting `active: false`
in the YAML instead, so historical signals keep their meaning.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_config, get_taxonomy
from .models import SignalType


@dataclass
class SeedResult:
    added: list[str]
    updated: list[str]
    deactivated: list[str]
    kept_for_history: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.deactivated)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.updated:
            parts.append(f"{len(self.updated)} updated")
        if self.deactivated:
            parts.append(f"{len(self.deactivated)} deactivated")
        if self.kept_for_history:
            parts.append(f"{len(self.kept_for_history)} retained for history")
        return ", ".join(parts) if parts else "no changes"


def seed_taxonomy(session: Session) -> SeedResult:
    cfg = get_config()
    taxonomy = get_taxonomy()
    tenant_id = cfg.tenant_id

    existing = {
        row.key: row
        for row in session.scalars(
            select(SignalType).where(SignalType.tenant_id == tenant_id)
        )
    }

    result = SeedResult([], [], [], [])
    yaml_keys: set[str] = set()

    for order, spec in enumerate(taxonomy.types):
        yaml_keys.add(spec.key)
        examples = "\n".join(spec.examples)
        row = existing.get(spec.key)

        if row is None:
            session.add(
                SignalType(
                    tenant_id=tenant_id,
                    key=spec.key,
                    label=spec.label,
                    product_fit=spec.product_fit,
                    base_weight=spec.base_weight,
                    sources=spec.sources,
                    decays=spec.decays,
                    description=spec.description,
                    examples=examples,
                    active=True,
                    sort_order=order,
                )
            )
            result.added.append(spec.key)
            continue

        fields = {
            "label": spec.label,
            "product_fit": spec.product_fit,
            "base_weight": spec.base_weight,
            "sources": spec.sources,
            "decays": spec.decays,
            "description": spec.description,
            "examples": examples,
            "active": True,
            "sort_order": order,
        }
        if any(getattr(row, name) != value for name, value in fields.items()):
            for name, value in fields.items():
                setattr(row, name, value)
            result.updated.append(spec.key)

    # A type removed from the YAML is deactivated, not deleted — existing signals
    # must keep resolving to something meaningful.
    from .models import Signal  # local import to keep module import order simple

    for key, row in existing.items():
        if key in yaml_keys or not row.active:
            continue
        in_use = session.scalar(
            select(func.count(Signal.id)).where(
                Signal.tenant_id == tenant_id, Signal.type_key == key
            )
        )
        row.active = False
        if in_use:
            result.kept_for_history.append(key)
        result.deactivated.append(key)

    session.flush()
    return result
