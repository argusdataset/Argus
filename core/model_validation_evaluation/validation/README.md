# Model Validation (Module 17)

Replays the whole ARGUS pipeline — Modules 06 through 15 — across a
historical period, PIT-correctly, in batch, producing signals with full
lineage exactly as the live scanner will.

## Files

| File | What it owns |
|---|---|
| `config.py` | Every number the replay uses, kind-tagged and versioned |
| `versions.py` | Correction 3: refusing a replay whose versions don't describe its code |
| `runs.py` | The `model_validation_runs` record |
| `replay.py` | The batch replay itself |
| `review.py` | The `PENDING_REVIEW → APPROVED / REJECTED` gate |

## Two things worth knowing before reading the code

**The engine is built for a scale it is never tested at.** A real run is
~10,000 securities over ~15-30 years. Every test here runs on five to ten
securities over three months. So the tests assert *structure* — that the
price panel loads once per scan date rather than once per security, that
the historical case set loads once per scan date rather than once per
candidate — because those are the properties that decide whether a real
run takes hours or weeks, and they are checkable at any scale.

**A replay refuses before it writes.** `versions.py` compares the checksum
of every configuration the replay is about to use against the published
row whose ID it cites, and compares the proposed lineage against whatever
the period already holds. Either mismatch stops the run. The escape hatch
for a deliberate re-score exists and is deliberately narrow: it must cite
a *newly published* target model version, so "reuse the old ID and call it
a re-score" is refused under both intents.

## What this module does not do

It does not run the real historical scan. That is a separate operational
event, blocked on the FMP subscription, `endpoints.py` verification
against a live key, and Module 06's full historical backfill. This module
is the engine; the run is later and deliberate.

It does not build a review UI. `review.py` is the backend surface a later
module's UI would sit on.
