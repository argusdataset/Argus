# Canonical Data Model

Canonical Data Model — built in Module 05. ARGUS's own data shapes, and
the point-in-time timestamp rules that govern them.

**These are the only data shapes any module after normalization may
import.** Nothing downstream references a provider's field names,
response envelopes, or `data.provider_adapters` at all. That boundary is
what makes adding a second provider a matter of writing one adapter plus
one translator, rather than touching the feature engine and everything
after it.

```python
from data.canonical_model import CanonicalOhlcvBar, PitTimestamps
```

## The four PIT timestamps

Every canonical record carries all four, non-nullable, matching Module
03's columns. The rule that governs all of them:

> A timestamp must reflect when ARGUS could actually have known the
> value — not when the underlying event occurred.

| Field | Meaning |
|---|---|
| `event_time` | when the real-world thing happened |
| `observation_time` | when it first became knowable |
| `availability_time` | when the provider made it available to ARGUS |
| `ingestion_time` | when ARGUS fetched it |

### Where each one comes from

| Canonical type | `event_time` | `observation_time` |
|---|---|---|
| OHLCV bar | session close (16:00 America/New_York) | session close — a bar is complete when the session ends |
| Fundamentals | fiscal period end | **`acceptedDate`**, never period end |
| Corporate action | effective / ex date | declaration date if supplied, else effective date |
| News | publication time | publication time |

**Fundamentals is the one that matters.** A Q1 ending March 31 may not be
filed until May 15. Sourcing `observation_time` from the period end would
let every backtest read those numbers six weeks before they existed, and
nothing downstream would ever notice. The fallback chain is
`acceptedDate` → `filingDate` → **reject the row**. There is deliberately
no fall back to the period end: it is always available and always wrong,
so permitting it would quietly reintroduce the exact bug.

### `availability_time` is derived, and errs late

FMP does not report when a record entered its dataset, so
`availability_time` is `observation_time + lag` from `ProviderLagPolicy`:

| Data type | Default lag | Reasoning |
|---|---|---|
| Daily bar | 16 hours | EOD publishes within hours, but consolidated values settle overnight; next-morning availability avoids claiming same-session knowledge of a close |
| Fundamentals | 24 hours | SEC acceptance is immediately public, but a vendor's parse and publication lag it |
| Corporate action | 24 hours | measured from announcement, or effective date when none is given |
| News | 30 minutes | aggregators surface articles quickly |

The asymmetry justifies the generosity: **a lag that is too long makes
ARGUS mildly pessimistic; one that is too short creates leakage, which is
unrecoverable and invisible.** Tighten them only once real observations
justify it — the policy is a dataclass so a caller can.

### Timezone handling

`event_time` for a bar is anchored to 16:00 `America/New_York` and
converted to UTC, so it follows daylight saving rather than drifting an
hour twice a year. Half-day sessions are not modelled; the difference is
three hours on a handful of dates and always in the conservative
direction.

Naive datetimes are treated as UTC, never as local time — the process
timezone is an accident of deployment and must not change what a
historical timestamp means.

## Exchange normalization

FMP labels exchanges inconsistently: SPY comes back as
`exchange="NYSE Arca"` with `exchangeShortName="AMEX"`. `normalize_exchange`
consults the long name first (it is the more specific of the two) and
falls back to the short name.

**Unrecognised labels become `UNKNOWN`, never a guess.** Silently mapping
an unfamiliar venue into NYSE would put securities into Module 06's
universe that do not belong there, with nothing to catch it.

`normalize_symbol` only uppercases and strips. Suffixes like `.TO` are
preserved deliberately — stripping them would collapse a Toronto listing
onto its US namesake, exactly the silent wrong join the ticker-history
exclusion constraints exist to prevent.

## Known gaps

- **No news table.** Module 03 defines none, so `CanonicalNewsArticle`
  has a type but no persistence path. Module 19 (Terminal) will need one;
  inventing the table here would have been out of scope. Flagged for
  whoever scopes Module 19.
- **`fiscal_period_end` and `effective_date` are `DateTime` in Module 03**
  but semantically dates. The round-trip is lossless (a date stores as
  midnight UTC), but reads compare against `.date()`.
