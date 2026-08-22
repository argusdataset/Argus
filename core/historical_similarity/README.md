# Historical Similarity

Historical Similarity Engine — built in Module 11. Answers one structured
question for one candidate: *how many historically similar setups exist,
and what actually happened to them?*

```python
from core.historical_similarity import (
    SimilarityConfig,
    find_similar_setups,
    publish_similarity_configuration,
    write_similarity_results,
)

snapshot_id = publish_similarity_configuration(connection, as_of=as_of)
evidence = find_similar_setups(
    connection,
    security_id,
    feature_vector.features,
    as_of,
    feature_schema_version_id,
    data_snapshot_id=snapshot_id,
)
write_similarity_results(connection, evidence)  # two rows, always

evidence.cross_asset.statistics.failure_rate  # other companies
evidence.same_asset.statistics.failure_rate  # this company's own history
```

Not a general-purpose nearest-neighbour search, and **not a score**. It
returns evidence; Module 13 decides what to do with it.

## Read this first: the dataset is nearly empty

Module 17's full historical scan has not run. Until it does there are
approximately zero cases, every query returns `INSUFFICIENT`, and the
Module 09 counter returns 0 for every security. That is correct
behaviour, not degraded behaviour — and it has one operational
consequence worth stating loudly:

> **Swapping this counter into Module 09's live gate before Module 17
> runs would stop the pipeline**, because any positive threshold gates
> the entire universe to `INSUFFICIENT_EVIDENCE`. Keep Module 09 on its
> placeholder until the case dataset exists.

Every threshold here is an unvalidated placeholder, and every stored
result carries `calibration_status: "UNVALIDATED_PLACEHOLDERS"` — the
same discipline Module 10 established.

## The distance metric

**Mean Euclidean distance over IQR-scaled features**, averaged over the
features two vectors share:

```
distance(c, h) = sqrt( (1/|S|) * Σ_{f ∈ S} ((c_f - h_f) / scale_f)² )
```

| Rejected | Why |
|---|---|
| Learned embedding | Ruled out by MVP scope; "why is this similar" must name features |
| Mahalanobis | Needs a covariance estimate from far more cases than features. With 41 features and a near-empty dataset it would be numerically unstable and confidently wrong — and its per-feature contributions are not decomposable |
| Raw Euclidean | Feature scales differ by orders of magnitude; count features would dominate |
| **MAD scaling** | **Tried first and wrong** — see below |

**Explainability** is arithmetic, not gloss: `explain()` returns
per-feature shares that sum to exactly 1.0. The decomposition *is* the
distance, re-expressed.

### Two properties found by testing

**MAD is blind to bimodality.** Median absolute deviation is robust to
outliers but measures the spread of the *majority cluster*. A real case
set is multi-modal — it contains different setup archetypes — and with
three base-shaped and four momentum-shaped cases every MAD collapsed to
within-cluster noise (0.03) while the between-shape gap was a hundred
times larger. Distances inflated to ~900 against a radius of 1.25.
Switched to IQR, which spans the 25th–75th percentile and so contains a
genuine population split.

**Averaging dilutes.** Taking the mean over shared features is what makes
pairs of differing overlap comparable, but it means sharp disagreement on
a few features is diluted by agreement on many. The metric measures
*overall structural resemblance*, not disagreement on any particular
axis. A consumer needing "close on these specific features" should read
the per-feature contributions rather than threshold the distance.

Both are consequences of relative scaling on a small dataset, and both
mean **an absolute distance is only interpretable against the population
it was scaled by.**

## The duplicate feature

Module 10 found `volatility_compression` and
`volatility_contraction_onset` are byte-identical computations. Harmless
when features are read individually; not harmless in a Euclidean
distance, where including both doubles that signal's weight silently.

**Handled by keeping one representative per duplicate group**, named in
`DUPLICATE_FEATURE_GROUPS` and recorded in every stored result's
`excluded_features`. Not by halving weights — that works arithmetically
but leaves two entries in every explanation, inviting a reader to treat
one measurement as two corroborating ones.

`test_the_two_features_are_still_identical` is a **tripwire**: it asserts
the duplication *still exists*. If Module 08 is ever corrected the test
fails and says so, because a stale exclusion would go on silently
discarding a real feature.

41 of 50 features participate. The other exclusions: liquidity (a thin
microcap and a mega-cap can form identical bases — and ARGUS exists to
find the former), sector/industry (no data in the schema), and `atr`
(absolute scale; `atr_percentile` carries it scale-free).

## Cross-asset and same-asset

| | Cross-asset | Same-asset |
|---|---|---|
| Question | What happened to other companies with this shape? | What happened when *this* company did it before? |
| Source | `setups` + `setup_outcomes`, excluding this security | This security's own cases, plus Module 10's transition log |
| Storage | `AnalogueScope.CROSS_ASSET` row | `AnalogueScope.SAME_ASSET` row |

**Never merged.** A company that failed this pattern twice is not "due"
for success on the third attempt; it may simply be a company whose bases
fail. `SimilarityEvidence` exposes **no combined count and no blended
statistic** — not because one could not be computed, but because the
moment one exists downstream code will use it.

Same-asset history reads Module 10's transition **facts** (which state,
how many cycles, how many retreats) and deliberately **not** Module 10's
`confidence`, which is a pattern-match score from unvalidated weights.

## Small samples

| State | Count | Statistics |
|---|---|---|
| `INSUFFICIENT` | < 5 | **None at all** |
| `SPARSE` | 5–29 | Reported, flagged, with intervals |
| `ADEQUATE` | ≥ 30 | Reported, flagged, with intervals |

Two analogues at +40% and −10% give a median of +15%. Arithmetically
correct, and worthless. `INSUFFICIENT` returns `None` for every statistic
— deliberate friction, so a downstream module handles the absence rather
than reading a two-sample median as evidence.

The count is always reported, because that is a fact about the search.
Only the inferences are suppressed.

Proportions use **Wilson intervals**, which stay inside [0, 1] at small N
and extreme rates — the region a failure rate lives in. Regime splits
re-apply the floor, since splitting a small sample makes every bucket
smaller.

`EXPIRED` is not a failure: it did not conclude. Counting indecision as
loss would systematically overstate failure.

## Module 09's counter

`HistoricalAnalogueCounter` implements Module 09's Protocol. No Module 09
code changed — the swap tests call Module 09's own `evaluate_eligibility`
and assert on its own gate results.

**Cross-asset only**: the gate asks whether enough comparable past
instances exist for a statistical claim, and a security's own attempts are
not a cross-sectional base for that.

### `min_historical_analogues` re-derived: 5 → 15

| | Placeholder | This module |
|---|---|---|
| Counts | Peers in a bucket, today | Concluded setups within a radius |
| Typical | Tens to hundreds | **Zero**, until Module 17 |
| Grows with | Universe size | Accumulated history |

Not a rescaling — a category change. 15 comes from Wilson interval
widths: at n=5 a proportion's interval spans ±0.4 and says nothing; at
n=15 it is ±0.25; at n=30, ±0.18. It is **arithmetic about intervals, not
a finding about markets**, and cannot be validated until there is a
dataset to validate against.

## Boundaries

Does **not**: analyse risk or pending events (Module 12), score anything
(Module 13), make live FMP calls, or use learned embeddings. Does not fix
Module 08's duplicate at source — flagged as a separate decision. Does
not modify Module 09 beyond implementing its Protocol.
