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

**On Windows, without a terminal:** double-click **Start Signal Engine.bat**. A
console window appears — that window *is* the app; leave it open and use the
browser, close it to stop. Right-click the .bat → Send to → Desktop to get a
shortcut. **Update Signal Engine.bat** fetches the latest code and leaves your
`config.yaml` and database untouched.

Everything the terminal did is also on the **Tasks** page in the app: detection
and directory refreshes are buttons, with live progress and the run summary.

<details>
<summary>Running it from a terminal instead</summary>

**Windows** — open PowerShell in this folder and run `.\run.ps1`.
**macOS or Linux** — open Terminal in this folder and run `./run.sh`.

Both are equivalent, and every command in the table below works with either.
</details>

That is the whole thing. It installs everything it needs (including `uv` and Python
on Windows), creates the database, seeds the signal taxonomy, and opens
<http://127.0.0.1:8420> in your browser.

**No API key needed, and no ongoing cost.** You record signals on a form: pick the
company, pick the signal from the taxonomy, set the date and timeline. The weight comes
from the taxonomy, so you never have to decide what a signal is worth. Scoring, decay,
compounding, tiers, the queue and the Buddy Score all work exactly the same.

*Optional:* if you later want free-text notes parsed into signals automatically, install
the extra and add a key —

```bash
uv pip install -e ".[llm]"
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

— and a "paste a note instead" box appears alongside the form. Nothing else changes.

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
| `./run.sh detect --dry-run` | Check public sources for new signals, writing nothing |
| `./run.sh detect` | Check public sources and file what it finds (see below) |
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
| 10 | Automated signal detection (openFDA, grants, news) | ✅ |

Phases 8 and 9 have their schema and config in place. Phase 9's model setting
(`llm.coach_model`) is already read from config. Job-posting detection is not
built — there is no free public jobs API worth trusting, so that signal type
stays a manual entry, which is where it came from.

---

## Checking public sources — `./run.sh detect`

Three free sources, no API key and no registration for any of them:

| Source | Covers | What it looks for | Weight it carries |
|---|---|---|---|
| **openFDA 510(k)** | US | Clearances naming your company as applicant | Strong — public record, dated |
| **EUDAMED** | EU | Devices registered on the EU market (MDR + IVDR) | Standing fact — always goes to review |
| **MHRA PARD** | UK | Registration to place devices on the UK market | Strong — public record, dated |
| **Google News RSS** | Global | Headlines naming your company **and** a taxonomy keyword | Weak — always goes to review |

Most of this market is not American, so openFDA alone sees very little of it.
EUDAMED and MHRA are what make a German or Singaporean manufacturer visible —
MHRA in particular catches non-UK companies selling into Britain through a UK
Responsible Person.

Two limitations worth knowing, both of them the sources' rather than choices:

- **EUDAMED publishes no registration date.** There is no "what's new since March"
  query to make. So it reports a standing fact — *"this company has 36 devices on
  the EU market, highest risk class IIb"* — as **one** signal per company rather
  than one per device, pinned below the auto bar so it always reaches you.
- **There is no grants source.** SBIR was removed: US-only, permanently returning
  `TooManyRequestsError` at source, and a grant is a qualifier rather than a
  trigger. Press coverage of a grant still classifies as `grant_award`.

It is **watchlist-first**: it checks companies you already track. It does not go
hunting for new ones unless you pass `--discover`, and anything it discovers
arrives as a watchlist entry with a signal waiting for review — never scored.

```bash
./run.sh detect --dry-run          # see what it would find, write nothing
./run.sh detect                    # file what it finds
./run.sh detect --source openfda   # one source only (repeatable)
./run.sh detect --since 2026-01-01 # default is the last 90 days
./run.sh detect --discover         # also look for companies you don't track
```

Everything found is marked `source = auto` with a link back to where it came
from. Two things decide where it lands:

1. **How sure the source is** the record is real (a 510(k) is certain; a headline is not).
2. **How sure we are it's your company.** An exact name match after stripping
   legal suffixes is trusted. A partial match — "Northwind" inside "Northwind
   Diagnostics Group" — deliberately scores *below* the auto bar.

The lower of the two is the confidence. At or above `scoring.auto_review_threshold`
(0.70) it scores; below it, it waits on the **Review** page for you to approve or
reject.

**One event is one signal.** Re-running never duplicates a find, and three outlets
reporting the same announcement collapse into a single signal rather than
compounding into a tier they didn't earn. Sources that issue a record per event —
a K-number, a EUDAMED UUID — are exempt, so two genuine clearances in the same
week both stand. The window is `detection.duplicate_event_window_days` (14).

A source being down is a normal outcome, not a failure: it is reported and the
rest of the run continues.

Keywords live in `config.yaml` under `detection.keywords` — plain data, edit them
as you learn how your market phrases things.

---

## The research directory — `./run.sh directory-refresh`

Two different questions, answered in two columns that never mix:

| | **Signalled** | **Worth a look** |
|---|---|---|
| Why it's there | A signal fired — what it **did** | It fits the ICP — what it **is** |
| Ranked? | Yes, by live signal strength | **No** |
| In the priority queue? | Yes | **Never** |
| What you do with it | Contact now | Research when you have time |

The directory is built from EUDAMED and MHRA — the registers of who actually
sells IVDs in your territories. It filters on territory, device-type keyword and
size, all of which live in `config.yaml` under `icp:`.

**It is deliberately not ranked.** The fit attributes are coarse and most
companies share them, so any ordering would sort by how much we happen to know
about a company rather than by how well it fits. A confident-looking rank built
on that would be misleading, so the list is filtered and alphabetical instead.

**Unknown is a real value.** Every attribute can be unknown, and unknown is never
stored or displayed as zero. Companies with an unknown size stay in the list —
if missing data excluded them, you'd be looking at the companies we have data on
rather than the companies that fit.

Directory entries live in their own table, so the queue, the Buddy Score and
every report are structurally incapable of seeing them. A company gets into the
pipeline by firing a signal, and by nothing else.

```bash
./run.sh directory-refresh              # walk both registers (slow — minutes)
./run.sh directory-refresh --dry-run    # preview, write nothing
./run.sh directory-refresh --keyword "lateral flow"   # one keyword only
```

**How it finds IVD companies at all.** EUDAMED has no working country, category
or legislation filter — but it does filter by risk class, and IVDR devices are
classified A/B/C/D where MDR devices are I/IIa/IIb/III. So asking for classes
B, C and D *is* asking for in-vitro diagnostics. That sweep is what returns
companies; trade-name keyword search barely works, because trade names are brand
names ("ImmunoCAP", "Anti-CCP"), and it's kept only for the labels it adds.
Class A is excluded — buffers, stabilisers and specimen receptacles.

**Size is the weakest attribute, and it usually says "unknown".** The sweep
counts devices it walked past, not a manufacturer's catalogue, so "at least 3"
fits a micro business and it fits bioMérieux equally well. A floor count can
therefore only ever confirm the *top* band; everything below stays unknown
rather than being guessed at. `large` is excluded from `target_size_bands` on
purpose — cold outbound to large corporates without a signal is what went
silent — and `unknown` is included, because otherwise you'd only ever see the
companies we happen to have data on.

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
  detect/                automatic detection: one module per public source
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
