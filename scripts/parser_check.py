"""Acceptance test for the natural-language signal parser.

Runs a set of notes through the real parser and prints the raw structured output
next to what was expected, so you can see at a glance whether the parser is doing
what it claims. Nothing is written to the database.

    ./run.sh parser-check                 # the built-in cases
    ./run.sh parser-check "your note"     # one note of your own
    ./run.sh parser-check --file notes.txt  # one note per line

Needs ANTHROPIC_API_KEY in .env or the environment.

THE CASE THAT MATTERS MOST is the vague one. A parser that invents a signal type
to fill the schema is worse than useless — it manufactures pipeline. If the
"nothing scoreable" case comes back with a signal, this script exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

from signal_engine.config import get_config, get_taxonomy
from signal_engine.llm.client import api_key_present
from signal_engine.llm.parser import parse_entry

def _colour_supported() -> bool:
    """Old Windows consoles render ANSI codes as literal garbage."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Windows Terminal and PowerShell 7 set this; legacy conhost does not.
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM"))
    return True


if _colour_supported():
    BOLD, DIM = "\033[1m", "\033[2m"
    GREEN, RED, YELLOW, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
else:
    BOLD = DIM = GREEN = RED = YELLOW = OFF = ""


@dataclass
class Case:
    note: str
    expect_types: set[str]
    expect_scoreable: bool
    why: str
    also_plausible: set[str] | None = None


CASES = [
    Case(
        note="Met them at LyoTalk. Doing manual bead production, hitting capacity, "
        "evaluating a machine next year.",
        expect_types={"manual_diy_production_at_capacity"},
        also_plausible={"conference_presentation"},
        expect_scoreable=True,
        why="Manual production at a capacity ceiling is the highest-converting signal "
        "in the taxonomy and appears in no public source. Should also capture the "
        "'next year' timeline. A second signal for LyoTalk is defensible but not "
        "required — 'met them at' says where you were, not that they exhibited.",
    ),
    Case(
        note="Just got FDA clearance on their strep panel and they're advertising for "
        "an assay development scientist.",
        expect_types={"regulatory_submission", "job_posting_relevant_science"},
        expect_scoreable=True,
        why="Two independent signals in one sentence. Both should be extracted; "
        "returning only one is a miss. Two distinct types on the same date should "
        "trigger the 1.3x compounding multiplier downstream.",
    ),
    Case(
        note="Seemed interested, might be worth a follow up.",
        expect_types=set(),
        expect_scoreable=False,
        why="THE IMPORTANT ONE. Contains no evidence of anything. Must return an empty "
        "signal list. If the parser reaches for a type here it will manufacture "
        "pipeline out of small talk, which is the failure this tool exists to prevent.",
    ),
    Case(
        note="New sterile fill-finish line coming online in Q3, adding lyo capacity.",
        expect_types={"new_lyo_capacity"},
        expect_scoreable=True,
        why="Straightforward Product A signal. Lyo capacity investment implies cycle "
        "development work. Timeline should capture Q3.",
    ),
]


def _weight_for(type_key: str | None) -> float | None:
    if not type_key:
        return None
    spec = get_taxonomy().by_key(type_key)
    return spec.base_weight if spec else None


def run_case(index: int, case: Case | None, note: str) -> bool:
    print(f"\n{BOLD}{'─' * 78}{OFF}")
    print(f"{BOLD}NOTE {index}{OFF}  {note}")

    if case:
        expected = ", ".join(sorted(case.expect_types)) or "(nothing scoreable)"
        print(f"\n{DIM}EXPECTED{OFF}  {expected}")
        if case.also_plausible:
            print(f"{DIM}        {OFF}  also acceptable: {', '.join(sorted(case.also_plausible))}")
        print(f"{DIM}WHY     {OFF}  {case.why}")

    outcome = parse_entry(note)

    if not outcome.ok:
        print(f"\n{RED}PARSE FAILED{OFF}  {outcome.error}")
        return False

    parsed = outcome.data
    signals = list(parsed.signals)

    print(f"\n{DIM}RAW STRUCTURED OUTPUT{OFF}")
    print(json.dumps(parsed.model_dump(), indent=2, default=str))

    print(f"\n{DIM}READING{OFF}")
    if not signals:
        print(f"  nothing_scoreable = {parsed.nothing_scoreable}")
        print(f"  reasoning         = {parsed.reasoning}")
    for s in signals:
        weight = _weight_for(s.type_key)
        print(f"  • signal type  {s.type_key}")
        print(f"    weight       {weight:g}   {DIM}(from taxonomy, not from the model){OFF}")
        print(f"    product fit  {s.product_fit}")
        print(f"    timeline     {s.timeline or '—'}")
        print(f"    confidence   {s.confidence:.2f}")
        print(f"    detected     {s.detected_date or '— (falls back to today)'}")
        print(f"    summary      {s.summary}")
        print(f"    evidence     {DIM}{s.evidence}{OFF}")

    tokens = f"{outcome.input_tokens} in / {outcome.output_tokens} out"
    print(f"\n{DIM}tokens: {tokens}   model: {outcome.model}{OFF}")

    if not case:
        return True

    got = {s.type_key for s in signals}
    acceptable = case.expect_types | (case.also_plausible or set())

    if not case.expect_scoreable:
        if signals:
            print(
                f"\n{RED}{BOLD}FAIL{OFF}  Expected nothing scoreable, got "
                f"{', '.join(sorted(got))}. The parser invented a signal to fill the "
                f"schema. This manufactures pipeline out of nothing."
            )
            return False
        print(f"\n{GREEN}{BOLD}MATCH{OFF}  Correctly returned no signal.")
        return True

    missing = case.expect_types - got
    unexpected = got - acceptable

    if missing:
        print(f"\n{RED}{BOLD}FAIL{OFF}  Missing expected signal(s): {', '.join(sorted(missing))}")
        return False
    if unexpected:
        print(
            f"\n{YELLOW}{BOLD}PARTIAL{OFF}  Found everything expected, plus unexpected: "
            f"{', '.join(sorted(unexpected))}. Judge whether that is a real signal or a stretch."
        )
        return True

    extra = got - case.expect_types
    note_extra = f" (plus acceptable {', '.join(sorted(extra))})" if extra else ""
    print(f"\n{GREEN}{BOLD}MATCH{OFF}  {', '.join(sorted(got))}{note_extra}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the signal parser against known notes.")
    parser.add_argument("note", nargs="*", help="parse one note of your own instead")
    parser.add_argument("--file", help="a file with one note per line")
    args = parser.parse_args(argv)

    cfg = get_config()
    if not api_key_present():
        print(
            f"{RED}No ANTHROPIC_API_KEY found.{OFF}\n\n"
            "Put it in a file called .env next to config.yaml:\n\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n\n"
            "then run this again. Nothing else is needed."
        )
        return 2

    print(f"{BOLD}Parser check{OFF}")
    print(f"{DIM}model {cfg.llm['parser_model']}, effort {cfg.llm.get('parser_effort')}, "
          f"{len(get_taxonomy().types)} signal types loaded{OFF}")

    if args.file:
        notes = [n.strip() for n in open(args.file, encoding="utf-8") if n.strip()]
        cases = [(None, n) for n in notes]
    elif args.note:
        cases = [(None, " ".join(args.note))]
    else:
        cases = [(c, c.note) for c in CASES]

    results = [run_case(i, case, note) for i, (case, note) in enumerate(cases, 1)]

    print(f"\n{BOLD}{'─' * 78}{OFF}")
    passed = sum(results)
    if all(results):
        print(f"{GREEN}{BOLD}{passed}/{len(results)} behaved as expected.{OFF}")
        return 0
    print(f"{RED}{BOLD}{passed}/{len(results)} behaved as expected — see FAIL above.{OFF}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
