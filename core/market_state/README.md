# Market State Engine

Market State Engine — built in Module 10. Assigns one of nine
`market_state` values per security per `as_of`, records every transition
append-only, and derives the four official watchlists.

```python
from core.market_state import (
    MarketStateConfig,
    classify_states,
    publish_target_model_version,
    record_transitions,
    all_watchlists,
)

config = MarketStateConfig()
version_id = publish_target_model_version(connection, config)

features = compute_features_batch(connection, universe_ids, as_of)  # Module 08
report = evaluate_eligibility(connection, pool, features, universe_id)  # Module 09

result = classify_states(
    features, eligibility=report, config=config, target_model_version_id=version_id
)
record_transitions(connection, result)

lists = all_watchlists(connection)  # DOWN_TREND / CONSOLIDATION / BREAKOUT_READY / UPTREND
```

## Read this first: what is solid and what is a guess

This is the first ARGUS module without a checkable correctness criterion.
"Is `BASE_FORMING` correctly distinguished from `CONSOLIDATION`" is a
modeling choice, not a fact, and **no threshold in this module has been
statistically validated.**

The module is therefore built along a deliberate split:

| | Status |
|---|---|
| State machine, transition log, evidence floor, watchlist derivation, batch interface | Engineering. Tested to the same standard as Modules 03-09. |
| Every numeric threshold | Unvalidated placeholder. Isolated so recalibration is a data change. |

`target_model_version.definition` records `calibration_status:
"UNVALIDATED_PLACEHOLDERS"` in the database itself, and every stored
state's `evidence` carries the same marker — so no downstream reader can
mistake these for measured values.

## Threshold isolation, and how recalibration would work

Every number lives in `thresholds.py` (engine) or
`target_model_matching/models/target_model_v1/thresholds.py` (model).
**No numeric literal appears in any classification logic**, enforced by
`tests/unit/market_state/test_threshold_isolation.py`, which parses the
source of `states.py`, `classifier.py` and `model.py` and fails on any
non-structural constant.

Each threshold carries its value, a `kind`, and its reasoning:

- **`directional`** — a definitional boundary (a sign change, a ratio
  crossing 1.0). Comparatively safe.
- **`magnitude`** — an invented number. 13 of the 19 engine thresholds
  are these, and recalibration starts here.

A real recalibration, concretely:

1. `StateThresholds.from_definition(row.definition)` — reconstruct the
   exact values a historical state was produced under.
2. Change the numbers. Nothing else.
3. `publish_target_model_version()` — a new checksum means a new
   immutable row; everything recorded under the old values stays
   attributable to them.
4. Re-run the replay. `as_of` is a plain argument, so the call that
   classifies today reclassifies 2015.

`StateThresholds.magnitudes()` hands a tuning tool exactly the numbers
worth moving, without it knowing anything about the state machine.

## The nine states

Precedence runs **most-advanced first, first match wins**. That ordering
does real work: a security that has just broken out still satisfies "range
is tight" for a while, because the range measurement lags. Testing
`CONSOLIDATION` first would pin it there and the breakout would never be
seen.

`DISTRIBUTION` sits above `UPTREND` because it qualifies having been in an
uptrend rather than being a stage beyond it.

`UNCLASSIFIED` is the honest refusal, and it has **two distinct reasons**
that must not be conflated:

- `insufficient_features_for_any_state` / `ineligible:<gates>` — a data
  problem, and the same refusal Module 09 makes.
- `no_state_predicate_matched` — evidence was fine and no state claimed
  the security. That is a **gap in the state machine**, and naming it
  separately is the only way such a gap is ever visible. One was found
  this way during development (see below).

## The four watchlists

```
DOWN TREND      = state IN (DOWN_TREND, BASE_FORMING)
CONSOLIDATION   = state IN (CONSOLIDATION, ACCUMULATION)
BREAKOUT READY  = state IN (BREAKOUT_WATCH, BREAKOUT_READY)
UPTREND         = state IN (UPTREND)
```

Queries over `market_state`, never stored. `DISTRIBUTION` and
`UNCLASSIFIED` appear on none of them. The four are disjoint and cover
exactly the seven public states — both asserted.

`UPTREND` is the newest of the four: a security whose breakout confirmed
used to leave every watchlist, hiding ARGUS's clearest evidence of working
correctly — a candidate it called before the move, now visibly moving. It
is a live view like the other three, not a permanent record: a security
that reverses back out leaves this list too, and its full history stays
queryable through `market_state_transitions` regardless.

## Transitions

`market_state_transitions` is the authority; `market_state` is a
projection of "where is this security now". The projection is rebuildable
from the log, not the reverse.

**Backward transitions are first-class.** A failed breakout returns a
security from `BREAKOUT_READY` to `CONSOLIDATION`, and that is information
— a name on its third attempt differs from one that cleared the level
first try. `cycle_count()` and `backward_transition_count()` answer that,
bounded by `as_of`, and exist now because Module 11 will need them.

Two details worth knowing:

- **Only changes are recorded.** Re-running a scan writes nothing.
  Otherwise `cycle_count()` would count scans rather than cycles.
- **Direction is stored, not derived on read.** A future reordering of
  `CYCLE_ORDER` must not retroactively change what a historical row meant.

## target-model-v1

Contained, not a peer. The engine assigns states; the model scores
*quality of match* within four of them (`CONSOLIDATION` → `ACCUMULATION` →
`BREAKOUT_WATCH` → `BREAKOUT_READY`) and that score becomes the state's
`confidence`. It cannot veto a state assignment.

**The brief is genuinely ambiguous here and this module does not resolve
it unilaterally** — see `target_model_matching/interface.py` and the
module report. The model exposes `supports_advancement`, which is recorded
as evidence and deliberately not acted on; making the model gate
transitions would be a one-place change in `_resolve_states`.

Four equally-weighted components, saturating rather than stepping. Equal
weights are the honest prior: unequal ones would imply someone knows which
phase matters more.

## Measured state sequence

The shared Module 08 lifecycle fixture, classified at each phase end:

| Phase | State | Watchlist | Confidence |
|---|---|---|---|
| prior_advance | `DISTRIBUTION` | — | — |
| decline | `DOWN_TREND` | DOWN TREND | — |
| stabilization | `CONSOLIDATION` | CONSOLIDATION | 0.38 |
| consolidation | `ACCUMULATION` | CONSOLIDATION | 0.43 |
| awakening | `BREAKOUT_WATCH` | BREAKOUT READY | 0.45 |
| confirmation | `UPTREND` | UPTREND | — |

Monotone forward progression through the cycle. `BASE_FORMING` is skipped
— by the stabilization phase the synthetic security is already quiet
enough to read as a consolidation. That is a calibration observation, not
a structural fault, and exactly the kind of thing validation would settle.

Tests assert *ordering along the cycle*, never which specific state a
phase lands in, because only the ordering is a claim the current
thresholds support.

## Two bugs found while building this

- **The `CONSOLIDATION` predicate tested the wrong concept.** It required
  `volatility_compression <= 0.85` — active compression — but a mature
  base sits near 1.0 by construction, since both measurement windows are
  inside the quiet stretch. A fully-formed consolidation matched no state
  at all. Now an upper bound ("not expanding"), with the quiet *level*
  tested by `atr_percentile`, which discriminates cleanly across the
  fixture (1.00 → 0.77 → 0.08 → 0.004 → 0.37 → 0.63).
- **`is_backward` treated `UNCLASSIFIED` as position −1**, so a security
  losing eligibility recorded as a failed breakout. Now states outside
  `CYCLE_ORDER` are neither forward nor backward.

## Boundaries

Does **not**: compute historical similarity (Module 11), do risk or
pending-event analysis (Module 12), score anything (Module 13), or make
live FMP calls. Classification performs no I/O — it takes Module 08's
feature vectors and returns states.
