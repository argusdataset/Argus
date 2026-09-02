# Module 15 — Outcome Tracking + CASE Records

Turns a concluded setup into a permanent record of what happened —
including, especially, when ARGUS was wrong. Every statistic the project
will ever report is computed against the dataset this module builds, so a
subtle error here produces a *plausible* number rather than a failure, and
everything downstream inherits it invisibly.

## The success definition

**`+1.5×ATR before -0.75×ATR within 60 trading days`**, measured from the
setup's `activated` event. ATR is the 20-session average true range as of
entry — Module 08's own `true_range` primitive, recomputed from the same
PIT-bounded price panel this module already loads.

That string is deliberately identical to Module 13's
`PROBABILITY_DEFINITION`, and a test asserts it. Module 13 reserves
`probability` for a calibrated model that does not exist yet; when it is
built it will be fitted against the labels this module assigns. If the two
drift, the model would be calibrated to predict something other than what
the dataset records — and nothing would fail, the numbers would simply
mean something nobody intended.

**Volatility-normalized, not a flat percentage.** A flat threshold
applied uniformly is a structural flaw, not a calibration detail: a
low-volatility large-cap and a high-volatility penny stock have
completely different "normal" daily ranges, so a flat percentage
systematically misclassifies outcomes — inflating the win rate for
naturally volatile securities and deflating it for stable ones, before
any real pattern signal is measured. Scaling the target and stop to each
security's own measured volatility at entry fixes that. A security with
too little price history to measure a 20-session ATR at entry gets no
resolved criterion at all (`NO_ATR`) rather than a flat-percentage
fallback, which would silently reintroduce the flaw for exactly the
securities it is riskiest to get wrong.

All the numbers are invented placeholders. Changing any of them relabels
the dataset, so a change becomes a new `data_snapshot` and outcomes
computed under the old criterion stay attributable to it. That is what
`setup_outcomes.data_snapshot_id` (migration 0006) is for.

## Status: criterion first, terminal event second

```
never activated                     → NO_VALID_OUTCOME
criterion resolved in the window    → SUCCESS or FAILED
terminal event was an invalidation  → INVALIDATED
otherwise                           → EXPIRED
```

In every ordinary case this reproduces Module 14's terminal-event mapping
exactly. It diverges only where the literal mapping is wrong: a setup can
reach its ATR-relative target on day thirty and still be closed by
Module 14's expiry or by an administrative invalidation, because Module
14 watches market states and eligibility, not price. Labelling that
`EXPIRED` would put a resolved success into the dataset as an unresolved
non-event and make Module 17's hit rate quietly too low.

**Both thresholds on one bar resolve as `FAILED`.** Daily bars do not
record intrabar order, so the adverse case is assumed — it can only
understate the pattern's performance, and a dataset that flatters the
thing it exists to test is worthless.

## The measurement window

`[activation, min(terminal event, activation + 60 trading days)]`.

Entry is the `activated` event, never the opening: measuring from the
opening would credit a setup for weeks of base-building no position was
taken through, and make MFE incomparable between a setup activated on day
3 and one activated on day 300. A setup that never activated has no entry
and gets no excursion — reported as such, never as an excursion of zero.

The end is the earlier of the two bounds. Past the terminal event would
attribute price action to a setup ARGUS had stopped tracking; past the
horizon would let "within 60 trading days" become "eventually". The
horizon walks Module 07's market calendar rather than multiplying by a
calendar-day ratio.

The entry bar's own high and low are excluded — they happened around the
moment of entry.

## PIT correctness

Prices come through Module 08's `load_panel`, which applies
`availability_time <= as_of` to the bars *and* to the corporate actions
adjusting them. Both halves fail silently if got wrong:

- An unadjusted series turns a 2-for-1 split into a −50% single-bar
  excursion, recording a successful setup as a catastrophic failure.
- A split filed *after* the computation date, applied anyway, rescales the
  whole window and changes MFE by the split ratio.

`tests/integration/outcome_tracking/test_pit_leakage.py` constructs both,
plus a bar revision filed later, and proves this module's `as_of` reaches
the loader. Four break attempts against it are recorded in the module
report.

## Failures are as complete as successes

By construction, not convention: the assembly branches on `OutcomeStatus`
nowhere. Two tests enforce it — one comparing the assembled records' field
structure, one comparing which columns are populated in the stored rows
(with `false_positive_type` excluded, since its asymmetry is the taxonomy
working). Adding a status branch to the assembly breaks both.

## The A–G taxonomy, and what it is worth

Assigned to every non-`SUCCESS` outcome, including `EXPIRED` and
`INVALIDATED` — "nothing happened" and "we stopped watching" are
informative negatives too, and classifying only the `FAILED` ones would
leave Module 17 blind to the most common way this pattern disappoints.

| | Meaning | Evidence | Confidence |
|---|---|---|---|
| A | No real pattern | Never activated | `inferred` |
| B | Pattern, no expansion | MFE under 0.45x the entry ATR | `weak` |
| C | False breakout | MFE over that floor, then failed | `inferred` |
| D | Breakdown | Realized return past -2.25x the entry ATR | `inferred` |
| E | Catalyst-driven | Scheduled binary event within days of the peak excursion | `coincident` |
| F | Illiquid distortion | Dollar volume under the floor at detection | `inferred` |
| G | Corporate-action distortion | Action effective inside the window | `coincident` |

**Distortions (G, F, E) are checked first**, because each explains away
the structural verdicts: a base that "failed" through a split artefact did
not fail as a pattern, and recording it as a type-D breakdown would teach
Module 17 a lesson about structure from an accounting event.

**The honest limitations.** There is no `certain` confidence value, on
purpose. E and G establish *coincidence*, never cause — an earnings date
near the peak is consistent with the catalyst driving the move and equally
consistent with the pattern working on a day that happened to have
earnings. F is a proxy for a proxy: low dollar volume makes a print less
trustworthy without saying this print was wrong. A, B, C and D are
threshold-separated regions of one continuous space, so a setup just
either side of the expansion floor gets different labels for a difference
the data may not support. The taxonomy is a starting point for human
review, not a finding.

**B, C and D scale with the security's own volatility.** Their floors were
flat percentages — 3% and -15% — until an audit found they carried the
same flaw the success criterion had: a 3% peak is noise for one security
and a real advance for another, so the taxonomy graded "did this move at
all" on a scale that meant something different for every name. They are
now multiples of the entry ATR, resolved per setup against
`Excursion.atr_fraction`.

The multiples are constant-ratio translations of the percentages they
replaced (`0.03/0.10 x 1.5 = 0.45`, `0.15/0.05 x 0.75 = 2.25`), so the
change moved no boundary — only the unit each boundary is written in.
Recalibrating the magnitudes is a separate decision that needs outcome
data. A setup whose entry ATR could not be measured gets **no** type
rather than defaulting to B: without a volatility scale, "went nowhere"
is not a judgement anything supports.

## `review_confidence` policy for automated cases

| Value | When |
|---|---|
| HIGH | The criterion resolved on complete data, no distortion flagged |
| MEDIUM | The criterion did not resolve, or a distortion was flagged |
| LOW | The excursion could not be measured, or inputs were missing |

It describes confidence in the **classification**, not in the setup. It is
an automated placeholder: a reviewer overwrites it during dataset
construction, which is exactly why Module 03 left `setup_outcomes` open to
UPDATE while blocking DELETE. The policy is stated so a reviewer knows
what an unreviewed value means rather than guessing.

## The CASE record is a projection, not a table

Every part of it is already stored — stages in `setup_events`, metrics in
`feature_vectors`, numbers in `setup_outcomes`, context in the canonical
tables, same-asset history in `market_state_transitions`. This module
joins them; it does not copy them. A materialized copy of six tables is a
copy that can disagree with all six.

`setup_outcomes` has no JSONB column, so materializing would have meant
adding one. If Module 16 or 17 needs the record frozen alongside the data
— for an export, or to pin the assembly logic — that is the point to add a
JSONB column or a view, and `CaseRecord.as_dict()` is the shape it should
hold.

## Known gaps, flagged not fixed

- **`setups` does not record its feature schema version.** A case record's
  metric block therefore depends on the caller supplying one; absent it,
  the block is honestly empty rather than looked up against the wrong
  version's vectors. A `feature_schema_version_id` column on `setups`
  would close this.
- **Module 07 has no "latest feature vector" reader.**
  `get_feature_vector_as_of` requires an exact `event_time`. This module
  uses Module 07's `select_latest_as_of` primitive with `event_time`
  precedence — the same arrangement `get_latest_fundamental_as_of` uses —
  rather than hand-rolling SQL, but the reader belongs in Module 07.
- **The `signals` join is positional.** See the module report: joining a
  setup to its score on `(security_id, event_time)` is not a foreign key
  and is not reliable enough to depend on. This module does not use it.
- **Two callers of one PIT rule for pending events.** Module 12's
  `pending_events_as_of` answers "what is coming up"; this module needs
  "what fell inside a closed window". Same `availability_time <= as_of`
  filter, written twice. A shared helper would be better.
