# portable/

Everything needed to rebuild this system in another stack — React + Supabase, or
anything else — **without reading a line of Python**.

The Python app in `signal_engine/` is one implementation of what is described here.
This directory is the specification, and it is the thing that survives a rewrite.

---

## What's here

### Read these first — the rules, in prose

| File | What it covers |
|---|---|
| **`SCORING.md`** | Decay, compounding, tiers, what scores and what doesn't, and the rules that override intuition |
| **`BUDDY-SCORE.md`** | The four components, the formulas, period handling, and the one invariant you must not break |

### Data and config — the numbers and the domain

| File | What it is |
|---|---|
| `taxonomy.json` | The 14 signal types: keys, weights, product fit, decay behaviour, and the descriptions the parser uses to recognise them |
| `scoring-parameters.json` | Every number: decay rate and floor, compounding window and multiplier, tier thresholds, confidence thresholds, Buddy Score weights and targets |

### The parser — prompts and schemas

| File | What it is |
|---|---|
| `parser-system-prompt.txt` | The complete system prompt, taxonomy already rendered in |
| `parser-user-template.txt` | Shape of the user turn |
| `parser-output-schema.json` | JSON Schema for the structured output |
| `manager-insight-system-prompt.txt` | System prompt for segment-level manager insights |
| `manager-insight-output-schema.json` | Its JSON Schema |

### The database

| File | What it is |
|---|---|
| `schema.postgres.sql` | Plain PostgreSQL DDL — the dialect Supabase uses |
| `schema.sqlite.sql` | Same schema, SQLite dialect |

### Verification

| File | What it is |
|---|---|
| `test-vectors.json` | Golden inputs and expected outputs for decay, compounding, scoring and tiers |

---

## Rebuilding: suggested order

1. **Load the schema.** `schema.postgres.sql` into Supabase. Read the header
   comment first — two constraints in it are load-bearing.
2. **Load the taxonomy** into `signal_types` from `taxonomy.json`.
3. **Implement scoring** from `SCORING.md`, reading its numbers from
   `scoring-parameters.json` rather than hardcoding them.
4. **Run `test-vectors.json` against it.** Every expected value must match. These
   were computed *by* the reference implementation, so they are authoritative, not
   aspirational. Do not move on until they pass.
5. **Wire up the parser** using the prompt and schema files.
6. **Implement the Buddy Score** from `BUDDY-SCORE.md`, then verify the
   40-unsignalled-vs-15-signalled case by hand.

---

## Three things that will look like details and are not

**1. `activity.contact_id` is nullable with `ON DELETE SET NULL`, while
`activity.company_id` is `NOT NULL`.**

That combination is what lets you delete every contact — all personal data — and
keep scoring, the Buddy Score and every report working on company-level touches.
It makes a contacts-free edition viable for regulated buyers and keeps GDPR
obligations minimal. A schema "tidy-up" that makes `contact_id NOT NULL`, or
cascades the delete, destroys the property silently.

**2. `activity` stores the company's tier and governing signal at the time of
contact.**

Denormalised on purpose. Join to the company's *current* tier instead and rescoring
retroactively rewrites whether past outreach was signal-driven — which makes the
"which signals produce meetings" report meaningless.

**3. The taxonomy is data everywhere, including in the prompt.**

No signal type is named in application logic in the reference implementation, and
the parser's output schema builds its `type_key` enum from the taxonomy at call
time — which is what makes the model structurally unable to invent a signal type.
If you hardcode an enum in TypeScript, you lose that and gain a maintenance
problem. Generate it.

---

## Keeping this directory honest

Everything except the three `.md` files is **generated**:

```
./run.sh export-portable
```

Regenerate after changing `data/taxonomy.yaml`, `config.yaml`, or the models. The
generated files carry a banner saying so. The prose specs are hand-written and are
the one thing you should edit directly — if you change a rule, change `SCORING.md`
or `BUDDY-SCORE.md` in the same commit.

---

## What is NOT in here

- The web UI. Deliberately — you're rebuilding that.
- CSV import. Mechanical, and specific to the source spreadsheet.
- Phases 8–10: the manager-insight scoring loop, the coach, automated signal
  detection. The schema and prompts for manager insights are here; the outcome-
  scoring loop that adjusts rule weights over time is not built in any stack yet.
