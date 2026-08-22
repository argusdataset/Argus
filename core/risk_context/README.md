# Module 12 — Risk / Context Analysis + Pending Material Events

Itemized risk inputs for one candidate at one point in time. **No score.**
Module 13 combines these into `risk_score`; this module's job is to
produce honest inputs and to be explicit about which of them it could not
measure.

## What it produces

`assess_risk_context(connection, security_id, as_of=..., features=...)`
returns a `RiskContext` with:

| Field | Contents |
|---|---|
| `flags` | Three named `RiskFlag`s: `liquidity_degree`, `volatility_spike`, `event_proximity` |
| `events` | `PendingEventsView` — what was scheduled, and whether ARGUS could see |
| `invalidation` | Backward-transition facts and eligibility trend |
| `config_version` | The threshold set that produced these verdicts |
| `missing_inputs()` | Every input that could not be read, and why |

Each `RiskFlag` is `raised: True | False | None`. **`None` is a first-class
answer**: it means the input was not measurable, and it is never
collapsed into `False`. `degree` carries the continuous reading the
threshold was applied to — in that input's own units, deliberately not
comparable across flags.

## The three flags

**`liquidity_degree`** — Module 09 already decided pass/fail at a $50,000
execution floor. This measures *degree* above that floor, because a name
at $60,000 a day and one at $5,000,000 both cleared the gate and are not
the same position to exit. Runs 0 (comfortable) to 1 (no volume).

**`volatility_spike`** — requires **both** an extreme ATR percentile
(against the security's own history) **and** volatility expansion above a
multiple. The pairing is the point: extreme-but-stable is a
characteristic, and expanding-from-quiet is the setup ARGUS is looking
for. Only both together suggest something changed since the features
described a quiet base.

**`event_proximity`** — days until the soonest scheduled binary event.
Not a judgement that earnings are bad; a marker that a structurally-driven
thesis and speculative anticipation of a binary outcome are different
trades that look identical on a chart.

## Pending material events

`event_type` is free text, per Module 03's schema — litigation, M&A,
trial results and patent decisions are addable without a migration. This
module reserves those names as Python constants (`RESERVED_EVENT_TYPES`)
so a future source does not coin a competing spelling, and sources only
`EARNINGS` (`SOURCED_EVENT_TYPES`). There is an integration test that
stores and queries an unsourced type end to end.

### The PIT decision, and its cost

FMP's earnings calendar reports *when a release is scheduled*, never
*when that schedule was announced*. Only the second governs leakage. This
module refuses to invent it: `observation_time` is the fetch timestamp,
and `availability_time` is derived from it with this module's own
`ingestion_lag`.

**Consequence, stated plainly: an earnings calendar backfilled today
contributes nothing to a historical replay.** A query as of 2015 sees no
events and reports that as *data unavailable*, never as *no event
scheduled*. Event-proximity risk becomes meaningful only from the point
ARGUS starts fetching calendars forward. Both facts have tests.

### Re-observation is not restatement

Every calendar fetch spanning an event writes another row for it, with a
later `observation_time` (Module 03's unique constraint includes it, so
they coexist). They are not restatements: knowledge is not un-learned. The
query collapses them on the **earliest** availability, which is why this
module does not call `select_latest_as_of` — that helper implements the
restatement rule, which is right for a revised fundamentals figure and
wrong here.

## Invalidation

Two signals, surfaced from Modules 09 and 10 rather than recomputed:

- **Backward transitions** — `backward_transition_count()` reads the flag
  Module 10 stored at write time. This module does not re-derive
  direction; Module 10's report was explicit that storing it prevents a
  future reordering of `CYCLE_ORDER` from retroactively changing what a
  historical row meant.
- **Lost eligibility** — `eligibility_history()` reads
  `eligibility_check_results` across runs. Module 09 stores passes as
  well as failures precisely so "evaluated and passed" stays
  distinguishable from "never evaluated"; that decision is what makes
  this signal computable, and this module is its first consumer. Module
  09 has no reader; this is one, and it re-runs no gate.

`EligibilityTrend` separates `LOST_ELIGIBILITY` (passed before, fails
now — actionable) from `NEVER_ELIGIBLE` (never valid, so nothing was
invalidated). `signals_raised()` returns names, never a blended
invalidation number.

## What is deliberately not here

| Left out | Why |
|---|---|
| Any aggregate risk number | Module 13's job. Enforced by `tests/unit/risk_context/test_no_aggregation.py`, which scans the result objects' own surfaces. |
| Module 11 similarity evidence | Already computed properly, with its own sufficiency state and intervals. Module 13 reads it directly. Until Module 17 runs it is empty, and an empty pass-through channel looks like evidence of no risk. |
| Module 10's `confidence` | An unvalidated pattern-match score, per Module 10's own warning. State and transition **facts** only. |
| Bankruptcy / going-concern | Module 09's hard gate, already applied. Re-deriving it would put one judgement in two places. |
| Non-earnings catalyst sourcing | No data source exists. The names are reserved; nothing is fetched. |
| Sector/market regime risk, correlation, position sizing, drawdown modelling | Not in the brief. This module is thin on purpose. |
| Persisted `RiskContext` rows | Module 03 defines no table for them, and inventing one is not this module's call. Only `pending_material_events` is written. |

## Thresholds

Seven, all in `config.py`, all kind-tagged, six of them `calibratable`
(invented magnitudes) and one `structural`. Every result carries
`calibration_status: "UNVALIDATED_PLACEHOLDERS"`, matching Modules 10 and
11 — **none of these numbers has met outcome data.** A source scan
(`test_no_numeric_literals_in_risk_logic`) fails on any numeric literal
in the logic modules that is not structural.

`min_inputs_for_assessment` is zero deliberately: this module never
refuses to produce a result. It reports what it could and could not
measure and lets Module 13 decide — a refusal would hide the partial
information that is the module's entire output.

## Cost

Per candidate, not per universe: four queries (events, transition
history, backward count, eligibility history). Module 09 has already
reduced ten thousand securities to a candidate list by the time anything
calls this. If a caller ever needs it universe-wide, that is the point to
batch it — not before.
