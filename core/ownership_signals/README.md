# Module 29 — Insider Clusters and Institutional Ownership

Two signals from the FMP Ultimate plan's ownership data. Both are
**display-only context**: neither enters `core/scoring/`, and neither can
add a security to a watchlist, remove one, or change where it ranks.

## The two signals, and why they are shaped differently

| | Insider cluster | Institutional (13F) trend |
|---|---|---|
| Question | Did several insiders buy recently? | Is institutional ownership growing or shrinking? |
| Nature | An event | A direction |
| Verdict | `raised: True \| False \| None` | **no verdict at all** |
| Cadence | Daily | Quarterly |
| Disclosure lag | 48 hours (Form 4) | 45 days (13F) |

### Insider clusters — `insider.py`

Counts **distinct people** who made an **open-market purchase** (SEC
transaction code `P`) in the trailing window. Three deliberate choices:

- **People, not transactions.** An officer buying in three tranches
  decided once. Counting tranches would let one decision clear a bar
  meant to require agreement between several.
- **Purchases only.** Grants (`A`) and option exercises (`M`) are
  compensation arriving on a schedule somebody else set. Counting them
  would make the signal fire hardest in the months a company happens to
  vest equity.
- **A count, not a dollar figure.** A $200,000 buy is a large personal
  commitment from a small-cap CFO and a rounding error from a mega-cap
  CEO. A dollar floor would systematically select large companies — the
  flat-threshold-across-a-heterogeneous-universe mistake
  `docs/architecture/KNOWN_ISSUES.md` E5 records this project already
  making once.

`raised` is tri-state. `None` means this security's Form 4 history has
never been fetched, which is a different fact from "insiders are not
buying" and must never be displayed as one.

### Institutional trend — `institutional.py`

Stores this quarter's figures, the prior quarter's, and the change
between them. **No `raised` column exists**, not even a nullable one: how
much ownership movement matters is the reader's judgement, and a boolean
here would encode somebody's opinion as if it were a measurement.

Change figures are `None` — never zero — when there is no prior quarter.
A first observation has nothing to have changed from.

| Stored field | Source |
|---|---|
| `investors_holding` | Institutions reporting a position this quarter |
| `investors_holding_change` | Provider's own figure when offered, else computed |
| `total_shares` | Shares held across all reporting institutions |
| `total_shares_change_percent` | Computed here — a percentage needs a denominator only the two absolutes supply |
| `ownership_percent` | Institutional holdings as a share of the float |
| `prior_year` / `prior_quarter` | Which quarter the comparison used, so a reader can check it |

## Point-in-time is the thing to get right here

Both data types have a disclosure lag, and both would leak badly without
it:

- **Form 4** is due two business days after an insider trades. Filtering
  on the transaction date would let a replay read a purchase before it
  was disclosed. A consequence worth knowing: *yesterday's* purchase is
  not part of today's reading.
- **13F** is due 45 days after quarter end. Treating a quarter's holdings
  as knowable at the quarter end would hand a backtest six weeks of
  hindsight on exactly the accumulation this module surfaces — the single
  largest leak available here.

Both constants are tagged `structural` in `config.py`: they follow from
SEC deadlines, not from anyone's judgement.

## Thresholds

Four, all in `config.py`, all kind-tagged. The two calibratable ones — a
thirty-day window and a two-buyer minimum — are **invented magnitudes**.
Every result carries `calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## The non-interference guarantee

`core/market_state/`, `core/scoring/`, `core/candidate_detection/eligibility/`
and `core/live_scanner/` must never import this package, its schema
module, or name its tables as strings. Proved three ways in
`tests/unit/news_signals/test_isolation_from_scoring_and_market_state.py`
and behaviourally in
`tests/integration/intelligence/test_signal_isolation.py`, which attaches
the loudest possible readings to a security with no ownership data at all
and asserts the watchlist entry, its order, and the whole score block are
byte-identical.

## How data gets here

`core/ingestion/ownership.py` (Module 26) fetches and writes the raw
rows; this module only reads them. That separation is why
`orchestrator.py` needs no FMP client and no secret beyond the database
connection.

- **Form 4 and 13F** are per-symbol fetches paced by Module 26's own tier
  decision — daily for BREAKOUT_READY and UPTREND, thirty days in
  DOWN_TREND. The cost, stated plainly: a cluster forming in a
  DOWN_TREND name is seen up to thirty days late. Acceptable because a
  cluster is measured over thirty days anyway; it is a slow signal, and
  observing it slowly does not change what it says.

## Cost

One aggregate query per batch for the insider signal, one for 13F —
never one per security. Storage is a chunked upsert; a same-day (or
same-quarter) rerun converges on one row rather than accumulating.
