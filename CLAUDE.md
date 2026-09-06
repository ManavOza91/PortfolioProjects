# CLAUDE.md

Signal-driven outbound engine for capital scientific instrument sales (freeze-drying
microscope = Product A, lyobead generator = Product B). **A company enters the pipeline
because a buying signal fired, not because it's on a target list.** Target lists are
watchlists, never fetch queues. Output is a ranked company queue with a stated reason;
finding the right person is a manual step the user does themselves.

**Heading for multi-tenant SaaS** sold to other life science instrument companies —
see load-bearing decision 14 before adding anything.

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
| `signal_engine/jobs.py` | Background jobs behind the Tasks page buttons |
| `signal_engine/directory.py` | ICP research directory — separate table, no scores, no rank |
| `signal_engine/reporting.py` | Read path for the dashboard |
| `signal_engine/web/` | FastAPI app, templates, CSS |
| `portable/` | Stack-agnostic spec for a rebuild elsewhere (see below) |
| `scripts/` | `parser_check.py`, `export_portable.py` |

Commands: `./run.sh` (or `.\run.ps1` on Windows) — also `demo`, `seed`, `rescore`,
`import [--dry-run]`, `detect [--dry-run|--source|--since|--backfill|--discover]`, `parser-check`,
`directory-refresh [--dry-run|--keyword|--source]`, `export-portable`, `purge-contacts`, `test`.

**The user does not use a terminal.** `detect` and `directory-refresh` are buttons on
the Tasks page (`signal_engine/jobs.py`, config `ui.jobs`); Windows gets
`Start Signal Engine.bat` and `Update Signal Engine.bat`. Anything new that would
otherwise need a command needs a button too, and the updater must never overwrite
`config.yaml` or `data/`.

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
Request shape is covered by stubbed tests only. All four detection sources — openFDA,
EUDAMED, MHRA PARD, Google News — are verified live. **MHRA PARD is an undocumented
POST endpoint** behind its public search page (`/searchManufacturers`), so it may
change shape without notice; failure is reported and the run continues.
SBIR was **removed** (US-only, permanently `TooManyRequestsError` at source).

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
10. **`Detection.unique_per_event`.** False for the press: three outlets covering one
    announcement collapse to one signal (company + type + 14 days), or a single event
    compounds itself into Tier A. True for sources that issue a record per event
    (K-number, EUDAMED UUID) so two clearances in one week both survive.
11. **EUDAMED publishes no registration date.** It is aggregated to ONE signal per
    company (not one per device) and pinned below the auto bar — it is a standing
    fact, not a dated event. Don't "fix" it by dating it today and letting it score.
12. **`directory_entries` is a separate table from `companies`, and must stay one.**
    Fit is what a company IS; a signal is what it DID, and only the second earns
    pipeline entry. Because directory rows are not companies, the queue, the Buddy
    Score and every report are structurally unable to see them — no filter to
    forget, no flag to get wrong. Guarded by `tests/test_directory.py`. Also:
    **the directory is never ranked.** The ICP attributes are coarse and mostly
    shared, so any ordering sorts by how much we happen to know rather than by
    fit. It is filtered and alphabetical. Don't add a fit score.
13. **Unknown is not zero.** Every directory attribute is nullable and a NULL
    renders as "unknown". `target_size_bands` includes `unknown` on purpose — if
    absence of evidence excluded a company, the list would be the companies we
    happen to have data on rather than the companies that fit. Corollary:
    **a floor device count may only confirm the unbounded top size band.**
    "At least 3" fits a micro business and bioMérieux equally, so anything below
    `large` stays unknown (`size_band(..., is_floor=True)`).
    Also: **IVDR risk class is the only working IVD filter EUDAMED offers.**
    Classes A–D are IVDs; I/IIa/IIb/III are MDR. There is no country, category
    or legislation filter that works — trade-name search finds almost nothing
    because trade names are brand names. Don't replace the sweep with keywords.
14. **Nothing about THIS user's business may live in code.** This is heading for
    multi-tenant SaaS sold to other life science instrument companies, so the
    freeze-drying microscope, the lyobead generator, the 14 signal types, the
    detection keywords, the ICP criteria and every scoring threshold are one
    customer's configuration — not defaults, not constants, not fallbacks in a
    `.get()`. A second customer selling chromatography columns must need zero code
    changes. This generalises #4: no signal type is named in logic, and neither is
    a product, a company, a keyword or a threshold. Anything currently global
    (`config.yaml`, `data/taxonomy.yaml`, `detection.keywords`) becomes per-tenant;
    the `tenant_id` columns are already there and already threaded through.

## Future work

**Onboarding generator.** Customer enters their company name and what they sell;
Claude generates a starting signal taxonomy and ICP definition, which they then
refine. Doubles as the self-serve demo — it's what turns "here is an empty
scoring engine" into something a stranger can evaluate in five minutes.

Two constraints on it:

- **Needs API access.** Not buildable on the current no-key setup, and the
  no-key manual path must keep working for customers who never turn it on.
- **Generated weights are a starting point, never authoritative.** They come from
  a model guessing at a market it has not sold into. The UI must say so, and the
  numbers must be editable before anything is scored against them — otherwise a
  customer inherits invented thresholds as if they were evidence. This user's own
  weights came from real conversion data; a generated set has no such backing.

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
