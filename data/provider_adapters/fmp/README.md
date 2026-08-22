# FMP Provider Adapter

FMP Provider Adapter — built in Module 04. Fetches data from Financial
Modeling Prep and returns typed intermediate objects.

It deliberately does **not** produce ARGUS canonical objects, write to the
database, apply corporate actions, build `universe_version`, or enforce
point-in-time rules. Those belong to Modules 05-07. Keeping that boundary
sharp is what makes adding a second provider a matter of writing one new
adapter rather than touching the feature engine or the state machine.

```python
from data.provider_adapters.fmp import FmpClient, FmpFetcher

async with FmpClient() as client:
    fetcher = FmpFetcher(client)
    listings = await fetcher.fetch_exchange_listings(("NYSE", "NASDAQ"))
    bars = await fetcher.fetch_daily_history("AAPL")
```

## ⚠️ Endpoint paths are unverified

**No FMP API key was available while this module was built, and FMP's
domain is unreachable from the build environment.** Every endpoint path
in `endpoints.py` was assembled from public documentation, not confirmed
against a live response. FMP has also migrated from the legacy
`/api/v3/...` layout to a newer `/stable/...` one and the two overlap
unevenly, so some paths are likely wrong.

This is why all paths live in `endpoints.py` and nowhere else: correcting
one is a single-line change with nothing else to touch. **The first task
with a real key should be to verify each path and each response shape
against `tests/unit/fmp/fixtures/`.** The fixtures are hand-written from
documented shapes, not recorded from live responses.

## What FMP actually offers

Findings that differ from what the roadmap assumed. These have
consequences for later modules.

### Rate limits are per-plan, and bulk is throttled separately

| Plan | Standard endpoints |
|---|---|
| Free | 250 requests/day |
| Starter | 300/min |
| Premium | 750/min |
| Ultimate | 3000/min |

Bulk CSV downloads are **not** covered by that allowance: FMP documents
roughly **one download per 10 seconds** (one per minute for profile and
ETF-holder bulk). There is also a trailing-30-day **bandwidth** cap —
500MB free, 20GB Starter, 50GB Premium, 150GB Ultimate — which is a real
constraint for a full-history backfill and is metered separately from
request count.

All of these are config, not constants (`ProvidersSettings`), because
they are properties of the subscription rather than of the API.

### "Bulk" is the wrong tool for a cold backfill

This is the finding most likely to change a plan. FMP's `eod-bulk`
endpoint returns **every symbol for one date**, not full history for many
symbols. That inverts the usual advice:

| Job | Per-symbol | Bulk-by-date |
|---|---|---|
| Cold backfill, ~30y, full universe | ~10,000 requests at 300-3000/min → **35 min – 5.5 hrs** | ~7,500 requests at 6/min → **~21 hrs** |
| Daily incremental | ~10,000 requests | **1 request** |

So the adapter provides both and uses each where it wins:
`backfill_daily_history` (per-symbol, concurrent, resumable) for the cold
load, `fetch_eod_for_date` (bulk) for the daily update. Designing
"bulk-first" throughout would have made the initial backfill roughly an
order of magnitude slower.

### Delisted coverage exists, but without a reason

FMP has a `delisted-companies` endpoint, which is what makes
survivorship-bias resistance possible at all. But it supplies only the
symbol, company name, exchange, IPO date and delisting date — **not why
the security was delisted.** A bankruptcy and an acquisition are
indistinguishable in this feed.

That matters directly for **Module 09's bankruptcy/going-concern
eligibility gate**, which the roadmap describes as a hard exclusion. It
cannot be built from this field. Options, none of them free:

- Infer from fundamentals already being fetched (collapsing equity,
  going-concern language, sustained negative cash flow) — imperfect, but
  uses data ARGUS already has.
- Infer from the price path at delisting (a delisting near zero is a
  different event from one at a premium) — cheap, and probably a decent
  discriminator.
- Add a second source for bankruptcy filings — real work, and a
  deferred-list item today.

**This needs a decision before Module 09.** It does not block Modules
05-08.

### Other observations

- **History depth**: FMP advertises 30+ years for most endpoints, but
  depth is not uniform — it depends on when a security listed and how
  well FMP covers it. The adapter never assumes a start date; a caller
  asks for what it wants and gets what exists.
- **Exchange listings**: `stock-list` returns everything FMP knows in one
  request, so the NYSE+NASDAQ universe is one call plus a client-side
  filter, with no hardcoded ticker list and no fixed count anywhere.
  Exchange labelling is inconsistent between the `exchange` and
  `exchangeShortName` fields, so `fetch_exchange_listings` matches both.
- **Errors arrive with HTTP 200.** Some FMP failures come back as a 200
  with an `{"Error Message": ...}` body. The client treats those as
  errors; taking them as data would put silent holes in the record.
- **Fundamentals carry `acceptedDate`**, which is the closest thing FMP
  offers to an observation time. It matters a great deal for Module 05:
  using the fiscal period end as if the numbers were known then would
  leak future information into every backtest.

## Design

| Concern | Where | Notes |
|---|---|---|
| Endpoint paths | `endpoints.py` | One place to correct. Marks which endpoints are bulk-tier. |
| Typed records | `models.py` | Mirror FMP's shapes, not ARGUS canonical. Unmodelled fields kept in `raw`. |
| Errors | `errors.py` | Distinct classes per failure mode. |
| Rate limiting | `rate_limit.py` | Separate token buckets for standard and bulk. |
| Caching | `cache.py` | Atomic writes; preserves original fetch time. |
| Resumption | `checkpoint.py` | Append-only JSONL; survives a kill mid-write. |
| HTTP | `client.py` | Auth, retry, backoff, error classification, redaction. |
| Fetch operations | `fetchers.py` | The public surface. |

### Provenance

Every record carries `provenance`: provider, endpoint name, URL path,
request parameters, and `fetched_at`. Module 05 maps `fetched_at` to the
canonical `ingestion_time` PIT field.

**On a cache hit, `fetched_at` is the timestamp of the original fetch,
not of the cache read.** The data really was observed then, and
refreshing it would misstate the point-in-time record. `from_cache` marks
which is which.

### Failure modes

Errors are raised, never returned as empty data — "we were rate limited"
and "this security had no trading history in 2009" both produce zero rows
but mean entirely different things, and conflating them would silently
corrupt the historical record.

| Situation | Result |
|---|---|
| Missing/rejected API key | `FmpAuthenticationError` (not retried) |
| HTTP 429, or a limit message in a 200 body | `FmpRateLimitError` (retried, honours `Retry-After`) |
| HTTP 5xx | `FmpProviderError` (retried) |
| HTTP 4xx, unparseable body, error payload | `FmpProviderError` (not retried) |
| Timeout, connection failure | `FmpTransportError` (retried) |
| Success with no rows | `FetchResult` with `empty_reason` set |

One honest limitation: **FMP does not reliably distinguish "no such
symbol" from "symbol exists but has no data in this range"** — both
commonly return an empty array with HTTP 200. `FmpSymbolNotFoundError` is
therefore only raised when the adapter can actually tell. Otherwise the
result reports `EmptyReason.NO_DATA_RETURNED` and the caller decides,
typically by cross-referencing the stock list and the delisted list.

### Credentials

The API key comes from `SecretsProvider.get_secret("FMP_API_KEY")`. It is
never a config field, never logged, never part of a cache key (so a key
rotation does not invalidate the cache), and redacted from error messages
before they can surface.

## Testing

All tests run against recorded fixtures through `httpx.MockTransport` —
no live calls, no key needed, no request budget spent, and results do not
change because a provider's data did. MockTransport rather than patching,
so URL construction, auth, rate limiting, caching, retry, and error
classification all execute as they would in production.

```bash
pytest tests/unit/fmp
```
