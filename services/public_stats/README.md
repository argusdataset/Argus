# Public Stats (Module 20)

The only module in ARGUS that is public with no login, and the only place
where *"don't manufacture demand, create undeniable value"* becomes a fact
somebody can check rather than an intention.

## Files

| File | What it owns |
|---|---|
| `gate.py` | **The boundary.** The only file permitted to load outcomes |
| `releases.py` | The live-outcome review gate, and the decision behind it |
| `aggregates.py` | The four charts. Pure — frame in, payload out |
| `snapshots.py` | Materialization, and refusing to serve a withdrawn figure |
| `schemas.py` | The public contract |
| `config.py` | The public sample floor, and chart shapes |
| `errors.py` | Module 19's envelope, reused |
| `app.py` | Routing. No logic |

## The central constraint

Nothing derived from a `PENDING_REVIEW` or `REJECTED` result may reach any
endpoint here. That is enforced by shape rather than by discipline:

- `gate.py` is the only file that may load setups or outcomes.
- Loading is impossible without a `PublishScope`, and a scope is built
  only by `current_scope`, which takes nothing but a connection.
- Every load sits inside a loop over a scope member — a load outside one
  would be a query with no approval bounds, and an AST test asserts none
  exists.

## The decision this module had to make

Module 17 gates historical results. Module 18 deliberately does not gate
live *scans*. Nobody had said whether a live-tracked **outcome** may be
published.

**The answer here is yes, but only inside an approved window.** A window
is a date range plus the snapshot that labelled its outcomes, approved by
a named human, moving through the same three states Module 17's gate uses.

The reasoning is at the top of `releases.py`, including the case against —
which is real, and loses to two facts: Module 15's success criterion and
its review classifications are admitted placeholders, so an outcome is not
purely mechanical; and without a gate, nobody ever decides to publish any
particular number.

The honest cost, stated rather than hidden: **public statistics do not
update continuously.** They update when a window is approved.

## Why materializing is safe here

Every snapshot records the exact set of approved runs and windows it drew
from, plus a hash. On read the current gate state is recomputed and
compared:

- match → serve
- current scope still **covers** the stored one → something was added.
  Served, flagged stale, reason stated.
- otherwise → something was **removed**. Refused with
  `STATISTICS_WITHDRAWN`.

That last case takes the page down until a refresh runs. That is the
intended trade: an error saying "these figures are being recomputed"
honours ARGUS's claim to be checkable, and a stale percentage does not.

## Refresh

`refresh_public_stats(connection)` is the only writer. Cadence is an
operational decision, not a property of traffic; `expected_refresh_hours`
only decides when a response calls itself stale, never when it expires.

## What is deliberately absent

No authentication — this module is public by design, and there is no code
path that could answer differently for different callers. No Copy
statistics. No Intelligence watchlists. No public write path: approving a
window happens through `releases.py` with database access, the same way
Module 17's gate is moved.
