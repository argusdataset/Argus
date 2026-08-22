# Candidate Detection

Candidate Detection and Eligibility Gating — built in Module 09. Two
genuinely different questions, asked in sequence, kept deliberately apart.

```python
from core.candidate_detection import (
    DetectionConfig,
    detect_candidates,
    evaluate_eligibility,
    new_run_id,
    publish_detection_configuration,
    write_eligibility_results,
)

config = DetectionConfig()
config_id = publish_detection_configuration(connection, config)

features = compute_features_batch(connection, universe_ids, as_of)  # Module 08
pool = detect_candidates(
    features, config=config, detection_configuration_id=config_id, run_id=new_run_id()
)

report = evaluate_eligibility(connection, pool, features, universe_version_id, config=config)
write_eligibility_results(connection, report)

for security_id, outcome in report.eligible().items():
    pass  # may proceed to Module 10. Ineligible ones do not.
```

## The two questions

| | Candidate Detection | Eligibility Filter |
|---|---|---|
| Asks | *Could this contain a setup?* | *Is there enough evidence to judge it at all?* |
| Tuned for | High recall — over-include | Honest refusal |
| Failure output | `ExclusionReason` | `INSUFFICIENT_EVIDENCE`, per gate |
| Not | a score, a classification | a low score |

**Why they stay separate.** Blended into one filter, false-positive
analysis becomes impossible: you can no longer tell a model that misreads
patterns from a model that does not know when it is uninformed. A low
score says "we evaluated this and it is weak". `INSUFFICIENT_EVIDENCE`
says "we did not evaluate this, and here is why".

The visible consequence is that detection deliberately admits securities
it "knows" will fail — an untradeable microcap enters the pool and is
then rejected by the liquidity gate, with a recorded reason. That looks
wasteful and is the point: "rejected for liquidity" is a fact you can
count across runs; "never entered the pool" is invisible.

## Thresholds: which are relative, which are absolute

Module 08's rule — no fixed duration thresholds — carries over, with one
honest refinement.

**Pattern judgments are relative.** "Is this base tight enough to be
interesting" has no absolute answer; it depends on the market and the era.
So the ranking is a *cross-sectional percentile within the batch*, which
is self-calibrating: a volatility-compression reading that means
"unusually quiet" in 2017 means "wildly volatile" in March 2020.

The one hard requirement is a **sign** condition, not a magnitude:
`drawdown_pct < 0`, the security is below its structural peak. Zero is a
definitional boundary, not a tunable constant. `drawdown_pct < -0.30`
would be the forbidden shape.

**Execution feasibility is absolute.** "Can a position be filled" is a
question about dollars, and dollars do not rank cross-sectionally. A
percentile floor would exclude the bottom N% of the universe every day by
construction, however tradeable that slice was. `min_avg_dollar_volume`
is the one absolute magnitude in the module — see below.

## The six gates

| Gate | Rejects when |
|---|---|
| `DATA_HISTORY` | Coverage of the spec's longest window is short |
| `DATA_QUALITY` | Too few features computed, or a critical one absent |
| `LIQUIDITY` | Average dollar volume below the execution floor |
| `BANKRUPTCY_RISK` | Two or more independent distress signals fire |
| `MINIMUM_HISTORICAL_ANALOGUES` | Too few analogues (provisional — see below) |
| `VALID_ASSET_IDENTITY` | Not resolvable through Module 06's universe at `as_of` |

**Every gate runs, always** — no short-circuiting, even after one has
failed. "Failed liquidity" and "failed liquidity, bankruptcy risk and
data history" are different facts, and only the second says the name is
hopeless rather than merely untradeable. `failures_by_gate()` is the
diagnostic this buys.

**Passes are stored too.** Recording only rejections would make
"evaluated and passed" indistinguishable from "never run".

## The liquidity floor: $50,000/day

The gate most likely to silently defeat ARGUS's purpose. The motivating
real examples — MLSS, SLS, HIVE, ALX, QBTS — were several of them thinly
traded during exactly the basing stretches ARGUS would want to have caught
them in. A floor chosen for respectability rather than feasibility deletes
the target population and looks disciplined doing it.

So the floor answers one question only: *is a retail-scale position
possible without the position becoming the tape?* At $50,000 of average
daily dollar volume, a $2,500 position is 5% of a day's turnover — workable
over a session. Below that, even modest size dominates the book.

`test_a_thinly_traded_small_cap_survives_the_whole_pipeline` runs a
~$96,000/day security through the full pipeline on Module 08's own
computed dollar volume. If it ever fails, ARGUS has quietly stopped being
able to find what it was built for.

## The bankruptcy proxy

A company bleeding toward zero produces price action indistinguishable
from a base. That is not bad fundamentals (a legitimate case — hence
`argus_score`'s 0% Fundamental Context weight); it is evidence the
observation is not an instance of the pattern at all. Hence a gate, not a
score input.

Four signals, all PIT-correct via `load_latest_fundamentals_as_of`:
negative shareholders' equity, cash runway under a year while burning,
debt above 90% of assets, and share count more than doubling year over
year.

**Two signals required to exclude, not one.** A pre-revenue biotech burns
cash with a short runway and dilutes heavily — that is a clinical-stage
company operating normally, not a dying one, and those are precisely the
names ARGUS exists to find. Single-signal securities pass with the signal
recorded.

Full limitations — including the delisting-reason gap, the absence of
going-concern audit opinions, and the unverified balance-sheet field
names — are documented at length in `eligibility/bankruptcy.py`.

## The Module 11 seam

`MINIMUM_HISTORICAL_ANALOGUES` needs historical similarity, which is
Module 11's job. Rather than defer the gate or build a similarity engine
here, the module defines an `AnalogueCounter` protocol and ships one
deliberately weak implementation behind it.

**`PeerProfileAnalogueCounter` counts cross-sectional peers at a single
`as_of`, not historical analogues.** It reports `method="peer_profile"`
and `provisional=True` into the stored `detail` so no row recorded before
Module 11 exists can be mistaken for one recorded after. It can catch a
genuinely unprecedented profile; it says nothing about whether the pattern
has historically resolved well.

The seam is a `Protocol`, so Module 11 owes this module no import, base
class, or registration — only a three-argument call.
`tests/unit/candidate_detection/test_analogue_swap.py` proves the
substitution with a stub sharing no code with the shipped implementation.

## Measured reduction

On a synthetic universe with the planted profiles described in
`tests/unit/candidate_detection/synthetic.py`:

| Universe | Pool | Ratio |
|---|---|---|
| 100 | 10 | 10.0% |
| 1,000 | 100 | 10.0% |
| 5,000 | 500 | 10.0% |

The reduction is dominated by the selection fraction rather than by the
directional requirement, because very few securities sit exactly at a
252-bar high. With a realistic share genuinely at highs, the directional
filter contributes:

| At highs | Removed by direction | Final pool |
|---|---|---|
| 5% | 5.2% | 9.5% |
| 20% | 19.8% | 8.0% |
| 40% | 41.3% | 5.9% |

Stated plainly: **the pool size is set by the compute budget, not by the
pattern filter.** That is the correct design for a high-recall pre-filter
whose expensive error is exclusion, but it means `selection_fraction` is
the number that matters, and it should be revisited once Module 13 can
say how much of the pool ever scores well.

## Boundaries

Does **not**: classify market state (Module 10), compute historical
similarity (Module 11 — only the explicitly-provisional proxy), score
anything (Module 13), or make live FMP calls. Detection performs no I/O
at all: it takes Module 08's feature vectors and returns a pool.
