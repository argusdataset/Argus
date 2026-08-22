# Feature Engine

Feature Engineering Engine — built in Module 08. Turns PIT-correct
canonical data into the 50 continuous measurements every later module
scores, ranks and explains.

```python
from core.feature_engine import compute_features_batch, publish_feature_schema_version
from core.feature_engine.spec import FeatureSpec

spec = FeatureSpec()
version_id = publish_feature_schema_version(connection, spec)

result = compute_features_batch(
    connection,
    security_ids,  # the whole universe, in one pass
    as_of,  # a plain argument: today, or a date in 2015
    spec=spec,
    feature_schema_version_id=version_id,
    market_security_id=spy_id,
)

for vector in result:
    if vector.evidence.has_sufficient_history:
        use(vector.features)  # floats, or None — never a filled 0.0
```

## The rule this module exists to obey

**No fixed duration thresholds anywhere.** A consolidation lasting three
weeks and one lasting three years must be measurable by the same
features. `if consolidation_days > 30` is the rigid rule the whole
architecture was built to avoid.

Every window here is a *measurement scale*, not a gate:

| | Gate (forbidden) | Scale (used throughout) |
|---|---|---|
| Shape | `if days_in_range > 30: is_base = True` | `realized_volatility` over 20 bars |
| Effect of changing it | Changes *what something is* | Changes the number's resolution |
| 3-week vs 3-year base | One qualifies, one does not | Both produce comparable readings |

Duration is a *feature* — `decline_duration_bars` — stored as a number
for downstream modules to weigh, never consumed here as a condition.
`tests/unit/feature_engine/test_spec.py` asserts the observable
consequence: shrink every window to a fifth or stretch it to double, and
all 50 features still compute.

## Point-in-time correctness

The leakage vector here is specific and was caught by Module 07 before it
could be built in. Module 05's `apply_adjustments` applies **every**
corporate action ARGUS has ever ingested — using it to build a historical
price series would let a 2015 feature reflect a split not knowable until
2020.

So this module never calls it. `panel.py` builds adjustment factors from
`load_corporate_actions_as_of` — actions filtered to `availability_time
<= as_of` — and applies them itself, vectorized. Same back-adjustment
convention as Module 05, strictly narrower input set.

Bulk PIT loaders live in **`core/data_validation/bulk.py`**, not here.
A vectorized engine cannot call the single-security `get_as_of` ten
thousand times per date, but putting bulk variants anywhere outside
`core/data_validation/` would mean a second copy of the enforcement rule
— precisely what Module 07 exists to prevent. Same rule, different arity.

`tests/integration/feature_engine/test_pit_leakage.py` documents the
break-attempt log proving those tests are load-bearing, including the
attempt that *passed* and the latent bug it exposed.

## The feature groups

| Group | What it measures | Count |
|---|---|---|
| A — `decline.py` | Prior decline and stabilization | 11 |
| B — `consolidation.py` | The base: tightness, quiet, defended edges | 13 |
| C — `awakening.py` | Volatility/volume re-expansion, pressure on the highs | 11 |
| D — `confirmation.py` | Clearing the range and holding | 7 |
| E — `context.py` | Relative strength, market regime inputs, liquidity | 8 |

Group F (outcomes) belongs to Module 15 and is deliberately absent.

Two design points worth knowing:

- **Group C's `rs_improvement_*` is computed from the relative-strength
  line's own slope**, with no dependence on a breakout having happened.
  Relative strength frequently leads price, and a feature that required
  the breakout first would make that leading behaviour unobservable by
  construction.
- **Group D reports magnitudes, never booleans.**
  `resistance_breakout_pct` is how far through the level price is
  (negative below it), not `has_broken == True`. A downstream module can
  threshold a magnitude however it likes; it cannot recover one from a
  boolean this module already discarded.

## Vectorization

`compute_features_batch` is the real implementation; `compute_features`
is a single-security convenience that delegates to it. That direction is
the only arrangement that survives a universe-scale historical scan.

Three checkable properties, all asserted in
`tests/integration/feature_engine/test_batch_performance.py`:

1. **Two SQL queries regardless of universe size** — one for bars, one
   for corporate actions. At two queries *per security per date*, a
   fifteen-year daily replay of 10,000 securities is 75 million round
   trips.
2. **Every feature is computed on wide (dates × securities) frames.**
   `close.rolling(20).mean()` covers the universe in one call.
3. **The only per-security work is the final row slice** — dictionary
   construction, not numerical work.

Property 1 is the one a disguised loop cannot fake, which is why the
suite counts queries rather than relying on wall-clock time alone.

A note on what vectorization does *not* buy: the arithmetic is genuinely
O(bars × securities), and no layout changes that. What the wide-frame
approach removes is the *fixed* cost paid per security — round trips,
pandas dispatch overhead, object construction. Measured on the test
fixture, cost per security falls by roughly a third between 5 and 25
securities. The test asserts amortization, not sublinear arithmetic.

## Multi-timeframe: what H4 actually means

Weekly and monthly panels are derived from daily by standard OHLCV
rollup. Nothing is invented; every output value is a reduction of daily
bars that exist. **Only completed periods are emitted** — a half-finished
week has a smaller range and lower volume purely because it is
half-finished, and mixing it into a rolling ATR would put a
systematically understated reading at the most recent and most
decision-relevant bar. Weekly features therefore lag by up to a week,
which is honest rather than convenient.

**H4 is refused, not fabricated.** `infra/db/README.md` records that H4
is "derived from daily", and the honest reading is that it cannot be. A
daily bar is one OHLCV tuple per session; the intraday path that produced
it is discarded before the data ever reaches ARGUS. The two ways to
manufacture it are both fabrication:

- Forward-filling one daily bar into six H4 buckets yields six identical
  bars — a flat intraday path that never happened, with a true range of
  zero for five of the six.
- Interpolating between daily closes invents a smooth path, which
  systematically *understates* intraday volatility and range expansion —
  precisely the Group B/C features H4 would exist to measure.

`derive_panel` raises `UnsupportedTimeframeError` rather than returning a
plausible-looking number. Real H4 features need intraday source data
(FMP's Ultimate tier exposes it); that is a data-acquisition decision,
not a derivation this module can perform.

## Missing evidence

A missing input survives the transformation into a feature, rather than
dissolving into a plausible number. An unavailable feature is `None`,
never `0.0` — 0.0 is a measurement ("drawdown from the 252-bar peak is
nil"), `None` is the absence of one, and a ranking that confused them
would put a 30-day-old listing at the top of a shallowest-drawdown sort.

Every vector carries `FeatureEvidence`: `bars_available` against
`bars_required`, the named `missing_inputs` with their `MissReason`, and
the exhaustive list of `unavailable_features`. Securities with no
knowable bars at all are reported in `missing_securities`, never silently
dropped — a caller who asked for 10,000 and received 9,300 cannot tell
survivorship bias from a quiet bug.

This is what Module 09's `INSUFFICIENT_EVIDENCE` gate will read.

## Known gaps

**No sector or industry data exists in the schema.** Confirmed by
inspection — there is no sector column anywhere in the identity tables.
Five of the 50 features therefore cannot be computed today and return
NaN with a recorded `MissReason.NEVER_INGESTED` rather than a fabricated
benchmark: `rs_vs_sector`, `rs_vs_industry`, `rs_deterioration_vs_sector`,
`rs_improvement_vs_sector` and `rs_alignment_sector`. The engine
accepts `sector_map` / `industry_map` arguments, so the features work the
moment that data is ingested; substituting the market benchmark would
have silently redefined what "relative to sector" means.

**Dividend adjustment is omitted.** `build_adjustment_factors` handles
splits only. The structural features this panel feeds are price-*shape*
measurements, and a dividend-adjusted series shifts every historical
level by an amount unrelated to the structure being measured.

## Boundaries

This module does **not**: detect candidates or apply eligibility gates
(Module 09), classify market state (Module 10 — `context.py` produces
regime *inputs*, never a label), score anything (Module 13), or make live
FMP calls. It computes numbers and reports honestly what it could not
compute.
