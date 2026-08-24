# Terminal Service (Module 19)

The first ARGUS module whose caller is not ARGUS. Serves what a person
sees when they look up a company: fundamentals, valuation, news, a chart,
and their own watchlists.

## Files

| File | What it owns |
|---|---|
| `schemas.py` | **The contract.** Every response shape, and the reasoning behind the three conventions that make them self-explanatory |
| `errors.py` | One error envelope, with stable machine-readable codes |
| `identity.py` | Who is asking — a deliberately temporary stub, shaped for Module 22 to replace |
| `config.py` | Page bounds and the two real product limits, kind-tagged |
| `company.py` | Fundamentals and valuation, through Module 07's PIT layer |
| `news.py` | Articles, through the `canonical_news` table migration 0010 adds |
| `bars.py` | PIT-correct, split-adjusted OHLCV, composed from Modules 07 and 08 |
| `datafeed.py` | The TradingView Advanced Charts datafeed protocol |
| `watchlists.py` | User watchlist CRUD |
| `freshness.py` | The one endpoint that touches scan output, via Module 18's `results.py` |
| `app.py` | Routing and translation. No logic |

## Three things to know before reading the code

**A missing value says why it is missing.** Eighteen modules have enforced
that `None` is never `0.0` and an absence is a different fact from a
measurement. In JSON, `null` means everything and nothing — so a block
that could not be produced is present, marked `available: false`, and
carries the `MissReason` that applied. A consumer can tell "not filed
yet" from "we do not carry this" without guessing.

**Fundamentals and news never reach scoring.** They are a context layer.
Module 13 keeps `fundamental_context` at 0% weight, and nothing here
imports from `core/scoring/` or `core/market_state/` — an AST scan over
this module's source asserts it, because a boundary that is only a
convention is one that eventually gets crossed by someone in a hurry.

**User Watchlists are not Intelligence Watchlists.** These are
user-created, named, manually edited. The three auto-generated lists
(DOWN TREND / CONSOLIDATION / BREAKOUT READY) derived from `market_state`
are Module 21's, and are not built here — not even read-only.

## Identity is a stub, and that is stated loudly on purpose

`current_user_id` trusts an `X-Argus-User` header. That is not
authentication; anyone who can reach the service can be anyone. It is
acceptable only because Module 22 has not defined its patterns and
guessing at them would produce a half-built auth system.

Two things stop it shipping by accident: it resolves against a real
`users` row rather than taking the header at face value, so ownership is
foreign-keyed from day one; and `stub_identity_enabled=False` closes
every user-scoped endpoint with a 501 naming Module 22. Turning it off is
a deployment change, not a code change.

Module 22 replaces one function body. No route signature, query or schema
changes.

## What the chart datafeed does and does not implement

Implemented: `/config`, `/time`, `/symbols`, `/search`, `/history` — the
complete set for a working historical chart.

Left out deliberately: `/marks` and `/timescale_marks` (ARGUS's overlays
are Module 21's), `/quotes` and streaming (ARGUS holds end-of-day bars;
there is nothing to stream), and the `/symbol_info` group request (an
optimisation for exchanges serving thousands of symbols at once).

No widget code. The Charting Library's JavaScript is frontend territory.
