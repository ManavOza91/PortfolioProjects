"""Drift guard for portable/.

portable/ is the specification a different stack gets reimplemented from. A spec
that has quietly drifted away from the running code is worse than no spec — you
would rebuild against rules that are no longer true.

These tests fail if portable/ is stale, and fail if the golden vectors stop
matching the live implementation.

If one fails after an intentional change: ./run.sh export-portable
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import pytest

from signal_engine.config import PROJECT_ROOT, get_config, get_taxonomy
from signal_engine.scoring import assign_tier, decay_factor

PORTABLE = PROJECT_ROOT / "portable"


def _load(name: str) -> dict:
    path = PORTABLE / name
    assert path.exists(), f"portable/{name} is missing — run ./run.sh export-portable"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "SCORING.md",
        "BUDDY-SCORE.md",
        "taxonomy.json",
        "scoring-parameters.json",
        "parser-system-prompt.txt",
        "parser-user-template.txt",
        "parser-output-schema.json",
        "manager-insight-system-prompt.txt",
        "manager-insight-output-schema.json",
        "schema.postgres.sql",
        "schema.sqlite.sql",
        "test-vectors.json",
    ],
)
def test_every_portable_artefact_exists(name):
    assert (PORTABLE / name).exists(), f"portable/{name} missing"


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_portable_is_not_stale():
    """Regenerating must be a no-op. If it isn't, the spec has drifted."""
    before = {
        p.name: p.read_bytes()
        for p in PORTABLE.iterdir()
        if p.is_file() and p.suffix != ".md"
    }

    result = subprocess.run(
        [sys.executable, "scripts/export_portable.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    after = {
        p.name: p.read_bytes()
        for p in PORTABLE.iterdir()
        if p.is_file() and p.suffix != ".md"
    }

    changed = sorted(n for n in after if before.get(n) != after[n])
    assert not changed, (
        f"portable/ was out of date: {', '.join(changed)} changed on regeneration. "
        "Commit the regenerated files."
    )


# ---------------------------------------------------------------------------
# Agreement with the live implementation
# ---------------------------------------------------------------------------


def test_exported_taxonomy_matches_the_live_one():
    exported = _load("taxonomy.json")
    live = {t.key: t for t in get_taxonomy().types}

    assert len(exported["signal_types"]) == len(live)
    for row in exported["signal_types"]:
        spec = live[row["key"]]
        assert row["base_weight"] == spec.base_weight
        assert row["product_fit"] == spec.product_fit
        assert row["decays"] == spec.decays


def test_exported_parameters_match_config():
    exported = _load("scoring-parameters.json")
    cfg = get_config()

    assert exported["scoring"]["decay"]["floor"] == cfg.decay["floor"]
    assert exported["scoring"]["compounding"]["multiplier"] == cfg.compounding["multiplier"]
    assert exported["buddy_score"]["weekly_touch_target"] == cfg.buddy["weekly_touch_target"]

    exported_tiers = [(t["name"], t["min_score"]) for t in exported["scoring"]["tiers"]]
    live_tiers = [(t.name, t.min_score) for t in cfg.tiers]
    assert exported_tiers == live_tiers


def test_golden_decay_vectors_still_hold():
    vectors = _load("test-vectors.json")
    as_of = dt.date.fromisoformat(vectors["as_of"])

    for case in vectors["decay"]:
        detected = as_of - dt.timedelta(days=case["age_days"])
        expiry = None
        if "expiry_date_days_ago" in case:
            expiry = as_of - dt.timedelta(days=case["expiry_date_days_ago"])

        factor, expired = decay_factor(
            detected, decays=case["decays"], expiry_date=expiry, as_of=as_of
        )
        assert factor == pytest.approx(case["expected_factor"], abs=1e-6), case["note"]
        assert expired == case["expired"], case["note"]


def test_golden_scoring_vectors_still_hold():
    from signal_engine.scoring import Contribution, score_from_contributions

    vectors = _load("test-vectors.json")
    as_of = dt.date.fromisoformat(vectors["as_of"])
    taxonomy = get_taxonomy()

    for case in vectors["scoring"]:
        contributions = []
        for sig in case["signals"]:
            spec = taxonomy.by_key(sig["type_key"])
            detected = as_of - dt.timedelta(days=sig["detected_days_before_as_of"])
            factor, expired = decay_factor(detected, decays=spec.decays, as_of=as_of)
            contributions.append(
                Contribution(
                    signal_id=None,
                    type_key=sig["type_key"],
                    label=sig["type_key"],
                    base_weight=spec.base_weight,
                    decay_factor=factor,
                    effective_weight=spec.base_weight * factor,
                    detected_date=detected,
                    age_days=max(0, (as_of - detected).days),
                    decays=spec.decays,
                    expired=expired,
                    source="manual",
                    product_fit=None,
                    summary=None,
                )
            )

        breakdown = score_from_contributions(contributions)
        expected = case["expected"]
        assert breakdown.raw_sum == pytest.approx(expected["raw_sum"], abs=1e-6), case["name"]
        assert breakdown.multiplier == pytest.approx(
            expected["compounding_multiplier"], abs=1e-6
        ), case["name"]
        assert breakdown.score == pytest.approx(expected["score"], abs=1e-6), case["name"]
        assert breakdown.tier == expected["tier"], case["name"]


def test_golden_tier_vectors_still_hold():
    for case in _load("test-vectors.json")["tiers"]:
        assert assign_tier(case["score"]) == case["expected_tier"], case["score"]


# ---------------------------------------------------------------------------
# Properties a reimplementation must not lose
# ---------------------------------------------------------------------------


def test_exported_parser_schema_closes_the_type_enum():
    """The enum is what stops a model inventing a signal type to fill the schema."""
    schema = _load("parser-output-schema.json")
    enum = schema["$defs"]["ParsedSignal"]["properties"]["type_key"]["enum"]
    assert set(enum) == {t.key for t in get_taxonomy().types}


def test_exported_sql_preserves_the_personal_data_separation():
    """The one schema property that makes a contacts-free edition possible."""
    sql = (PORTABLE / "schema.postgres.sql").read_text(encoding="utf-8")
    activity = sql.split("CREATE TABLE activity")[1].split(");")[0]

    assert "company_id INTEGER NOT NULL" in activity, "activity.company_id must be NOT NULL"
    assert "contact_id INTEGER," in activity, "activity.contact_id must be nullable"
    assert "FOREIGN KEY(contact_id) REFERENCES contacts (id) ON DELETE SET NULL" in activity


def test_exported_sql_covers_every_table():
    sql = (PORTABLE / "schema.postgres.sql").read_text(encoding="utf-8")
    from signal_engine.models import Base

    for table in Base.metadata.sorted_tables:
        assert f"CREATE TABLE {table.name}" in sql, f"{table.name} missing from exported SQL"


def test_the_system_prompt_has_no_date_in_it():
    """A date in the system block would silently break prompt caching every call."""
    text = (PORTABLE / "parser-system-prompt.txt").read_text(encoding="utf-8")
    body = text.split("-" * 78, 1)[1]
    today = dt.datetime.now(dt.timezone.utc).date()
    assert today.isoformat() not in body
    assert str(today.year) not in body.replace("2026-01-12", "")


def test_scoring_spec_documents_the_tier_calibration_rule():
    """The rule that stops the Buddy Score punishing the best available signal."""
    spec = (PORTABLE / "SCORING.md").read_text(encoding="utf-8")
    assert "at or below the heaviest" in spec


def test_buddy_spec_documents_the_spray_invariant():
    spec = (PORTABLE / "BUDDY-SCORE.md").read_text(encoding="utf-8")
    assert "40 touches to unsignalled companies must score worse" in spec
