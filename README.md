# Signal Engine

A company-and-signal intelligence tool for capital scientific instrument sales.

**A company enters the pipeline because a buying signal fired — not because it
appears on a target list.** No signal, no entry. Target lists are watchlists for
monitoring, never fetch queues.

The output is a ranked queue of companies with active buying signals and a stated
reason for each. Finding the right person at each company is a manual research step
you perform yourself, and it is the step that produces your best outreach.

---

## Running it

**Windows** — open PowerShell in this folder and run:

```powershell
.\run.ps1
```

**macOS or Linux** — open Terminal in this folder and run:

```bash
./run.sh
```

That is the whole thing. It installs everything it needs (including `uv` and Python
on Windows), creates the database, seeds the signal taxonomy, and opens
<http://127.0.0.1:8420> in your browser.

The two runners are equivalent — every command below works with either. Substitute
`.\run.ps1` for `./run.sh` on Windows.

To use the AI features (note parsing, and the coach when it lands), put your
Anthropic API key in a file called `.env` next to this README:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without a key the system still runs — notes are stored raw and flagged as unparsed,
so nothing you type is ever lost. You can parse them later.

### Other commands

| Command | What it does |
|---|---|
| `./run.sh` | Migrate, seed, and serve (the normal one) |
| `./run.sh migrate` | Apply database migrations only |
| `./run.sh seed` | Reload `data/taxonomy.yaml` into the database |
| `./run.sh rescore` | Recompute every company score from its signals |
| `./run.sh import --dry-run` | Preview a CSV import without writing anything |
| `./run.sh import` | Run the CSV import |
| `./run.sh demo` | Load a small worked example so you can see the shape of it |
| `./run.sh purge-contacts` | Delete **all** personal data, keeping scoring intact |
| `./run.sh parser-check` | Run the parser against known notes and grade the output |
| `./run.sh export-portable` | Regenerate `portable/` (see below) |
| `./run.sh test` | Run the test suite |

---

## The stack, and why

| Layer | Choice | Why this one |
|---|---|---|
| Language | Python 3.11 | Best Anthropic SDK and data tooling. |
| Runner | `uv` | One command, no virtualenv ritual. The only prerequisite. |
| Web | FastAPI + Jinja2, plain HTML forms | **No build step, no npm, no CDN.** Works with the network off. |
| Database | SQLite via SQLAlchemy 2.0 + Alembic | Postgres becomes a connection-string change, not a rewrite. Alembic evolves the schema without losing data. |
| Parsing | Anthropic SDK + Pydantic via `messages.parse()` | Schema-validated output — the parser is structurally incapable of returning a signal type that isn't in your taxonomy. |

There is deliberately no JavaScript framework. A tool that has to start with one
command on a non-developer's laptop cannot depend on a working `npm install`.

---

## Rebuilding this in another stack — `portable/`

If this becomes a SaaS product on a different stack, `portable/` is what ports.
It holds the domain logic as data, config, prose specs, plain SQL and golden test
vectors — enough to reimplement the system **without reading any Python**.

| File | What it is |
|---|---|
| `portable/SCORING.md` | Decay, compounding, tiers, what scores and what doesn't — the formulas in prose |
| `portable/BUDDY-SCORE.md` | The four components and the invariant that must not break |
| `portable/taxonomy.json` | The signal types, weights, product fits, decay behaviour |
| `portable/scoring-parameters.json` | Every threshold and constant |
| `portable/parser-system-prompt.txt` | The complete parser prompt, taxonomy rendered in |
| `portable/parser-output-schema.json` | JSON Schema for the structured output |
| `portable/schema.postgres.sql` | Plain PostgreSQL DDL — the dialect Supabase uses |
| `portable/test-vectors.json` | Golden inputs and expected outputs, to verify a reimplementation |

Start at `portable/README.md`. Everything except the three `.md` files is generated
by `./run.sh export-portable`, and `tests/test_portable.py` fails if it goes stale —
so the spec cannot silently drift away from the code that actually runs.

---

## The two files you edit

Nothing about this system's behaviour is hardcoded in Python. Two files control it.

### `data/taxonomy.yaml` — what counts as a signal

The fourteen signal types, their weights, which product each points at, and whether
each decays. Add a type, change a weight, retire one — then `./run.sh seed`.

Application code never names a signal type. A different instrument company replaces
this file wholesale and the system is theirs.

### `config.yaml` — how scoring and the Buddy Score behave

Decay rate and floor, the compounding window and multiplier, tier thresholds, your
weekly touch target, and which Claude models to use.

---

## How scoring works

**Slow decay.** Signals lose 10% of their weight per quarter and stop at 40% of the
original. They never reach zero. One company was lost to a competitor in 2024 and
re-engaged in 2026 wanting a second unit; another raised funding in 2024 and is buying
now. Standard sales-tool decay logic — where a six-month-old signal is dead — is wrong
for capital equipment.

**Structural signals never decay.** A disclosed multi-product pipeline does not stop
being multi-product. `decays: false` in the taxonomy.

**Compounding.** Two independent signals inside 90 days multiply the combined score by
1.3. Observed pattern: regulatory submission → funding → authorisation within seven
months at one company.

**Funding is a qualifier, not a trigger.** It carries a low weight and never surfaces
a company on its own. It indicates affordability and scaling intent, nothing more.

**No hard category exclusions.** Entry is earned by evidence, never denied by category.
The previous system excluded "veterinary" and would have blocked a veterinary
diagnostics company that actually engaged.

**Tiers are calibrated against the taxonomy.** The Tier B threshold sits at or below
the heaviest signal weight, so one top-tier observation — DIY production at capacity,
say — is on its own enough to count as quality outreach. If it were set higher, the
Buddy Score would mark acting on the single best signal available as spray. A test
guards this, so editing a weight or a threshold in isolation will tell you.

**Nothing scoreable means nothing scored.** If a note contains no recognisable signal
it is stored with status `unscored` rather than assigned a fabricated number.
Automatically detected signals below the confidence threshold go to a review queue.

---

## The Buddy Score

Pipeline value is a lagging indicator. Outbound behaviour is leading. Scored 0–100,
weekly, with monthly, quarterly and annual rollups.

| Component | Weight | Measures |
|---|---|---|
| Progression | 30 | Replies → conversations → meetings booked |
| Signal quality | 25 | % of outreach to Tier A/B signal companies |
| Volume | 25 | Touches against your own target |
| Consistency | 20 | Active days, no gaps — steady beats bursts |

Signal quality is weighted high deliberately. Forty touches to unsignalled companies
scores worse than fifteen to signalled ones — otherwise the score trains you back into
the spray model it exists to replace. There is a test that enforces exactly this.

Meetings are the terminal metric, not orders. Cycles run six months to two years, which
is far too long for orders to be usable feedback.

---

## Personal data

The contacts table is entirely optional. The system runs on companies and signals alone.

`activity.company_id` is `NOT NULL`; `activity.contact_id` is nullable with
`ON DELETE SET NULL`. So:

```bash
./run.sh purge-contacts
```

deletes every contact and every scrap of personal data, and scoring, the Buddy Score
and all reporting keep working on company-level touches. That is a property of the
schema, not a promise — there is a test for it.

This keeps GDPR obligations minimal and makes a contacts-free commercial version
viable for regulated industries.

---

## Importing your existing data

Companies import as **watchlist entries with no signal history**. Contacts import as
plain records attached to companies. They are a starting address book, not scored
pipeline — nothing gets a score until a signal fires.

1. Export your Google Sheets tabs to CSV and put them in `data/import/`.
2. Copy `data/mapping.example.yaml` to `data/mapping.yaml` and edit it so the
   left-hand side matches your CSV column headers.
3. `./run.sh import --dry-run` to see exactly what it would create.
4. `./run.sh import` when it looks right.

The importer is idempotent — running it twice does not duplicate companies. It matches
on domain first, then on a normalised company name.

---

## What is built

| # | Phase | Status |
|---|---|---|
| 1 | Schema, database, import | ✅ |
| 2 | Natural-language signal parser | ✅ |
| 3 | Manual company and contact entry with note parsing | ✅ |
| 4 | Scoring engine: weights, compounding, decay, tiers | ✅ |
| 5 | Activity logging | ✅ |
| 6 | Buddy Score and rollups | ✅ |
| 7 | Interactive dashboard | ✅ |
| 8 | Manager insight layer with expiry and outcome scoring | schema only |
| 9 | Coach: prioritise, draft, critique, notice | not started |
| 10 | Automated signal detection (openFDA, grants, news, jobs) | not started |

Phases 8–10 have their schema and config in place. Phase 9's model setting
(`llm.coach_model`) is already read from config.

---

## Out of scope, deliberately

- Net-new logo outreach only. No account management, no CRM functionality, no
  post-sale workflow.
- **No automated sending.** The tool drafts; you send. Every message gets human review.
- No third-party contact enrichment provider. No scraping of sites that prohibit it.
  LinkedIn scraping is out.

---

## Layout

```
config.yaml              scoring, tiers, Buddy Score, models, server
data/taxonomy.yaml       the signal taxonomy — editable data, not code
data/mapping.example.yaml  CSV column mapping template
data/import/             drop your CSV exports here
signal_engine/
  config.py              config + taxonomy loading and validation
  models.py              the schema, with the design constraints it enforces
  db.py                  engine and sessions
  scoring.py             decay, compounding, tier assignment
  buddy.py               Buddy Score and rollups
  importer.py            CSV import with mapping and dry-run
  seed.py                taxonomy -> database
  llm/parser.py          natural-language signal parser
  web/                   FastAPI app, templates, dashboard
alembic/                 database migrations
tests/                   scoring, Buddy Score, and data-separation tests
```

## Moving to Postgres

Change one line in `config.yaml`:

```yaml
database:
  url: "postgresql+psycopg://user:pass@localhost/signal_engine"
```

Then `./run.sh migrate`. No application code changes — every query goes through
SQLAlchemy.
