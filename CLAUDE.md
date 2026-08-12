# CLAUDE.md

Signal-driven outbound engine for capital scientific instrument sales (freeze-drying
microscope = Product A, lyobead generator = Product B). **A company enters the pipeline
because a buying signal fired, not because it's on a target list.** Target lists are
watchlists, never fetch queues. Output is a ranked company queue with a stated reason;
finding the right person is a manual step the user does themselves.

## Working rules (user is on a Pro plan with a 5-hour limit — respect these)

- Don't re-read files already read this session unless the user changed them.
- Don't run the full suite (`./run.sh test`) unless asked, or you changed something
  that could plausibly break it. Run single test files instead.
- No proactive refactoring, tidying, or "while I'm here" improvements.
- Before any task needing more than a few tool calls, say roughly what's involved and
  wait for confirmation.
- Targeted edits over whole-file rewrites.
- Short summaries. No recap of what you just did unless something went wrong.

## Architecture

Python 3.11, FastAPI + Jinja2 (plain HTML forms — no JS framework, no build step),
SQLite via SQLAlchemy 2.0 + Alembic, Anthropic SDK with `messages.parse()`.

| Path | Role |
|---|---|
| `config.yaml` | All thresholds, weights, targets, model choice. Nothing hardcoded. |
| `data/taxonomy.yaml` | The 14 signal types — **editable data, not code** |
| `signal_engine/models.py` | Schema; the docstring lists the constraints it enforces |
| `signal_engine/scoring.py` | Decay, compounding, tiers |
| `signal_engine/buddy.py` | Buddy Score + rollups |
| `signal_engine/ingest.py` | Write path: notes → companies/signals/contacts/activity |
| `signal_engine/llm/parser.py` | Prompt + dynamic Pydantic schema built from taxonomy |
| `signal_engine/detect/` | Auto-detection; one module per source + `runner.py` (all judgement) |
| `signal_engine/reporting.py` | Read path for the dashboard |
| `signal_engine/web/` | FastAPI app, templates, CSS |
| `portable/` | Stack-agnostic spec for a rebuild elsewhere (see below) |
| `scripts/` | `parser_check.py`, `export_portable.py` |

Commands: `./run.sh` (or `.\run.ps1` on Windows) — also `demo`, `seed`, `rescore`,
`import [--dry-run]`, `detect [--dry-run|--source|--since|--discover]`, `parser-check`,
`export-portable`, `purge-contacts`, `test`.

## Built vs not

**Built (phases 1–7, 10):** schema + migrations, CSV import, NL parser, manual entry,
scoring engine, activity logging, Buddy Score, dashboard, auto-detection. 155 tests pass.

**Not built:** phase 8 manager-insight scoring loop (table + parsing exist; the
outcome-weighting loop does not), phase 9 coach (prioritise/draft/critique/notice —
only `llm.coach_model` config exists). No jobs source in `detect` — there is no free
public jobs API worth trusting, so that signal type stays manual.

**No API key required.** Signals are entered on a form (`/add/signal` → `create_signal`);
the taxonomy supplies the weight. `anthropic` is an optional extra (`.[llm]`) and is NOT
installed by default — keep every import of it lazy or inside `llm/client.get_client()`.
When a key IS present, `llm_ready` turns on and the free-text note box reappears by
itself in `add.html` and `company.html`. Don't reintroduce a "no API key" warning.

**Never verified:** a live parser call. No key has been available in any session.
Request shape is covered by stubbed tests only. Same for the **SBIR** detector — that
API returns `TooManyRequestsError` at source on every request, so its response shape
follows sbir.gov's docs but has never been seen. openFDA and Google News are verified live.

## Load-bearing decisions — do not "tidy" these

1. **`activity.contact_id` is nullable, `ON DELETE SET NULL`; `activity.company_id` is
   NOT NULL.** This is what lets every contact be deleted — all personal data — with
   scoring, Buddy Score and reporting intact. Making `contact_id` NOT NULL or cascading
   the delete silently destroys the contacts-free edition.
2. **`activity` stores tier and governing signal *at time of contact*.** Denormalised on
   purpose. Joining to current tier makes rescoring rewrite history and the
   signal-to-meeting report becomes fiction.
3. **Tier B threshold must be ≤ the heaviest taxonomy weight** (currently 25 ≤ 30).
   Buddy Score signal-quality only counts Tier A/B, so a higher B means acting on the
   best available signal scores as spray. Guarded by a test.
4. **No signal type is named in application logic.** The parser's `type_key` enum is
   generated from the taxonomy at call time — that's what stops the model inventing a
   type. Never hardcode the enum.
5. **No category exclusions anywhere.** The old system excluded "veterinary" and would
   have blocked a company that actually engaged. Entry is earned by evidence.
6. **Nothing scoreable → `status='unscored'`, never a fabricated number.** Raw text is
   always stored, including when parsing fails. Parser is fail-soft by design.
7. **Signals decay to a 40% floor, never zero.** Structural types don't decay at all.
   Capital cycles run 6 months to 2 years; standard decay logic is wrong here.
8. **Buddy Score invariant:** 40 touches to unsignalled companies must score worse than
   15 to signalled ones. `tests/test_buddy.py::test_spray_scores_worse_than_targeted`.
9. **Detectors never write or judge.** They return `Detection` objects; `detect/runner.py`
   does matching, confidence and the review gate for every source identically. A partial
   name match is pinned below `auto_review_threshold` on purpose, and a discovered
   company can never auto-score — both are what stop detection manufacturing pipeline.

## portable/

Spec for rebuilding in another stack (user may move to React + Supabase via Lovable).
`SCORING.md` and `BUDDY-SCORE.md` are hand-written prose formulas; everything else
(`taxonomy.json`, `scoring-parameters.json`, prompts, JSON Schemas, `schema.postgres.sql`,
`test-vectors.json`) is generated by `./run.sh export-portable`.
`tests/test_portable.py` fails if it goes stale. **After changing taxonomy, config or
models, regenerate and commit.**

## Repo state

Branch `claude/signal-driven-outbound-engine-vd3dog`. **Push is blocked** — this session
type has anonymous read access only (no credential helper, no token), so GitHub returns
403. The user pushes from their own machine. Don't retry pushes; don't spend turns on it.
