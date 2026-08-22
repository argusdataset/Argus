# Normalization

Normalization — built in Module 05. Turns provider records into ARGUS
canonical records and persists them.

This is the **provider-independence boundary**. Everything upstream knows
about FMP; nothing downstream does.

```python
from data.normalization import (
    SecurityIdentityResolver,
    CanonicalWriter,
    normalize_security,
    persist,
)

with engine.begin() as connection:
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.resolve("AAPL")
    outcome = normalize_security(security_id=security_id, bars=bars, actions=actions)
    persist(outcome, CanonicalWriter(connection))
```

## What happens, in order

1. **Resolve** the ticker to a stable `security_id`, as of each record's
   own date.
2. **Translate** provider records to canonical ones, stamping the four
   PIT timestamps from the correct source fields
   (`data/canonical_model/README.md` has the table).
3. **Validate** for impossible values.
4. **Adjust** prices for corporate actions, keeping the raw series
   intact.
5. **Persist**, insert-only.

Adjustment runs *after* validation on purpose: a bar with an impossible
price would otherwise seed a dividend factor computed from it and quietly
distort every earlier bar in the series.

`normalize_security` is pure — no database access — so the transformation
can be tested without one. `persist` is separate, and takes a
`Connection` rather than an `Engine`, so the caller owns the transaction
boundary.

## Envelope absorption

Module 04 found FMP returns payloads in at least three shapes:

| Shape | Example |
|---|---|
| bare array | `[{...}, {...}]` |
| keyed wrapper | `{"historical": [{...}]}` |
| bare object | `{...}` |

The adapter flattens these at fetch time; `envelopes.unwrap_envelope`
handles the same shapes for nested payloads riding inside a record's
`raw`/`data`.

**The contract for any future provider adapter:** deliver a flat sequence
of typed records, one per observation, with unmodelled provider fields
kept rather than dropped. Envelope shape is the adapter's problem, never
the canonical model's. A test asserts all three shapes produce identical
canonical output, so the inconsistency provably does not leak.

## Identity resolution

Everything references `security_identity.id`. A ticker is only a lookup
key, resolved **as of a date**, because tickers change hands:

- **Ticker change** (FB → META): `record_ticker_change` closes the old
  window and opens the new one against the *same* `security_id`, so a
  2019 bar filed under FB and a 2024 bar under META join into one
  history. Getting this wrong yields two disconnected half-histories with
  nothing visibly failing.
- **Ticker recycling**: a delisted company's ticker reassigned years
  later resolves to whichever security held it at the date asked about.

Migration 0002's gist exclusion constraints make the lookup unambiguous —
one security holds one ticker at a time, one ticker maps to one security
at a time — so "who was trading as AAPL on 2013-06-01" has exactly one
answer, enforced by the database rather than assumed here.

**Scope note:** identity *registration* lives here rather than in Module
06 because canonical rows cannot be written without a `security_id` to
reference. Module 06 owns universe *versioning* (`universe_version` /
`universe_membership`) — different tables, a different question.

## Corporate action adjustment

Both series are kept, and both are needed:

- **Raw** is what actually printed. It is the point-in-time reality a
  trader would have seen, and the only honest basis for "this would have
  worked". Pre-split Apple really did trade near $400.
- **Adjusted** is split- and dividend-continuous. Without it, a 4:1 split
  looks like a 75% crash and every structural feature computed across it
  — decline depth, consolidation range, breakout magnitude — is nonsense.

Adjustment is **recomputed from ARGUS's stored corporate actions, not
taken from FMP's `adjClose`.** Two reasons: a vendor's adjusted series can
change silently between fetches, breaking the guarantee that re-running a
recorded configuration gives an identical result; and an adjustment ARGUS
computed can be explained from the specific actions that produced it.

Conventions:

- Back-adjustment: the most recent bar keeps its raw price, earlier bars
  are scaled. Keeps "today's price" a real number.
- Splits scale price by `denominator/numerator` and volume inversely.
- Dividends scale by `(close_before_ex − dividend) / close_before_ex`.
- The bar *on* the effective date is not adjusted; it already reflects
  the action.
- Bars with no later action get adjusted values equal to raw, so
  downstream reads are uniform rather than null-checked per bar.
- An action that cannot be applied (a split with no usable ratio, a
  dividend with no prior close) is **skipped and reported**, never
  approximated — a wrong factor silently distorts every earlier bar.

## Validation

Sanity checks only. Point-in-time *enforcement* is Module 07, at query
time.

**The bar for flagging is deliberately high.** ARGUS exists to find
stocks that fell 90% and then based for years; genuinely extreme data is
the signal, not the noise. A validator tuned to "this looks unusual"
would discard exactly the cases the system is built to find.

| Issue | Severity |
|---|---|
| negative volume, non-positive price, high < low, open/close outside range, duplicate | fatal — withheld from persistence |
| price move > 50% in a session with no corporate action that day | advisory — **flagged and still accepted** |

Flagged records are returned to the caller rather than discarded inside
this module: what to do with a suspect row is an operational choice, and
hiding it here would make that choice invisible.

## Persistence

Every write is an INSERT. There is no update or delete path, by
construction *and* by enforcement — **migration 0003 adds append-only
triggers to the three canonical tables**, so an UPDATE from anywhere is
rejected by the database.

That migration closes a gap in the Module 03 baseline:
`infra/db/schema/canonical.py` documented "restatements are new rows,
never edits" and the uniqueness constraints include `observation_time`
for exactly that reason, but the triggers were never installed, leaving
the doctrine as policy rather than structure.

Restatements work as designed: a revised fundamental arrives as a **new
row** with a later `observation_time`, and both survive. That is what
makes a historical claim checkable — the earlier row is the record of
what ARGUS believed at the time.

Re-ingestion is idempotent via `ON CONFLICT DO NOTHING`, so resuming an
interrupted backfill inserts what is missing and skips what is there.
`DO NOTHING` rather than `DO UPDATE` precisely because the latter would
fire the append-only trigger — the constraint and the guard agree.

## Testing

```bash
pytest tests/unit/normalization tests/integration/normalization
```

Unit tests need no database. Integration tests use a throwaway database
migrated to head and skip cleanly when no PostgreSQL is reachable. No
test makes a live API call — envelope tests run Module 04's real fetch
path against `httpx.MockTransport`.
