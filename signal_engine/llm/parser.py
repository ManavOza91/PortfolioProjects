"""Natural-language signal parser.

One mechanism, three entry points — company/signal note, contact note, manager
insight. Free text in, structured signal out.

The signal taxonomy is injected into the system prompt from data/taxonomy.yaml at
call time. No signal type is named anywhere in this file. Swap the taxonomy and the
parser recognises a different domain's signals with no code change.

Two behaviours matter more than accuracy:

  * If a note contains nothing scoreable, the parser must say so. An empty signal
    list is a correct answer. A fabricated score is not.

  * No category is ever excluded. The previous system excluded "veterinary" and
    would have blocked a veterinary diagnostics company that actually engaged.
    Entry is earned by evidence, never denied by category.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, create_model

from ..config import Taxonomy, get_config, get_taxonomy
from .client import ParseOutcome, parse_structured

ProductFit = Literal["A", "B", "both"]


# ---------------------------------------------------------------------------
# Schemas — built at runtime so type_key is constrained to the live taxonomy
# ---------------------------------------------------------------------------


def _taxonomy_fingerprint(taxonomy: Taxonomy) -> tuple:
    return tuple((t.key, t.label, t.base_weight) for t in taxonomy.types)


@lru_cache(maxsize=4)
def _models_for(fingerprint: tuple) -> dict[str, type[BaseModel]]:
    keys = tuple(k for k, _, _ in fingerprint)
    if not keys:
        raise ValueError("Taxonomy has no signal types — nothing can be parsed.")

    # Literal accepts a tuple and expands it, so the model sees a real enum of the
    # taxonomy's keys rather than a free-text field it could hallucinate into.
    type_key_enum = Literal[keys]  # type: ignore[valid-type]

    parsed_signal = create_model(
        "ParsedSignal",
        type_key=(
            type_key_enum,
            ...,
        ),
        product_fit=(ProductFit, ...),
        confidence=(float, ...),
        evidence=(str, ...),
        summary=(str, ...),
        detected_date=(str | None, ...),
        timeline=(str | None, ...),
        __doc__=(
            "One buying signal found in the text. `evidence` must quote or closely "
            "paraphrase the words in the note that justify it."
        ),
    )

    parsed_entry = create_model(
        "ParsedEntry",
        company_name=(str | None, ...),
        company_domain=(str | None, ...),
        company_country=(str | None, ...),
        company_segment=(str | None, ...),
        contact_full_name=(str | None, ...),
        contact_job_title=(str | None, ...),
        contact_email=(str | None, ...),
        contact_linkedin_url=(str | None, ...),
        contact_persona=(str | None, ...),
        signals=(list[parsed_signal], ...),  # type: ignore[valid-type]
        nothing_scoreable=(bool, ...),
        reasoning=(str, ...),
    )

    parsed_insight = create_model(
        "ParsedInsight",
        segment=(str | None, ...),
        geography=(str | None, ...),
        signal_type=(type_key_enum | None, ...),  # type: ignore[valid-type]
        weight_adjustment=(float, ...),
        summary=(str, ...),
        expiry_days=(int, ...),
        confidence=(float, ...),
        nothing_scoreable=(bool, ...),
    )

    return {"signal": parsed_signal, "entry": parsed_entry, "insight": parsed_insight}


def entry_model() -> type[BaseModel]:
    return _models_for(_taxonomy_fingerprint(get_taxonomy()))["entry"]


def insight_model() -> type[BaseModel]:
    return _models_for(_taxonomy_fingerprint(get_taxonomy()))["insight"]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _taxonomy_block(taxonomy: Taxonomy) -> str:
    lines = []
    for t in taxonomy.types:
        ex = "; ".join(t.examples[:4])
        lines.append(
            f"- key: {t.key}\n"
            f"  name: {t.label}\n"
            f"  product: {t.product_fit} | weight: {t.base_weight:g} | "
            f"sources: {t.sources}{' | structural, never decays' if not t.decays else ''}\n"
            f"  means: {t.description}\n"
            f"  sounds like: {ex}"
        )
    return "\n".join(lines)


def build_entry_system_prompt() -> list[dict[str, Any]]:
    """Stable across calls, so it caches. The date goes in the user turn."""
    cfg = get_config()
    taxonomy = get_taxonomy()
    products = cfg.products

    text = f"""\
You extract buying signals from a salesperson's free-text notes about companies in \
the scientific instrument market.

THE TWO PRODUCTS

Product A ({products.get('A', {}).get('code', 'A')}) — {products.get('A', {}).get('name', '')}.
Bought by: {" ".join(str(products.get('A', {}).get('buyers', '')).split())}

Product B ({products.get('B', {}).get('code', 'B')}) — {products.get('B', {}).get('name', '')}.
Bought by: {" ".join(str(products.get('B', {}).get('buyers', '')).split())}

THE SIGNAL TAXONOMY

Use only these keys. Each entry gives the key you must return, what it means, and \
the kind of language it sounds like.

{_taxonomy_block(taxonomy)}

HOW TO EXTRACT

1. Read the note for evidence that this company might buy. Match each piece of \
evidence to exactly one taxonomy key. One note can contain several signals; return \
one entry per distinct signal.

2. NEVER INVENT A SIGNAL. If the note contains nothing that matches the taxonomy, \
return an empty signals list and set nothing_scoreable to true. An empty result is a \
correct and useful answer — a company with no signal must not enter the pipeline. Do \
not stretch a vague remark to fit a key. Do not treat a company simply being \
interesting, well-known, or a good logo as a signal.

3. `evidence` must quote or closely paraphrase the actual words in the note that \
justify the signal. If you cannot point at specific words, you do not have a signal.

4. `summary` is one plain sentence, written so it can be shown on a queue as the \
reason to contact this company today. Be specific and concrete. Write "Doing manual \
bead production, evaluating a machine next year", not "Shows interest in beads".

5. `confidence` is 0.0 to 1.0 and reflects how certain you are the signal is real and \
correctly typed. Explicit first-hand statements: 0.9-1.0. Clear second-hand report: \
0.7-0.9. Inference from indirect wording: 0.4-0.7. Guesswork: below 0.4 — and if you \
are guessing, prefer returning no signal at all.

6. `detected_date`: an ISO date (YYYY-MM-DD) if the note states or clearly implies \
when the thing happened. Resolve relative references such as "last month" or "at the \
March show" against today's date, given in the user message. If there is no basis for \
a date, return null and the system will use today.

7. `product_fit` is which product the evidence points at. Use the taxonomy's product \
for the key unless the note clearly indicates otherwise.

8. `timeline` captures any stated buying timeline in the note's own terms \
("evaluating next year", "budget in Q3"). Null if none is stated.

RULES THAT OVERRIDE YOUR INSTINCTS

- NO CATEGORY EXCLUSIONS. Never dismiss a company because of its market segment, \
size, geography, or field — including veterinary, agricultural, academic, food, or \
any other adjacent area. A veterinary diagnostics company is exactly as eligible as a \
large pharma. Entry is earned by evidence, never denied by category.

- FUNDING AND GRANTS ARE QUALIFIERS, NOT TRIGGERS. Tag them when present, but never \
treat money alone as a reason to buy. They indicate affordability and scaling intent.

- THE MOST VALUABLE SIGNALS ARE HUMAN-OBSERVED. Manual or DIY production at capacity, \
competitor dissatisfaction, and format shifts appear in no public database. When a \
note describes one of these, that is the highest-value thing in the note. Do not \
under-weight it because it sounds anecdotal — anecdote is exactly how it arrives.

- Extract contact details only if the note actually contains them. Do not infer an \
email address from a name and domain.

Fill `reasoning` with one short sentence: the strongest signal you found, or why the \
note contains nothing scoreable."""

    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_insight_system_prompt() -> list[dict[str, Any]]:
    taxonomy = get_taxonomy()
    text = f"""\
You convert a sales manager's segment-level observation into a structured scoring rule.

A manager insight is not about one company. It is a claim about a segment, a \
geography, or a kind of buyer — for example: "Nordic point-of-care respiratory \
developers are scaling fast; three asked about ambient shipping."

Extract:

- `segment`: the kind of company the claim is about, in the manager's own terms \
("point-of-care respiratory developers"). Null if the claim is not segment-specific.
- `geography`: the region or country, if any. Null otherwise.
- `signal_type`: the taxonomy key this insight relates to, if it maps cleanly to one. \
Null if it does not — many insights are about a segment rather than a signal type.
- `weight_adjustment`: how many points to add to companies matching this rule. Use a \
small number. 3-6 for a soft observation, 7-12 for a strong specific one backed by \
named evidence, up to 15 for something the manager states with certainty and detail. \
Negative values are allowed if the manager is warning a segment off.
- `expiry_days`: how long this should stay live before it must be re-confirmed. \
Default 90. Longer only if the claim is structural rather than about current momentum.
- `confidence`: 0.0-1.0, how clearly the text states an actionable rule.
- `summary`: one sentence restating the rule as it will be shown in the interface.

If the text is not an actionable segment-level rule — it is chit-chat, a note about a \
single named company, or too vague to act on — set nothing_scoreable to true and \
weight_adjustment to 0. Do not manufacture a rule out of an offhand remark.

Never exclude a segment on the grounds of category. Reduce weight only when the \
manager explicitly says to.

Available taxonomy keys for signal_type:
{_taxonomy_block(taxonomy)}"""

    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _today_line(today: dt.date | None = None) -> str:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return f"Today's date is {today.isoformat()} ({today.strftime('%A %d %B %Y')})."


def parse_entry(
    note: str,
    *,
    company_hint: str | None = None,
    contact_hint: str | None = None,
    today: dt.date | None = None,
) -> ParseOutcome:
    """Parse a company note or a contact note. Same mechanism, different hints."""
    cfg = get_config()
    parts = [_today_line(today), ""]
    if company_hint:
        parts.append(f"The user has already named the company: {company_hint}")
    if contact_hint:
        parts.append(f"The user has already given this contact detail: {contact_hint}")
    if company_hint or contact_hint:
        parts.append("")
    parts.append("NOTE:")
    parts.append(note.strip())

    return parse_structured(
        system=build_entry_system_prompt(),
        user="\n".join(parts),
        output_format=entry_model(),
        model=str(cfg.llm.get("parser_model", "claude-sonnet-4-6")),
        effort=cfg.llm.get("parser_effort"),
    )


def parse_manager_insight(text: str, *, today: dt.date | None = None) -> ParseOutcome:
    cfg = get_config()
    user = f"{_today_line(today)}\n\nMANAGER INSIGHT:\n{text.strip()}"
    return parse_structured(
        system=build_insight_system_prompt(),
        user=user,
        output_format=insight_model(),
        model=str(cfg.llm.get("parser_model", "claude-sonnet-4-6")),
        effort=cfg.llm.get("parser_effort"),
    )


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def coerce_date(value: Any, *, default: dt.date | None = None) -> dt.date:
    """Accept an ISO date, or fall back to today. Never raises."""
    default = default or dt.datetime.now(dt.timezone.utc).date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                return dt.datetime.strptime(value.strip()[:10], fmt).date()
            except ValueError:
                continue
    return default
