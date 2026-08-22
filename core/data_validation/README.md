# Data Validation

Data Validation — built in Module 07. The point-in-time enforcement layer:
every future module reads canonical/derived data through `get_as_of`,
never through raw SQL that could bypass `availability_time` filtering.
This is the module that makes ARGUS's core claim — *"this pattern
actually would have worked historically"* — checkable rather than merely
asserted.

```python
from core.data_validation import get_as_of, EntityType
from data.canonical_model.records import CanonicalStatementType

result = get_as_of(
    connection,
    EntityType.FUNDAMENTALS,
    security_id,
    as_of,
    statement_type=CanonicalStatementType.INCOME_STATEMENT,
)
if result:
    use(result.value)
else:
    # result.reason explains why — never a silent None or a stale fallback.
    ...
```

## The enforcement rule

**Filter on `availability_time`, never `event_time` or `observation_time`.**
Among rows that pass the filter, take the one with the greatest
`availability_time` — never the first match, never the unconditionally
latest row. This single rule lives in exactly one place,
`engine.select_latest_as_of` / `select_all_as_of`, and every entity-specific
query (`ohlcv.py`, `fundamentals.py`, `corporate_actions.py`,
`feature_vectors.py`) is built on it rather than re-implementing it.

Universe membership (`universe.py`) uses a different but equally
load-bearing predicate — Module 06's half-open listing interval
(`listed_from <= as_of AND (listed_to IS NULL OR listed_to > as_of)`) —
because that is the correct mechanism for that entity, not because the
rule is inconsistent.

## `get_as_of`: the single chokepoint

Every typed function above is independently usable, but `query.get_as_of`
is the one name "read PIT-correct data" means in this codebase — the
thing a future module should reach for instead of raw SQL against
`canonical_ohlcv` or `canonical_fundamentals`. Required params by entity
type are documented on the function; a missing one raises immediately
rather than silently querying the wrong thing.

**Dual-mode by construction.** Every call takes an explicit
`as_of: datetime` with no "current" default. A live caller passes
`datetime.now(UTC)`; a Module 17 batch-replay caller passes a date from
2015. Both go through the identical function and get the identical
return type — which is exactly what
`docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` asks Modules 08-16 to
have. If `get_as_of` needed a `live: bool` flag or a separate batch
variant, that requirement would already be broken here.

## No implicit "current" fallback

Every query returns `AsOfResult`, not a bare value. A miss is explicit —
`result.found is False`, `result.reason` names why (`NOT_YET_AVAILABLE`,
`NEVER_INGESTED`, `OUTSIDE_INTERVAL`) — so "nothing was knowable as of
this date" can never be confused with "the field is null" or silently
papered over with the most recent row regardless of date.

## The adversarial leakage test

`tests/integration/data_validation/test_pit_enforcement.py` is the
module's actual point, more than the rest of the suite combined. The
scenario: a fundamentals figure is restated two and a half months after
its original filing (Module 05's rule — a restatement is a new row,
never an edit). A query dated before the restatement must see only the
original value; a query dated after must see the correction.

Verified by hand during development, in two steps:

1. **First attempt**: swapped the `ORDER BY availability_time DESC` for
   `ORDER BY observation_time DESC`, leaving the `availability_time <=
   as_of` filter untouched. The test still passed — the fixture's
   `observation_time` and `availability_time` happen to coincide, so the
   WHERE clause alone was already doing all the work. Worth keeping on
   record rather than discarding: "I broke something and the test still
   passed" is not proof the enforcement is load-bearing; you have to
   break the actual mechanism.
2. **Second attempt**: removed the `availability_time <= as_of` condition
   entirely. The test failed immediately, returning the restated $91.2B
   figure for a query dated two months before it existed. Reverted
   afterward.

`test_a_naive_query_would_leak_the_restatement` keeps a permanent,
concrete version of this in the suite: it runs the wrong query directly
against the same fixture and asserts it leaks, alongside the real
function proving it doesn't.

## `interval_evidence`, surfaced not dropped

Module 06 stores one string per membership row (`"from=X;to=Y"`)
recording how each listing-interval boundary was established.
`universe.parse_interval_evidence` recovers the structured
`IntervalEvidence` pair, and both `get_universe_membership_as_of` and
`list_universe_members_as_of` return it on every result — never silently
dropped.

A `first_observed` boundary is a materially weaker claim than a
`delisted_feed` one. A future module — historical similarity comparing
setups across different eras, or model evaluation weighting sample
quality — is expected to use this to down-weight or filter on evidence
strength, rather than treating every membership claim in a query result
as equally solid.

## Scope: what this module does *not* build

A `universe_version` is already a snapshot as of its own `as_of_date` —
Module 06's `construct_version` only persists members covering that
date. This module does not construct new snapshots for arbitrary dates
(that's Module 06, already built); it queries an *existing* version's
membership PIT-safely, re-applying the interval predicate defensively
and surfacing evidence. A caller needing a universe for a date with no
materialized version calls Module 06's `construct_version` first.

## Gap and duplicate detection

Separate from the PIT query layer — these ask "is there a hole in what
we ingested" and "did we re-ingest the same value twice", not "what did
we know on a given date", so neither is filtered by `availability_time`.

Both are **advisory only**, matching Module 05's philosophy for its own
validation: flag, never block or silently repair. ARGUS exists to find
extreme, unusual price action; a gap detector that interpolated would
invent data, and a strict one that raised would abort a batch job over a
stretch that might be a genuine pre-IPO or halt period. A `GapReport` or
a list of `DuplicateBar`s is a fact for an operator to look at.

`calendar.py` computes US market holidays algorithmically (fixed dates,
nth-weekday rules, and Easter for Good Friday — verified against four
known Easter dates) rather than a hardcoded per-year table, so gap
detection works for any year in ARGUS's ~30-year window. Early closes
(half-days) are not modelled — the day still counts as a trading day,
which is all gap detection needs — mirroring the same call Module 05
made in `session_close` for the same reason.

Duplicate detection is narrower than a schema-level unique constraint:
Module 03 already guarantees no byte-identical row can exist twice. What
this catches is two rows for the *same* logical record with *different*
`observation_time` but *identical* values — a spurious re-ingestion (a
cache miss that re-fetched something that hadn't actually changed)
rather than a genuine restatement. A real correction with a different
value is never flagged; that's exactly what the PIT layer's restatement
handling exists to serve.

## A near-miss caught while building this, not a Module 03 defect

Fundamentals restatements are identified by `(security_id,
statement_type, fiscal_period_end)` here — a real date — never by
`fiscal_period` (a label like `"Q1"`) alone. FMP does not year-qualify
that label, and Module 03's uniqueness constraint on
`canonical_fundamentals` doesn't include `fiscal_period_end`. In practice
a same-second collision between two different years' "Q1" filings is
astronomically unlikely, so the constraint still enforces real
uniqueness — this doesn't need a migration, because nothing is actually
broken at the schema level. But a query keyed on the label alone would
have been wrong the day two years' Q1 statements needed disambiguating,
so this module's queries use the column that's actually a period
identity. `test_a_year_qualified_period_end_disambiguates_same_label_different_years`
pins this.

## Testing

```bash
pytest tests/unit/data_validation tests/integration/data_validation
```

Unit tests (calendar, `AsOfResult`) need no database. Integration tests
use a throwaway database migrated to head and skip cleanly when no
PostgreSQL is reachable — PIT enforcement is a claim about what SQL
actually returns, so it is proven against real PostgreSQL, not mocked.
