# Module 14 — Setup Qualification + Lifecycle

```
DETECTION → QUALIFICATION → ACTIVE → OUTCOME
```

Event-sourced throughout. `setups` has no status column — Module 03's
schema comment says adding one "would reintroduce the mutable field the
event sourcing exists to avoid" — so status is always derived from
`setup_events`, which is append-only. There is a test asserting the column
still does not exist, because this is the module with a motive to add it.

## Detection does not require a score

The central decision, and the one that determines whether ARGUS can ever
bootstrap itself.

The obvious rule is "a setup is created when a candidate is scored".
Applied today it creates nothing, ever:

```
no setups → no outcomes (Module 15) → no historical cases
         → Module 11 returns INSUFFICIENT for everything
         → Module 13 refuses to score anything
         → no setups
```

Module 13's realistic run produced twelve `INSUFFICIENT_EVIDENCE` results
and zero scores, correctly. A score as the price of admission would mean
ARGUS never opens a setup, never records an outcome, and Module 17 has
nothing to calibrate against — permanently.

So **detection is driven by Module 10's state assignment**, matching
Module 03's own comment on `setups` ("A setup is created when a candidate
reaches CONSOLIDATION"). Module 13's score gates **qualification**, which
is where ARGUS commits to tracking rather than merely noticing.

Today every setup sits at DETECTION. That is the honest picture: ARGUS has
noticed these bases and cannot yet say anything about them.

| Stage | Trigger |
|---|---|
| DETECTION | Module 10 state ∈ {CONSOLIDATION, ACCUMULATION} |
| QUALIFICATION | Module 13 `SCORED`, above `min_argus_score` **and** `min_confidence` |
| ACTIVE | Module 10 state ∈ {BREAKOUT_WATCH, BREAKOUT_READY} |
| OUTCOME | endpoint state, invalidation, or an expiry window closing |

The two bars are checked separately rather than folded together: Module 13
made a high score on thin evidence structurally possible by design, and
this is where ARGUS decides not to act on one.

## Two state machines, not one

A security's market state moves both ways and Module 10 records every
move. A setup's lifecycle status answers a different question — how far
ARGUS has committed — and runs forward only.

A retreat while ACTIVE is recorded as an event **at** ACTIVE, carrying
Module 10's own stored backward count, rather than as a demotion.
Demoting would erase nothing from the log but would make "has this setup
been active" unanswerable from the current status, and every consumer
reads the current status. `advance()` enforces the monotonicity so a
caller cannot regress by accident.

**UNCLASSIFIED never ends a setup.** Module 10 established that it sits
outside the cycle — a change in what is *knowable*, not a retreat. One
missing feature vector must not close setups that are alive, and closing
one is irreversible.

## Ordered by sequence, never by time

`current_status` orders by `sequence_number`. Two cases make time
ordering wrong, and both occur in practice:

- **Same-timestamp events.** A retreat and the invalidation it triggers
  are observed in one scan and share an `occurred_at`. Ordering by time
  leaves which came last to the database's row order.
- **Batch replay.** Module 17 replays historical dates, so `created_at`
  runs forward while `occurred_at` runs backwards. Neither column alone
  orders one setup's history; the sequence, assigned per setup in append
  order, does.

The unit test for this feeds the events **backwards** on purpose. Sorting
by `occurred_at` is a stable sort, so it happens to give the right answer
when rows arrive already in sequence order — the defect is only visible
when they do not. That was found by a break attempt, not by design.

## Gated candidates

Module 13 writes no `signals` row for a gated candidate, so `setup_events`
is the only place a broken thesis can be recorded. That handoff is this
module's core new responsibility.

| Module 13 verdict | Open setup | Result |
|---|---|---|
| `GATED_LOST_ELIGIBILITY` | yes | `invalidated_lost_eligibility` → OUTCOME |
| `GATED_INELIGIBLE` | yes | `invalidated_ineligible` → OUTCOME |
| either | no | **nothing written** |

The last row is deliberate. Creating a setup in order to record that it
should not exist would turn `setups` into a log of everything ARGUS ever
looked at — once per scan across a ten-thousand-name universe. Module 09's
`eligibility_check_results` already records durably that the candidate was
evaluated and which gate rejected it, in the table built for exactly that.

The invalidation payload carries Module 13's `verdict.detail` whole — the
eligibility trend, when the security last passed, which gates newly
failed — rather than a summary, because re-deriving any of it later would
mean re-running Module 09 against data that has moved on.

## Expiry

The counterweight to tracking unscoreable candidates. Without it every
base ARGUS ever noticed would hold its security's slot forever, and a new
base forming later could not open a setup.

- `max_active_duration_days` (180) — an ACTIVE setup that never resolved.
- `max_detection_duration_days` (365) — detected, never reached ACTIVE.

Detection's window is deliberately the longer of the two: a base can take
a year, and closing one early costs exactly the slow setup ARGUS exists to
find. There is a test asserting the ordering, because inverting them would
quietly reverse both intentions.

This module recognises that an endpoint is due. Module 15 decides what
actually happened; nothing here computes MFE, MAE or a return.

## Dual mode

`as_of` is a plain argument everywhere and nothing reads a wall clock.
`current_status(..., as_of=...)` bounds the history, so a replay of day
one sees DETECTION where today sees ACTIVE. The same call path serves a
live scan and Module 17's replay.

## Thresholds

Five, all in `config.py`, kind-tagged, four `calibratable` and one
`structural`. Every event payload carries
`calibration_status: "UNVALIDATED_PLACEHOLDERS"`, and the qualification
event records the whole threshold set it cleared, so a setup qualified
under today's bar stays attributable to it after the bar moves. A source
scan fails on any numeric literal in the logic modules.

## What is deliberately not here

| Left out | Why |
|---|---|
| Outcome computation (MFE/MAE/return) | Module 15. This module recognises the endpoint and hands off. |
| A fifth lifecycle state | The brief forbids it, and a test asserts `LIFECYCLE_ORDER` is exactly the enum. |
| Lifecycle demotion | Retreats are events inside a status. See above. |
| Watchlist/UI | Module 21. |
| Live FMP calls | Nothing here touches a provider. |

## Known gaps, flagged not fixed

- **Sequence allocation is a read-then-insert.** Two writers appending to
  the same setup concurrently can read the same maximum; the second insert
  then violates `uq_setup_event_sequence` and fails loudly, which is the
  correct outcome. Serialising would need a lock this module has no
  business taking. Tested.
- **One open setup per security is maintained, not enforced.** The schema
  permits two. `_index_by_security` resolves a collision by taking the
  most recently opened and leaves both visible in the log; a partial
  unique index (the shape migration 0005 used for `signals`) would make it
  structural.
