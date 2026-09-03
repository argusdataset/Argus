# Module 26 — Daily Ingestion Orchestration + Tiered Deep Refresh

Decides *when* to fetch *what* for *whom*. Module 04 fetches, Module 05
normalizes and persists; this module does neither.

## Why it exists

Nothing in production wired Module 04's fetchers to Module 05's
persistence on any schedule. `fetch_eod_for_date`, `fetch_daily_history`,
`fetch_financial_statement`, `fetch_news`, `normalize_security` and
`persist` all had **zero non-test callers**, and the seven deployed
processes included no ingestion step.

The consequence was concrete: Module 18's scanner checks
`canonical_ohlcv` coverage before it will scan, and nothing was writing
to that table. Once `ARGUS_UNIVERSE_VERSION` was set, the scanner would
have run every scheduled weekday and recorded `DATA_NOT_READY`, forever.
This is not an enhancement — it is the foundation the scanner already
assumed existed.

## Why `core/ingestion/` rather than `data/ingestion/`

`data/` is the provider and canonical-record layer, and today it depends
on nothing in `core/` — the arrow points the other way, with six `core/`
modules importing `data/`. This module has to read universe membership
(Module 07's reader over Module 06's data), the `market_state`
projection (Module 10) and Module 18's own schedule, all of which live in
`core/`. Putting it under `data/` would invert that direction for the
first time.

`core/live_scanner/` is the precedent and the closest analogue: an
orchestrator that owns no data of its own, reads from `core/` and
`data/`, and is driven by a cron wrapper in `infra/deploy/`. This module
has exactly that shape.

## The two scopes

**A. Daily full-universe OHLCV.** Required, unconditional, every trading
day, every universe member. No tiering: Module 10 cannot see a
DOWN_TREND → CONSOLIDATION transition it has no bars for.

**B. Tiered deep refresh** — fundamentals and news, at a cadence set by
the security's current watchlist phase:

| Phase | Interval |
|---|---|
| DOWN TREND (DOWN_TREND, BASE_FORMING) | 30 days |
| CONSOLIDATION (CONSOLIDATION, ACCUMULATION) | 10 days |
| BREAKOUT READY (BREAKOUT_WATCH, BREAKOUT_READY) | daily |
| UPTREND | daily |

A security on no watchlist — `UNCLASSIFIED`, `DISTRIBUTION`, or one with
no state row — gets no tier and no deep refresh. Its bars are still
fetched, because scope A is unconditional.

Universe *construction* (Module 06) is out of scope. This module assumes
a version exists and is named by `ARGUS_UNIVERSE_VERSION`, and refuses
the same way `infra/deploy/scanner.py` does when it is not — through the
same function, not a copy of it.

## The order of the two, and why it is not an implementation detail

Both halves spend the same FMP per-minute budget. Only one has a
deadline: `readiness.py` checks OHLCV coverage and nothing else, so a
lagging fundamental never blocks a scan. Running them concurrently would
spend the price pull's margin on news, so they run in sequence, prices
first, and the deep refresh gets whatever is left.

## Which FMP endpoint, and why it is inferred rather than chosen

`fetch_eod_for_date()` covers the whole universe in one bulk-tier
request and is obviously better for a daily pull — except that bulk
endpoints are believed to require FMP's Ultimate plan, and calling one
without the entitlement fails rather than degrades.

No endpoint reports which plan a key is on. What configuration does
carry is the per-minute limit, and FMP's published limits are distinct
per plan: 300 Starter, 750 Premium, 3000 Ultimate. So
`fmp_requests_per_minute >= bulk_entitlement_requests_per_minute` is
read as "Ultimate provisioned, use bulk", and anything below it as "use
one standard-tier request per symbol". The default lands on per-symbol,
which is what the recommended Premium plan supports.

It is a proxy and it fails safe: having Ultimate but a conservative
limit configured costs a slower run against an endpoint every plan has.
Being wrong the other way requires raising the limit past 3000 without
the plan. Revisit `strategy.py` the day the tier changes.

## Request volume

**OHLCV, a firm number.** One request per universe member per trading
day on the per-symbol path — about **10,000 requests/day** for a
10,000-name universe, or **~2.5 million a year** over 250 sessions. At
Premium's 750/min that is roughly fourteen minutes; at Starter's 300/min
about thirty-three. The cron fires ninety minutes before the scanner, so
either finishes with margin. On the bulk path it is **one** request a
day.

**Deep refresh, a formula.** The real phase distribution is unknown
until this runs against live data, so this is parameterized rather than
guessed. With `N` universe members, `f_p` the fraction in phase `p` and
`d_p` that phase's interval in days, each security in phase `p`
consumes `(S + 1) / d_p` requests per day — `S` statement types plus one
news request:

```
requests/day  =  N × (S + 1) × Σ_p ( f_p / d_p )
```

With today's configuration `S = 5`, so `S + 1 = 6`, and:

```
requests/day  =  6N × ( f_down/30 + f_consol/10 + f_breakout/1 + f_up/1 )
```

The shape matters more than any point estimate: the two daily tiers
dominate completely. At `N = 10,000`, every percentage point of the
universe sitting in BREAKOUT_READY or UPTREND costs **600 requests a
day** on its own, while a percentage point in DOWN_TREND costs 2. So the
number to watch after the first live run is `f_breakout + f_up`, and the
cheapest lever if it is too expensive is not the 30-day tier — it is
trimming `statement_types`, since fundamentals change quarterly and are
being re-requested daily for the urgent names.

Both halves draw on one `FmpClient`, hence one set of token buckets, so
these add rather than competing for separate budgets.

## Idempotency has to survive an empty filesystem

Module 04's JSONL checkpoint is reused for in-run resumability, as it
should be — a process killed halfway through ten thousand symbols
resumes rather than restarting.

It cannot be the durable answer, because a Railway cron container starts
each firing with a fresh filesystem: last night's checkpoint is not
there. So the skip that makes a re-run free is a database question asked
before any fetching — *which members already hold a bar for this
session* — and the checkpoint is the second layer. The two cover each
other: a symbol checkpointed after a successful fetch whose persist then
failed is skipped in-run and re-fetched tomorrow, when the database
finds no bar for it.

The deep refresh needs neither, because the log table answers it: a
security refreshed today is not due today.

## The new table

`deep_refresh_log` (migration 0014), one row per completed refresh:
`security_id`, `refreshed_on`, `refreshed_at`, `triggering_watchlist`,
`market_state`, `trigger`, `statements_written`, `news_written`,
`config_version_label`, `detail`. Unique on `(security_id,
refreshed_on)`.

A **log**, not a per-security row that gets overwritten, and the reason
is the Source-of-Truth principle `core/market_state/watchlists.py`
states: a watchlist is a filter over the `market_state` projection and
never an independently stored value. A `current_phase` column here would
be that second copy. `triggering_watchlist` is the phase that drove *one
past refresh* — a historical fact — and nothing reads it as an answer to
"what phase is this security in now", which is always asked live.

Due-ness compares dates, not timestamps: the intervals are whole days,
the job runs once a day, and a cron firing ninety seconds early must not
skip a daily tier by landing at 23h 59m.

Not append-only guarded, matching `live_scan_runs` (migration 0009): an
operational record of a job, from which nothing computes a statistic. It
is therefore not eligible for Module 25's `MONITORED_TABLES`, which
asserts everything it watches is guarded — so its growth, bounded by the
number of due securities per day, is watched by nobody. Flagged, not
fixed.

## Two things this module could not fix

**1. The scanner still cannot see what this writes.** Module 05 stamps a
daily bar's `availability_time` as its session close **+ 16 hours**;
Module 18's point-in-time cutoff for that session is its close **+ 5**
(`scan_offset_hours`). `check_readiness` filters on `availability_time <=
as_of`, and 16 > 5, so coverage reads zero however complete the
ingestion was. The scanner would go on recording `DATA_NOT_READY` — the
same outcome as before this module existed, reached a different way.

Module 18's own fixtures insert bars with a **one-hour** availability
lag, which is why every readiness test has always passed against a bar
no production path can produce.

The fix is one number: `scan_offset_hours` must exceed the bar lag, e.g.
5 → 17. It belongs to `core/live_scanner/config.py`, which this module
was told not to modify, so it is flagged — and demonstrated:
`tests/integration/ingestion/test_readiness_handoff.py` pins the
arithmetic, pins the failure, and shows the one-number change working.

Worth noting alongside it: `scan_offset_hours` is tagged `operational`,
which claims a number "bounds how the computation runs, never what it
produces". It feeds `as_of`, and `as_of` decides what a scan can see.

**2. Corporate actions are never ingested.** Scope A is prices, scope B
is fundamentals and news. Nothing fetches splits or dividends on any
schedule, and Module 08's `load_panel` builds its adjustment factors
from `canonical_corporate_actions` at load time — so with that table
static, a split makes every price series wrong from the split date back,
silently. Module 15's own README describes the consequence: an
unadjusted 2-for-1 split is a −50% single-bar excursion, recording a
successful setup as a catastrophic failure.

It was not added because Module 04 exposes splits and dividends only
per-symbol (there is no calendar endpoint), so a daily full-universe
sweep would be **two more requests per symbol per day** — tripling the
OHLCV volume. That is a spending decision, and it is the user's, not
this module's. The cheap version, if one is wanted, is a third tier:
corporate actions on the same 30/10/1/1 cadence as the deep refresh,
which costs `2N × Σ_p (f_p / d_p)` instead.

## What is tested

- `tests/unit/ingestion/test_threshold_isolation.py` — the AST scan
  Modules 10-18 established, plus the honesty check on this module's new
  `cost_policy` kind and on the absence of `UNVALIDATED_PLACEHOLDERS`.
- `tests/unit/ingestion/test_tier_dueness.py` — every due-ness rule,
  including all five ordered pairs whose tier strictly shortens.
- `tests/unit/ingestion/test_endpoint_strategy.py` — the plan inference
  and its direction of failure.
- `tests/integration/ingestion/test_daily_ingestion.py` — a first day, a
  free re-run (with the checkpoint deleted between them), a checkpoint
  resume isolated from the database skip, and the `acceptedDate` PIT
  rule surviving this path.
- `tests/integration/ingestion/test_tiered_refresh.py` — all seven
  listed states mapping to the right tier, both internal states getting
  none, and the phase-transition reset.
- `tests/integration/ingestion/test_readiness_handoff.py` — the
  availability/offset finding, from both sides.
- `tests/integration/deploy/test_ingestion_process.py` — the wrapper's
  refusals and exit codes, and that both crons resolve the universe
  through one function.

## Known gaps, flagged not fixed

- **`canonical_news` has no writer in Module 05.** Module 19 added the
  table; `CanonicalWriter` still handles only bars, fundamentals and
  corporate actions, so "persist via Module 05" is not possible for
  news. `news_writer.py` does it here instead, following
  `persistence.py`'s convention exactly. It should not survive: the
  right fix is `CanonicalWriter.write_news`, and it deletes that file.
- **Fundamentals and news share one cadence.** Fundamentals change
  quarterly; news changes hourly. Refreshing both daily for the urgent
  tiers is five-sixths fundamentals re-requesting rows `persist()` then
  declines to insert. Two independent cadences would be strictly better
  and were out of scope.
- **The bulk path does not self-repair a missed day.** The per-symbol
  path asks for a lookback window, so a missed session is filled in on
  the next run. The bulk endpoint answers for one date, so a day missed
  under it stays missed until a per-symbol run or a backfill covers it.
- **`deep_refresh_log` growth is unmonitored.** See the table section.
