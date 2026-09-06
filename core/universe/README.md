# Universe Construction & Versioning

Universe — built in Module 06. Answers one question, for **any** date:

> Which securities existed and were tradeable on NYSE/NASDAQ as of X?

```python
from core.universe import build_intervals_from_fetch, construct_version, UniverseRepository
from data.normalization.identity import SecurityIdentityResolver

# `engine.begin()`, not `engine.connect()`. Under SQLAlchemy 2.0 a
# connection that closes without an explicit commit rolls back — so the
# earlier version of this example fetched ten thousand tickers,
# registered their identities, wrote the version and its membership, and
# then discarded all of it, with a log that looked like success.
async with FmpClient() as client:
    with engine.begin() as connection:
        construction = await build_intervals_from_fetch(
            FmpFetcher(client), SecurityIdentityResolver(connection)
        )

with engine.begin() as connection:
    repository = UniverseRepository(connection)
    today = construct_version(construction, repository)
    back_then = construct_version(construction, repository, as_of=datetime(2015, 3, 1, tzinfo=UTC))
```

In practice you do not write this: **`python -m infra.deploy.universe`**
does it, commits it, prints the label to set as `ARGUS_UNIVERSE_VERSION`,
and refuses with exit 1 if the next scheduled ingestion would find no
members in what it just built. See that module's docstring for the timing
trap it checks.

One fetch produces intervals; those intervals can be re-sliced into a
version for any date without refetching.

## Why membership is a dated join

A universe built only from today's listings excludes every company that
did not survive to the present. Backtests run against it look better than
reality, and **nothing fails** — the inflation is invisible unless
someone specifically checks. That is the survivorship-bias mechanism this
module exists to defeat.

So membership is not a status flag. Each security gets half-open
`[listed_from, listed_to)` intervals, and the universe for a date is
"which securities had an interval covering it". `listing_status` still
rides along, describing the security as of the version's as-of date, but
it is not what the query turns on.

**This required a Module 03 correction** (migration 0004): the original
`universe_membership` had `listing_status` and no date range at all. See
"Corrections to earlier modules" below.

## Interval evidence, and what FMP does not give us

**FMP's `stock-list` reports no listing or IPO date.** It says what is
listed now and nothing about when that began. The delisted feed *does*
carry `ipoDate` and `delistedDate` — so ARGUS knows more about when a
dead company was listed than a live one.

Boundaries come from the best available evidence, and which was used is
stored on every membership row:

| Evidence | Source | Reliability |
|---|---|---|
| `delisted_feed` | provider's `ipoDate` / `delistedDate` | reported |
| `price_history` | earliest/latest canonical bar | strong inference |
| `first_observed` | when the listing was first seen | weakest |
| `missing` | provider should have supplied it and did not | none |

An inferred boundary must stay distinguishable from a reported one.
Treating them as equally authoritative is how "the universe says it was
listed" quietly becomes unfalsifiable.

**Practical consequence:** until price history is ingested, a
currently-listed security's interval starts at *first observation*, so it
will not appear in any earlier historical universe. That is deliberate —
erring narrow produces a visible coverage gap rather than a silent claim
that a security was tradeable when ARGUS cannot show it. **Run the Module
04 backfill before constructing historical universes**, and pass
`bar_date_bounds(connection)` into `build_intervals_from_fetch`.

## Erring direction

Where a boundary is unknown, intervals are made **narrower, not wider**.
A too-wide interval puts a security into backtests during periods it was
not tradeable — a silent correctness error. A too-narrow one omits it
from some periods — a visible coverage gap that shows up in the report.

## Edge cases

- **Ticker change** (FB → META): intervals join on `security_id`, so one
  membership record, not two disconnected halves.
- **Delisted then relisted**: two intervals, and **the gap between them is
  preserved**. Bridging it would claim continuous listing across a period
  the security was not tradeable; the gap itself is honest and visible.
  The second interval cannot start before the delisting, even when price
  history reaches further back.
- **Recycled ticker**: two securities, two identities, resolved by date
  via Module 05.
- **Degenerate interval** (IPO date == delisting date): widened by one day
  and flagged `missing`, so the database's ordering check accepts it while
  the evidence marker says not to trust the boundary.

## Admission, and reporting instead of dropping

The universe is exactly what the exchanges report. No hardcoded ticker
list, no fixed size, no assumed count anywhere.

| Exclusion reason | Meaning |
|---|---|
| `unknown_exchange` | venue label unrecognised — **the one that matters** |
| `non_universe_exchange` | recognised but not NYSE/NASDAQ (Arca, OTC, …) |
| `foreign_symbol_suffix` | ticker denotes a non-US venue despite its label |
| `unresolved_identity` | no `security_id` and minting disabled |
| `missing_symbol` | provider row had no usable symbol |

Module 05's instruction, carried forward: **report `UNKNOWN` counts,
never drop them silently.** If FMP rebrands a venue to something
`normalize_exchange` cannot match, those securities vanish from the
universe and nothing downstream fails — the scan just covers fewer
stocks, and the first hint would be an unexplained drop in signal counts
months later.

So every exclusion is counted by reason, sampled, and the unrecognised
raw labels are tallied. The summary is stored in the version's
`definition`, so an unexplained shrink is diagnosable from the record
without re-running the fetch.

Note that `normalize_exchange` matches on substring, so an ordinary tier
rename ("NASDAQ Global Select" → "NASDAQ Tier Renamed") still classifies.
Only a rebrand with no recognisable substring falls to `UNKNOWN`.

The symbol-suffix guard is an explicit list of non-US venue suffixes, not
"any dotted ticker" — US class shares are dotted too (`BRK.B`, `BF.B`),
and a blanket rule would drop real NYSE constituents.

**Module 06 calls `fetch_stock_list()`, not Module 04's
`fetch_exchange_listings()`.** The latter applies its own client-side
exchange filter, which would discard the very rows this module has to
count — an unrecognised label would be dropped there and never reach the
report. Admission is this module's decision, so it sees everything.

## Versioning

`universe_version` and `universe_membership` are append-only (Module 03's
guards). A signal records the `universe_version_id` it was computed
against; if a published version's membership could be edited afterwards,
every historical result referencing it would become unverifiable.

Each version stores a **membership checksum** covering identity, venue and
both boundaries. Re-running construction over unchanged data finds the
equivalent version and reuses it rather than writing a duplicate; a
genuine change — a new listing, a corrected date, a reclassification —
produces a different checksum and therefore a new version.

## What this module deliberately does not do

- **No delisting reason.** FMP does not report one (Module 04's finding),
  so a bankruptcy and an acquisition are indistinguishable here. This
  module records *that* and *when* a security left, never *why*. Module
  09's bankruptcy-exclusion gate needs that answer and needs its own
  approach.
- **No identity minting scheme.** Registration is Module 05's
  (`SecurityIdentityResolver`); this module only asks it to run, and only
  for securities that pass admission.
- **No PIT enforcement.** Membership is built so Module 07 *can* query it
  point-in-time correctly; the enforcement mechanism is Module 07's.

## Corrections to earlier modules

**Migration 0004 — `universe_membership` listing intervals.** Module 03
modelled membership as `listing_status` alone, with no date range. That
makes the central question Module 07 has to answer unanswerable, and it
could not have been retrofitted later, because the source data (FMP's
delisted feed) is only available at construction time. Added
`listed_from`, `listed_to`, `exchange`, `interval_evidence`, an ordering
check, and an index serving the "in the universe on date X" predicate.

## Testing

```bash
pytest tests/unit/universe tests/integration/universe
```

Unit tests need no database. Integration tests use a throwaway database
migrated to head and skip cleanly when no PostgreSQL is reachable. No
test makes a live API call — construction runs Module 04's real fetch
path against `httpx.MockTransport`.
