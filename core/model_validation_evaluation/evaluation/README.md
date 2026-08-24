# Model Evaluation (Module 17)

Takes a validation run's predictions plus Module 15's recorded outcomes
and answers "does this work, and under what conditions".

## Files

| File | What it owns |
|---|---|
| `config.py` | Sample floors, window sizes, score buckets — all kind-tagged |
| `dataset.py` | The one-row-per-setup frame every metric reads |
| `metrics.py` | Precision, recall, hit rate, expectancy, MFE/MAE, FPR, drawdown |
| `buckets.py` | Score monotonicity and the calibration curve |
| `walk_forward.py` | Rolling train/test windows |
| `breakdowns.py` | Regime and same-asset/cross-asset splits |
| `engine.py` | Assembling and storing one report |

## The four decisions that shape everything here

**Evaluation reads labels and never assigns them.** Whether a setup
succeeded is Module 15's judgement, recorded under a named snapshot.
There is no success threshold in this module. Re-deciding it here would
produce metrics that disagree with the stored dataset while looking
identical to it.

**Every return statistic is benchmark-relative.** A 30% gain in a year the
market gained 35% is not evidence, and a suite that reported it as one
would flatter the model exactly where it mattered least.

**Score monotonicity is a first-class output, and it can fail.** Two
questions hide inside "does ARGUS work": whether the pattern has an edge,
and whether the score orders it. A system can pass the first and fail the
second — and then the score is decoration that gets acted on. The analysis
is tested against a population where it must hold *and* against flat and
inverted ones where it must not.

**Thin samples report a count, never a rate.** Module 11's discipline,
applied everywhere: below the floor, statistics are `None` rather than
approximate. Today essentially every breakdown comes back INSUFFICIENT,
which is the correct answer and not a defect.

## What the numbers mean right now

Nothing. No historical scan has run, so the dataset is empty or nearly so
and every analysis returns "undetermined, and here is why". That is the
output this module is designed to produce honestly — the alternative is a
module that looks finished because it reported confident statistics from
four setups.
