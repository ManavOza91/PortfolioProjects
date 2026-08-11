# Scoring rules

Everything here is stack-agnostic. Numbers come from `scoring-parameters.json`;
signal weights come from `taxonomy.json`. Verify your implementation against
`test-vectors.json`.

Reference implementation: `signal_engine/scoring.py`.

---

## The thesis, in one rule

**A company enters the pipeline because a signal fired.** Nothing else creates a
score. A company on a target list with no signal has a score of zero and must not
appear in the queue. Getting this wrong is the difference between this tool and the
one it replaces.

---

## 1. Effective weight of one signal

```
effective_weight = base_weight × decay_factor
```

`base_weight` is snapshotted onto the signal record when it is created, copied
from its type in the taxonomy. It is **not** re-read from the taxonomy at scoring
time — so changing a weight affects future signals and does not silently restate
what a company was worth last year.

### decay_factor

```
if not signal_type.decays:
    return 1.0                          # structural — never decays, never expires

age_days = max(0, as_of − detected_date)
quarters = age_days ÷ days_per_quarter            # days_per_quarter = 91.31
factor   = rate_per_quarter ^ quarters            # rate_per_quarter = 0.90
factor   = max(factor, floor)                     # floor = 0.40

if expiry_date is set and as_of > expiry_date and expiry_drops_to_floor:
    factor = floor

return factor
```

Three things that are deliberate and easy to get wrong:

**A signal never reaches zero.** The floor is 40% of the original weight. Capital
equipment cycles run six months to two years. A company lost to a competitor in
2024 re-engaged in 2026 wanting a second unit; another raised funding in 2024 and
bought in 2026. Standard sales-tool decay — where a six-month-old signal is dead —
is simply wrong for this market. At six months a signal is still worth 81%.

**Structural signals return 1.0 unconditionally.** A disclosed multi-product
pipeline does not stop being multi-product. `decays: false` in the taxonomy. Note
this beats expiry too: a structural signal with an expiry date still returns 1.0.

**Expiry drops to the floor, not to zero.** Expiry means "this needs
re-confirming", not "this never happened".

Optional: if `confidence_scales_weight` is true, multiply by the signal's
confidence as well. Off by default, so a signal that was once uncertain and has
since been confirmed is not penalised twice — confidence already gates whether a
signal scores at all (§4).

---

## 2. Compounding

Two independent signals close together mean more than their sum. Observed pattern:
regulatory submission → funding → authorisation inside seven months at one company.

```
if fewer than 2 contributions: multiplier = 1.0

for each contribution A:
    window = all contributions B where |B.detected_date − A.detected_date| ≤ window_days
    if require_distinct_types:
        count = number of DISTINCT type_keys in window
    else:
        count = number of contributions in window
    track the maximum count seen

if max_count < 2: multiplier = 1.0
else if stack:    multiplier = base_multiplier ^ (max_count − 1)
else:             multiplier = base_multiplier
```

Defaults: `window_days = 90`, `multiplier = 1.30`, `require_distinct_types = true`,
`stack = false`.

**Independence means a different signal type.** Two job postings are one story. A
regulatory submission plus a funding round are two. This is the whole point of the
`require_distinct_types` flag — turning it off makes the score reward repetition.

The window is a sliding comparison against each signal in turn, not a fixed
calendar window. Three signals at days 0, 80 and 160 compound (0 and 80 are within
90 of each other), even though 0 and 160 are not.

---

## 3. Company score and tier

```
raw_sum = Σ effective_weight  over signals with status = 'scored'
score   = raw_sum × compounding_multiplier
tier    = first tier (sorted by min_score, highest first) where score ≥ min_score
```

Only `status = 'scored'` signals contribute. See §4.

### Tier calibration — a constraint, not a preference

Tiers are configurable, but one relationship must hold:

> **The Tier B threshold must be at or below the heaviest `base_weight` in the taxonomy.**

The Buddy Score's signal-quality component only counts outreach to Tier A/B
companies. If Tier B sits above the heaviest single signal, then no single signal
can ever reach Tier B — so acting on the strongest available evidence would score
as low-quality outreach. Exactly backwards, and an easy mistake to make by editing
weights and thresholds separately.

Current: heaviest weight 30, Tier B at 25. A fresh top-weight signal lands in B on
its own. `tests/test_scoring.py::test_the_heaviest_signal_alone_reaches_quality_outreach`
enforces this.

### Derived product fit

Sum effective weights per product; a signal with fit `both` adds to both totals.
If the weaker total is ≥ 75% of the stronger, the company is `both`; otherwise it
takes the stronger one.

---

## 4. Signal status — what scores and what does not

| status | contributes | when |
|---|---|---|
| `scored` | yes | confidence ≥ the relevant threshold and a known signal type |
| `review` | no | detected, but confidence below threshold — held for human confirmation |
| `unscored` | no | stored, but nothing scoreable found in it |
| `rejected` | no | reviewed and dismissed |

```
if source == 'auto': threshold = auto_review_threshold      # 0.70
else:                threshold = manual_review_threshold    # 0.45

status = 'scored' if confidence ≥ threshold else 'review'
if the signal has no recognised type_key: status = 'unscored'
```

Automatic detections face a much higher bar than things a human saw first-hand.
Ten confirmed signals beat a hundred uncertain ones.

**A note with nothing scoreable in it is stored with `status = 'unscored'`, not
given a fabricated number.** The raw text is always kept — including when parsing
fails entirely — because a human observation is the highest-converting signal in
this system and must never be lost to a network error.

---

## 5. Rules that override intuition

These exist because the previous system got them wrong and it cost real pipeline.

**No category exclusions, anywhere.** Never filter a company out by market segment,
size, geography or field. The old system excluded "veterinary" and would have
blocked a veterinary diagnostics company that actually engaged. Entry is earned by
evidence, never denied by category. There is no exclusion list in the reference
implementation and there should not be one in yours.

**Funding is a qualifier, not a trigger.** Low weight (15). It indicates
affordability and scaling intent, nothing more. None of the eleven companies that
engaged bought because they had just raised. Never surface a company on funding
alone — the weight is set so it cannot reach the top tier by itself.

**The highest-converting signals are human-observed and appear in no database.**
Manual/DIY production at capacity, competitor dissatisfaction, format shifts. Four
of eleven engaged companies were doing manual production and had hit a ceiling; one
had built their own rig. If your build treats manual entry as a secondary path
bolted onto automated detection, it will miss the best signals it has.

**Scores are derived, not stored.** Cache them for sorting, but recomputation from
the signal list must be idempotent and must be re-run regularly — decay means
yesterday's score is already slightly wrong.

**History is not rewritten.** Record the company's tier and the governing signal
*at the time of contact* on every activity row. Without that, rescoring six months
later retroactively decides whether outreach you already sent was signal-driven,
and the signal-to-meeting conversion report becomes fiction.
