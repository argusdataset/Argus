# Module 28 — Reactive News-Volume Signal

Answers one narrow question, per security, per day: did unusually many
`canonical_news` articles show up today, relative to this security's own
trailing average? **A reactive, already-occurred anomaly detector — not a
prediction, and not a read of *what* the news is about.** Volume only.

## What it produces

`evaluate(...)` — pure, connection-free — returns a `NewsVolumeSignal`:

| Field | Contents |
|---|---|
| `raised` | `True \| False \| None`. **`None` is a first-class answer**, never coerced to `False` — the same discipline `core/risk_context/flags.py`'s `RiskFlag.raised` follows. |
| `today_count` | `canonical_news` rows for this security on `scan_date`, PIT-filtered on `availability_time <= as_of`. |
| `baseline_mean` | Trailing daily average over `baseline_window_days`. `None` exactly when `raised` is `None`. |
| `baseline_window_days`, `multiple_threshold` | The configuration this verdict was computed under, carried with it so a stored row is re-derivable without the code that produced it. |
| `unavailable` | A `core.data_validation.result.MissReason`, set exactly when `raised is None`. |
| `detail` | The raw counts and window bounds behind the verdict — full reproducibility, mirroring `RiskFlag.detail`. |

`assess_news_volume` wraps it for one security; `assess_batch` computes it
for many from a single aggregate query (`queries.py`'s `news_counts_for`,
using Postgres `FILTER` so one pass over `canonical_news` produces
today's count, the baseline total and the earliest known article date
together); `store_signals` upserts the result into `news_volume_signals`
on `(security_id, signal_date)`.

## The two undetermined cases

`raised` is `None` for two distinct reasons, each carrying its own
`MissReason`:

- **`NEVER_INGESTED`** — no `canonical_news` row has ever been observed
  for this security. There is nothing to compare today's count against.
- **`NOT_YET_AVAILABLE`** — some history exists, but the earliest known
  article is not yet `baseline_window_days` old. A name first covered
  eight days ago has an eight-day history, not a thirty-day one, and
  reporting a thirty-day average from it would report a number that only
  looks precise.

Both are reported as `None`, never as `False` — an undetermined reading
and a measured "no anomaly" are different facts, and collapsing them
would make the first look like the second.

## The decision, once determinable

`baseline_mean = baseline_total / baseline_window_days` — the average
**including** zero-article days, which is what makes "normal" differ
between a quiet name and a busy one. `raised` is whether today's count
reaches `anomaly_multiple` times that average (`>=`, not `>`).

**The one deliberate exception:** a `baseline_mean` of exactly zero cannot
be multiplied into a threshold, so the fallback is the plainest reading of
"any coverage at all is unusual for a name that normally has none":
`raised = today_count > 0`. A security whose baseline is genuinely zero
raises on its very first article. That is disclosed, not patched — see
`config.py` on why no absolute floor was added on top of it.

## Thresholds

Two, both in `config.py`, both `calibratable` — **neither has met outcome
data.** Every result carries `calibration_status:
"UNVALIDATED_PLACEHOLDERS"`, matching every other calibrated module in
ARGUS. A source scan (`tests/unit/news_signals/test_threshold_isolation.py`)
fails on any numeric literal in the logic modules that is not structural.

`config.py` also explains, at length, why this module deliberately does
**not** add an absolute article-count floor on top of the relative
multiple: this project already made that mistake once in the opposite
direction (a flat percentage threshold, corrected by the ATR-relative
audit in `docs/architecture/KNOWN_ISSUES.md` E5), and a flat floor here
would reintroduce it.

## The non-interference guarantee

**This signal has zero influence on watchlist membership or scoring, not
even as a condition.** `core/market_state/`, `core/scoring/`,
`core/candidate_detection/eligibility/` and `core/live_scanner/` must
never import this package or read the `news_volume_signals` table — a
security that is `BREAKOUT_READY` purely from price/volume structure
appears and is evaluated exactly as it would if this module did not
exist.

Enforced two ways:

- **Structurally**, on the parse tree —
  `tests/unit/news_signals/test_isolation_from_scoring_and_market_state.py`
  scans all four packages for any import of `core.news_signals`, any
  reach into `infra.db.schema.news_signals`, and any mention of
  `"news_volume_signals"` as a string constant.
- **Behaviourally** — `tests/integration/intelligence/test_news_signal_isolation.py`
  gives a security with **zero `canonical_news` rows ever** a real
  `BREAKOUT_READY` transition and a real score, snapshots the watchlist
  entry and detail response, attaches the strongest possible news-signal
  reading (`raised=True`) directly to the same security, and asserts
  every field except the one meant to change is byte-identical.

This module also does not depend on `core.market_state` even though
nothing requires that: `orchestrator.py` resolves who to assess via
`core.data_validation.universe.list_universe_members_as_of` — the whole
universe, not "who is on a watchlist" — rather than importing Module 10's
watchlist query, which keeps the batch driver decoupled from market state
as well as the scoring path.

## Storage and API

`services/intelligence` reads the `news_volume_signals` table directly
(`reads.py`'s `latest_news_signal` / `latest_news_signals`), the same
"reads and assembles, computes nothing" boundary the rest of that service
holds — it never imports this package's compute functions. The result
appears as `SecurityDetail.news_signal` on `GET
/intelligence/securities/{ticker}` and as `IntelligenceEntry.news_signal_raised`
on every watchlist entry, purely additive next to what was already there.

For full article text, the existing Terminal (Module 19) news endpoint is
the answer — this module never generates or serves article content.

## The daily run

`run_daily_news_signals(engine, universe_version_id=...)` — one scheduled
wake-up, deployed as `infra/deploy/news_signals.py`. Reuses Module 18's
own `scan_date_for`/`as_of_for` for "which trading day is this" rather
than adding a fourth independent answer to a question Modules 18, 26 and
27 already ask one function. Needs no FMP client and no secret beyond the
database connection — `canonical_news` is already filled by Module 26's
own deep refresh — so unlike the ingestion cron, this process does not
carry `docs/architecture/KNOWN_ISSUES.md` G3's exposure at all.

## What is deliberately not here

| Left out | Why |
|---|---|
| Any influence on scoring, state, or eligibility | The whole point — see "The non-interference guarantee" above. |
| Reading or summarising article text | v1 is a volume anomaly only. What the news is *about* is a later phase. |
| Planned/scheduled catalysts (earnings, court dates, patent/FDA calendars) | A separate, later phase pending a data-source decision — not a `canonical_news` question at all. |
| Frontend/UI | Backend and API only. The stored `raised` flag is what a future "flashing button" would read. |
| Telegram integration | Not in this phase. Module 27's alerts stay scoped to `BREAKOUT_READY` transitions. |
| A `core/risk_context/` home | Deliberately outside it: those flags feed `core/scoring/`'s `risk_level`, and this signal must never join them. |

## Cost

One aggregate query per batch run, for however many securities are in the
universe — never one query per security, the same discipline
`core/ingestion/refresh_log.py` and `services/intelligence/watchlists.py`
follow. Storage is a chunked upsert at `DEFAULT_BATCH_SIZE = 1_000`,
matching `data/normalization/persistence.py`'s convention.
