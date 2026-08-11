# Buddy Score

A leading indicator of pipeline health, 0–100. Pipeline value is lagging;
outbound behaviour is leading, so this scores the behaviour.

Parameters in `scoring-parameters.json` → `buddy_score`.
Reference implementation: `signal_engine/buddy.py`.

---

## Components

| Component | Points | Measures |
|---|---|---|
| Progression | 30 | replies → conversations → meetings booked |
| Signal quality | 25 | % of outreach to Tier A/B signal companies |
| Volume | 25 | touches against your own target |
| Consistency | 20 | active days, no gaps — steady beats bursts |

Weights must sum to 100. Validate this at startup rather than discovering it in a
report.

---

## Inputs

For a period `[start, end]`, take all activity rows in range. Then:

```
is_outbound = direction == 'out' AND type IN ('email','call','linkedin','meeting')

touches        = count of is_outbound
active_days    = count of DISTINCT date among is_outbound
quality_touches= count of is_outbound where company_tier_at_time_of_contact ∈ quality_tiers

replies        = count where type == 'reply' OR direction == 'in'
                       OR outcome ∈ {reply, replied, responded}
conversations  = count where outcome ∈ {conversation, call_held, discussion}
meetings       = count where type == 'meeting'
                       OR outcome ∈ {meeting_booked, meeting, demo_booked}
```

Replies, conversations and meetings are **not** restricted to outbound rows — an
inbound reply is the outcome you are measuring.

### Tier is read from the activity row, not from the company

`company_tier_at_time_of_contact` is stamped when the touch is logged. Do not join
to the company's current tier. Rescoring must not retroactively change whether last
month's outreach counted as quality.

---

## Period scaling

```
days_in_period    = (end − start) + 1
weeks             = days_in_period ÷ 7
touch_target      = weekly_touch_target      × weeks     # 15/week
active_day_target = weekly_active_day_target × weeks     # 4/week
```

Weekly is the base grain. Monthly, quarterly and annual are computed directly over
the longer period with scaled targets — not averaged from weekly snapshots, which
would mishandle partial weeks.

Week starts **Monday**. Quarters are calendar quarters.

---

## The four formulas

```
volume        = 25 × min(1, touches ÷ touch_target)

consistency   = 20 × min(1, active_days ÷ active_day_target)

signal_quality= 25 × (quality_touches ÷ touches)          if touches > 0 else 0

progression:
  if touches == 0: 0
  else:
    reply_ratio = min(1, (replies       ÷ touches) ÷ target_reply_rate)         # 0.15
    conv_ratio  = min(1, (conversations ÷ touches) ÷ target_conversation_rate)  # 0.08
    meet_ratio  = min(1, (meetings      ÷ touches) ÷ target_meeting_rate)       # 0.05
    blended     = 0.30×reply_ratio + 0.25×conv_ratio + 0.45×meet_ratio
    progression = 30 × blended

score = progression + signal_quality + volume + consistency
```

Every component is capped, so the total cannot exceed 100.

**Meetings are weighted heaviest inside progression (0.45) and are the terminal
metric — not orders.** Cycles run six months to two years, far too long for orders
to be usable feedback.

---

## The invariant you must not break

> **40 touches to unsignalled companies must score worse than 15 touches to
> signalled ones.**

If it doesn't, the score trains the seller back into the spray model it exists to
replace — the one that produced 400–500 leads concentrated in 28 companies, most of
them irrelevant.

Work it through with the defaults:

| | 40 unsignalled | 15 signalled |
|---|---|---|
| Volume | 25 (capped) | 25 |
| Consistency | ≤20 | ≤20 |
| Signal quality | **0** | **25** |
| Progression | low — no signal, few replies | higher |
| **Total** | **≈45** | **≈70** |

Signal quality carries 25 points and is the swing. If you reweight the components,
re-check this case. `tests/test_buddy.py::test_spray_scores_worse_than_targeted`
asserts it directly, and it is the single most important test in the suite.

A deliberately low volume target (15/week) is part of the same design: this system
trades touch count for research depth. Finding the right person at each company is
a manual step, and it is the step that produces the best outreach.

---

## Trend

Compare the current period's score against the mean of the previous
`trend_lookback_periods` (default 4), skipping periods that scored zero.

```
if no prior non-zero periods: direction = 'new'
delta = current − baseline
direction = 'up'   if delta >  2
            'down' if delta < −2
            'flat' otherwise
```

The ±2 dead band stops normal week-to-week noise reading as a trend.
