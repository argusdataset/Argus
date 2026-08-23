# Module 13 — Scoring Engine

Combines Modules 08, 10, 11 and 12 into five numbers — or, honestly, into
none. Today it produces none for essentially every candidate, and that is
the correct answer.

## The five numbers

| Number | From | Today |
|---|---|---|
| `argus_score` | Weighted sum of seven components | Rarely produced |
| `confidence` | Evidence reliability, from inputs no component reads | Rarely produced |
| `opportunity_score` | Module 11's cross-asset MFE distribution | Rarely produced |
| `risk_score` | Module 12's itemized inputs, each normalized | Rarely produced |
| `probability` | A calibrated model | **Always `None`** |

**`argus_score` is never a probability.** An 87 does not mean an 87% chance
of anything. `probability` is the only number in ARGUS that could ever be
one, it is `None`, and it stays `None` until a model is calibrated against
real outcomes after Module 17. Its *definition* is stored with every
signal anyway, so the structure is present and the absence is unambiguous.

## The open 10%, and how it was closed

The brief's seven components sum to 90% and it asked for the remaining 10%
to be assigned by a named choice. **Proportional redistribution**, executed
by `_share()` in `config.py` rather than written out as seven
hand-computed constants.

Proportional scaling is the only redistribution that leaves every pairwise
ratio between components unchanged, so it adds no information the brief
did not already contain — which is the most that can honestly be claimed
for numbers nothing has validated. There is a test asserting that property
directly. Rounding the results to tidy percentages would be a second
invented decision, so they are left exact:

```
pattern_quality      0.2778   historical_evidence  0.2222
market_regime        0.1667   volume_liquidity     0.1111
volatility_structure 0.1111   fundamental_context  0.0
risk_reward          0.1111
```

**The eighth-component alternative was considered and rejected on two
specific grounds.** The obvious candidate was a *prior decline* component
— nothing in the seven visibly measures the decline the setup is defined
as starting from. But target-model-v1 already scores `prior_decline`
(`peak_to_trough_decline`) and `stabilization`
(`downside_momentum_reduction`, `volatility_contraction_onset`) inside its
own quality, so such a component would double-count Pattern Quality rather
than fill a gap. And Module 03's `signals` table has exactly seven
`component_*` columns, so an eighth could not be stored — the stored
breakdown would stop reconstructing the score, breaking the guarantee that
every score is explainable from its named components. There is an
integration test asserting the breakdown does reconstruct it.

## `confidence` is not a restatement of the score

The two must be able to diverge sharply, and the only way to guarantee
that is to compute them from different inputs. `confidence.py` reads no
component value, no `argus_score`, no pattern quality, and not Module 10's
state confidence. Its five factors are sample sufficiency, interval width,
feature coverage, weight coverage, and event clarity.

A worked case from the test suite: perfect pattern match, excellent
structure, six historical analogues with an interval a third of the range
wide, a 60%-filled feature window, earnings in two days →
**`argus_score` 87.7, `confidence` 33.7.** Both are right, and a reader
who saw only the first would be misled.

Event clarity is Module 12's finding #4 implemented: an imminent binary
event does not make the base worse, it makes the thesis less structurally
determined, which is a statement about how much the reading is worth.

## The interval travels with the number

The Historical Evidence component is scored from the **upper bound** of
Module 11's Wilson interval on the failure rate, not from the point
estimate. A 0.6 failure rate from 6 cases and from 200 cases are the same
point estimate; the first one's pessimistic bound is far worse, so it
scores far worse. The interval also enters `confidence` separately through
its width. Both have tests, and swapping the bound for the point estimate
breaks three of them.

## Three refusals, and only one of them writes a row

| Decision | Row written | Meaning |
|---|---|---|
| `SCORED` | Yes, all five columns | Honest numbers |
| `INSUFFICIENT_EVIDENCE` | Yes, all NULL | ARGUS could not judge |
| `GATED_INELIGIBLE` | **No** | Module 09's gates said no |
| `GATED_LOST_ELIGIBILITY` | **No** | The thesis has broken; Module 14's territory |

`LOST_ELIGIBILITY` gates rather than weights, per Module 12's finding #3:
folding "this thesis has broken" into a continuous score as one input
among several would let a strong reading elsewhere numerically outweigh
it. And it writes nothing, because an `INSUFFICIENT_EVIDENCE` row would
claim ARGUS could not gather evidence when in fact it gathered evidence
and the evidence says the setup is over.

Any **undetermined** risk input refuses the whole candidate. Module 12
reports an unmeasured risk flag as unmeasured, never as safe, and handing
such a candidate a middling `risk_score` would let something ARGUS knows
nothing about outrank something measured and clean. Module 03's CHECK
constraint requires a `SCORED` row to carry a `risk_score`, so this is the
schema's rule as much as the module's.

## Why almost nothing is scored today

`min_weight_coverage` is 0.85, above 0.80 deliberately: the
historical-evidence component alone carries 0.2222, so its absence is on
its own enough to refuse a composite. Module 11 returns `INSUFFICIENT` for
every candidate until Module 17's scan populates the case dataset.

A twelve-candidate end-to-end run through Modules 08 → 10 → 11 → 12 → 13
produces **12 `INSUFFICIENT_EVIDENCE`, 0 scored**: coverage 0.778 for the
four candidates target-model-v1 covers (historical evidence the only thing
missing) and 0.500 for the eight in states it does not judge (pattern
quality missing too). This is asserted as the expected state, not worked
around.

## Module 08's duplicate feature

`volatility_contraction_onset` (Group A) and `volatility_compression`
(Group B) are the same arithmetic under two names. Module 11 flagged it.

**Not fixed at source** — a feature rename invalidates every stored
`feature_schema_version`, and that is Module 08's call. **Not silently
double-counted either**: `DUPLICATE_FEATURES` names the suppressed side,
`COMPONENT_FEATURES` declares what each component reads, and
`tests/unit/scoring/test_duplicate_features.py` computes both features
from real bars to prove they are identical, then asserts no component
holds both. Adding the suppressed name back breaks 41 tests.

**A finding this module cannot fix:** target-model-v1 reads
`volatility_contraction_onset` in its `stabilization` sub-component *and*
`volatility_compression` in its `consolidation` sub-component, so one
signal enters its `quality` twice — and that quality is 27.8% of
`argus_score`. That is inside Module 10 and is flagged, with a test
recording the fact.

## Reproducibility

Every signal carries all six lineage IDs, and scoring is a pure function
of `ScoringInputs` and `ScoringConfig` — `score_candidate` performs no I/O
at all. `ScoringConfig.from_definition()` rebuilds the exact configuration
a stored signal cites, including every ramp, so a score written under
today's placeholders stays re-derivable after they have been replaced
twice.

## Fixed in Module 14

- **`signals` uniqueness.** Migration 0005 added the `uq_signals_identity`
  partial unique index on `(security_id, event_time, data_snapshot_id,
  scoring_configuration_id) WHERE supersedes_signal_id IS NULL`.
  `write_signal` now uses `ON CONFLICT DO NOTHING` against it instead of a
  read-then-insert guard, so the protection holds under concurrency and
  corrections still work.
- **`signals.detail`.** Migration 0005 added the JSONB column. It carries
  the raw readings, what each ramp made of them, why a component was
  unmeasured, the weight coverage, the confidence factors, the refusal
  reason, and the calibration status — the "why" the seven columns alone
  could not hold.
- **target-model-v1's double-count.** Module 14 removed the duplicated
  volatility reading from its `stabilization` sub-component. See
  `core/market_state/target_model_matching/models/target_model_v1/model.py`.

## Known gaps, flagged not fixed

- **Module 10 discards its typed `TargetModelAssessment`.** A typed seam
  exists between Module 10 and its model but not between Module 10 and its
  consumers. `upstream.py` reads the assessment back out of the evidence
  JSONB Module 10 publishes, rather than re-running the model or reading
  `StateAssignment.confidence` (which the brief forbids feeding into
  scoring).
