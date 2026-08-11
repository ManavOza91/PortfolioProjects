"""Regenerate the portable/ directory.

portable/ holds everything a different technology stack needs in order to
reimplement this system without reading a line of Python. It is GENERATED from
the live code and config, so it cannot drift away from what actually runs.

    ./run.sh export-portable

Hand-written prose specs (SCORING.md, BUDDY-SCORE.md, README.md) are NOT touched
by this script — they explain the formulas; this script emits the machine-readable
parameters, schemas, prompts, SQL and golden test vectors that go with them.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from signal_engine.config import PROJECT_ROOT, get_config, get_taxonomy
from signal_engine.llm.parser import (
    _today_line,
    build_entry_system_prompt,
    build_insight_system_prompt,
    entry_model,
    insight_model,
)
from signal_engine.models import Base
from signal_engine.scoring import (
    Contribution,
    assign_tier,
    compounding_multiplier,
    decay_factor,
    score_from_contributions,
)

OUT = PROJECT_ROOT / "portable"

# Fixed reference date so the golden vectors are deterministic forever.
AS_OF = dt.date(2026, 1, 1)

GENERATED_BANNER = (
    "GENERATED FILE — do not edit by hand.\n"
    "Regenerate with: ./run.sh export-portable\n"
    "Source of truth: data/taxonomy.yaml, config.yaml, signal_engine/\n"
)


def _write(name: str, content: str) -> Path:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_json(name: str, payload: dict | list, note: str) -> Path:
    if isinstance(payload, dict):
        payload = {"_generated": GENERATED_BANNER.strip(), "_note": note, **payload}
    return _write(name, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 1. Taxonomy
# ---------------------------------------------------------------------------


def export_taxonomy() -> Path:
    tax = get_taxonomy()
    return _write_json(
        "taxonomy.json",
        {
            "version": tax.version,
            "product_fit_labels": tax.product_fit_labels,
            "signal_types": [
                {
                    "key": t.key,
                    "label": t.label,
                    "product_fit": t.product_fit,
                    "base_weight": t.base_weight,
                    "sources": t.sources,
                    "decays": t.decays,
                    "description": t.description,
                    "examples": t.examples,
                }
                for t in tax.types
            ],
        },
        "The signal taxonomy. This is the single most portable artefact here — "
        "swap it and the whole system targets a different market. JSON mirror of "
        "data/taxonomy.yaml, which stays the editable original.",
    )


# ---------------------------------------------------------------------------
# 2. Scoring and Buddy Score parameters
# ---------------------------------------------------------------------------


def export_parameters() -> Path:
    cfg = get_config()
    return _write_json(
        "scoring-parameters.json",
        {
            "scoring": {
                "decay": cfg.decay,
                "compounding": cfg.compounding,
                "tiers": [{"name": t.name, "min_score": t.min_score} for t in cfg.tiers],
                "auto_review_threshold": cfg.auto_review_threshold,
                "manual_review_threshold": float(
                    cfg.scoring.get("manual_review_threshold", 0.45)
                ),
                "manual_default_confidence": cfg.manual_default_confidence,
                "confidence_scales_weight": bool(
                    cfg.scoring.get("confidence_scales_weight", False)
                ),
            },
            "buddy_score": cfg.buddy,
            "products": cfg.products,
        },
        "Every number the scoring engine and Buddy Score depend on. Pair with "
        "SCORING.md and BUDDY-SCORE.md, which give the formulas these plug into.",
    )


# ---------------------------------------------------------------------------
# 3. Parser prompts and output schemas
# ---------------------------------------------------------------------------


def export_prompts() -> list[Path]:
    cfg = get_config()
    written = []

    entry_prompt = build_entry_system_prompt()[0]["text"]
    written.append(
        _write(
            "parser-system-prompt.txt",
            f"# {GENERATED_BANNER}"
            "# The system prompt for the note parser, rendered from the live taxonomy.\n"
            "# Send as a single cacheable system block. The taxonomy section is\n"
            "# generated from taxonomy.json — regenerate this file when that changes.\n"
            f"# Model in use: {cfg.llm['parser_model']}, effort {cfg.llm.get('parser_effort')}\n"
            "#\n"
            "# NOTE: today's date is deliberately NOT in here. It goes in the user turn,\n"
            "# so the system block stays byte-identical and the prompt cache keeps working.\n"
            f"{'-' * 78}\n{entry_prompt}\n",
        )
    )

    insight_prompt = build_insight_system_prompt()[0]["text"]
    written.append(
        _write(
            "manager-insight-system-prompt.txt",
            f"# {GENERATED_BANNER}{'-' * 78}\n{insight_prompt}\n",
        )
    )

    written.append(
        _write(
            "parser-user-template.txt",
            f"# {GENERATED_BANNER}"
            "# Shape of the user turn. Everything volatile lives here, never in the\n"
            "# system block. Lines in {braces} are substituted; omit optional ones.\n"
            f"{'-' * 78}\n"
            f"{_today_line(AS_OF).replace(AS_OF.isoformat(), '{today_iso}').replace(AS_OF.strftime('%A %d %B %Y'), '{today_human}')}\n"
            "\n"
            "The user has already named the company: {company_hint}      # optional\n"
            "The user has already given this contact detail: {contact_hint}  # optional\n"
            "\n"
            "NOTE:\n"
            "{the note text}\n",
        )
    )

    written.append(
        _write_json(
            "parser-output-schema.json",
            entry_model().model_json_schema(),
            "JSON Schema for the note parser's structured output. Enforce this with "
            "your provider's structured-output feature. type_key is a closed enum "
            "built from the taxonomy, which is what stops the model inventing a "
            "signal type to fill the schema.",
        )
    )

    written.append(
        _write_json(
            "manager-insight-output-schema.json",
            insight_model().model_json_schema(),
            "JSON Schema for the manager-insight parser's structured output.",
        )
    )

    return written


# ---------------------------------------------------------------------------
# 4. Database schema as plain SQL
# ---------------------------------------------------------------------------


def _dump_sql(dialect, header: str) -> str:
    lines = [f"-- {line}" for line in GENERATED_BANNER.strip().splitlines()]
    lines.append("--")
    lines += [f"-- {line}" for line in header.strip().splitlines()]
    lines.append("")

    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        lines.append(f"{ddl};")
        lines.append("")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            idx = str(CreateIndex(index).compile(dialect=dialect)).strip()
            lines.append(f"{idx};")
        if table.indexes:
            lines.append("")
    return "\n".join(lines)


def export_sql() -> list[Path]:
    pg_header = """
PostgreSQL schema — the dialect Supabase uses.

Two constraints are load-bearing, not incidental:

  1. activity.company_id is NOT NULL and activity.contact_id is nullable with
     ON DELETE SET NULL. That combination is what makes it possible to delete
     every contact — all personal data — while scoring, the Buddy Score and all
     reporting keep working on company-level touches. Do not "tidy" this by
     making contact_id NOT NULL or by cascading the delete.

  2. tenant_id is on every table and created_by on every authored row. Unused in
     a single-user build, but present so multi-tenancy is not a migration later.

Serial/identity columns are emitted as the SQLAlchemy default. In Supabase you may
prefer bigint generated always as identity, or uuid keys — either is fine, nothing
in the logic depends on the key type.
"""
    sqlite_header = "SQLite schema — what the reference Python implementation runs on."

    return [
        _write("schema.postgres.sql", _dump_sql(postgresql.dialect(), pg_header)),
        _write("schema.sqlite.sql", _dump_sql(sqlite.dialect(), sqlite_header)),
    ]


# ---------------------------------------------------------------------------
# 5. Golden test vectors
# ---------------------------------------------------------------------------


def _contribution(type_key: str, detected: dt.date, *, weight: float, decays: bool):
    factor, expired = decay_factor(detected, decays=decays, as_of=AS_OF)
    return Contribution(
        signal_id=None,
        type_key=type_key,
        label=type_key,
        base_weight=weight,
        decay_factor=factor,
        effective_weight=weight * factor,
        detected_date=detected,
        age_days=max(0, (AS_OF - detected).days),
        decays=decays,
        expired=expired,
        source="manual",
        product_fit=None,
        summary=None,
    )


def export_test_vectors() -> Path:
    tax = get_taxonomy()

    decay_cases = []
    for days, decays, label in [
        (0, True, "same day — full weight"),
        (91, True, "one quarter — about 10% lost"),
        (182, True, "six months — still worth most of its weight, NOT dead"),
        (365, True, "one year"),
        (730, True, "two years — a real deal re-engaged at this age"),
        (1825, True, "five years — floored, never zero"),
        (1825, False, "five years, structural — no decay at all"),
    ]:
        detected = AS_OF - dt.timedelta(days=days)
        factor, expired = decay_factor(detected, decays=decays, as_of=AS_OF)
        decay_cases.append(
            {
                "age_days": days,
                "decays": decays,
                "expected_factor": round(factor, 6),
                "expired": expired,
                "note": label,
            }
        )

    expiry_detected = AS_OF - dt.timedelta(days=10)
    factor, expired = decay_factor(
        expiry_detected,
        decays=True,
        expiry_date=AS_OF - dt.timedelta(days=1),
        as_of=AS_OF,
    )
    decay_cases.append(
        {
            "age_days": 10,
            "decays": True,
            "expiry_date_days_ago": 1,
            "expected_factor": round(factor, 6),
            "expired": expired,
            "note": "past its expiry — drops straight to the floor, still not zero",
        }
    )

    score_cases = []
    scenarios = [
        (
            "single heaviest signal, fresh",
            [("manual_diy_production_at_capacity", 0)],
            "The highest-converting signal in the taxonomy must on its own reach a "
            "tier that counts as quality outreach.",
        ),
        (
            "two independent signals 30 days apart",
            [("regulatory_submission", 50), ("funding_scale_up", 20)],
            "Compounding fires: distinct types inside the 90-day window.",
        ),
        (
            "two signals of the SAME type",
            [("job_posting_relevant_science", 10), ("job_posting_relevant_science", 30)],
            "No compounding — two job postings are one story, not two signals.",
        ),
        (
            "two independent signals 400 days apart",
            [("regulatory_submission", 400), ("funding_scale_up", 5)],
            "No compounding — outside the window.",
        ),
        (
            "funding alone",
            [("funding_scale_up", 0)],
            "Funding is a qualifier, not a trigger. Must not reach the top tier.",
        ),
        (
            "structural signal only, very old",
            [("multi_product_pipeline", 1500)],
            "Structural signals never decay.",
        ),
    ]

    for name, signals, note in scenarios:
        contributions = []
        for key, days_ago in signals:
            spec = tax.by_key(key)
            contributions.append(
                _contribution(
                    key,
                    AS_OF - dt.timedelta(days=days_ago),
                    weight=spec.base_weight,
                    decays=spec.decays,
                )
            )
        breakdown = score_from_contributions(contributions)
        mult, _reason = compounding_multiplier(contributions)
        score_cases.append(
            {
                "name": name,
                "note": note,
                "signals": [
                    {
                        "type_key": key,
                        "detected_days_before_as_of": days_ago,
                        "base_weight": tax.by_key(key).base_weight,
                    }
                    for key, days_ago in signals
                ],
                "expected": {
                    "raw_sum": round(breakdown.raw_sum, 6),
                    "compounding_multiplier": round(mult, 6),
                    "score": round(breakdown.score, 6),
                    "tier": breakdown.tier,
                },
            }
        )

    tier_cases = [
        {"score": s, "expected_tier": assign_tier(s)}
        for s in (0, 5, 11.99, 12, 24.99, 25, 30, 54.99, 55, 100, 250)
    ]

    return _write_json(
        "test-vectors.json",
        {
            "as_of": AS_OF.isoformat(),
            "how_to_use": (
                "Reimplement the scoring rules in your stack, then run these vectors "
                "through them. If every expected value matches, your implementation "
                "agrees with the reference one. These were computed BY the reference "
                "implementation, so they are authoritative rather than aspirational."
            ),
            "decay": decay_cases,
            "scoring": score_cases,
            "tiers": tier_cases,
        },
        "Golden vectors for verifying a reimplementation.",
    )


# ---------------------------------------------------------------------------


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[Path] = [export_taxonomy(), export_parameters()]
    written += export_prompts()
    written += export_sql()
    written.append(export_test_vectors())

    print(f"Wrote {len(written)} generated files to portable/\n")
    for path in sorted(written):
        size = path.stat().st_size
        print(f"  {path.relative_to(PROJECT_ROOT)}  ({size:,} bytes)")

    hand_written = ["README.md", "SCORING.md", "BUDDY-SCORE.md"]
    missing = [n for n in hand_written if not (OUT / n).exists()]
    print("\nHand-written specs (not regenerated):")
    for name in hand_written:
        mark = "MISSING" if name in missing else "ok"
        print(f"  portable/{name}  [{mark}]")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
