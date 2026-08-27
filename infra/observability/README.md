# Observability (Module 23)

Making twenty-two modules' discipline visible to somebody who is not
reading source code. Nothing here computes a new fact — every signal is
something an earlier module already decided and recorded, read and
counted rather than re-derived.

## Files

| File | What it owns |
|---|---|
| `logging.py` | The structured-logging convention, the JSON formatter, the scrubber |
| `freshness.py` | Four-state data freshness over the PIT columns |
| `pipeline.py` | Module 18's scan-health feed, consumed; ingestion lag |
| `anomalies.py` | The two monitors Modules 10 and 17 asked for by name |
| `ordering.py` | The "latest row wins" audit, and a probe for it having bitten |
| `health.py` | ok / degraded / down, for infrastructure monitoring |
| `config.py` | Five numbers, all operational |

## The Alembic logging incident, and why it is written down here

This is the concrete example of why observability has to be verified
rather than assumed, so it is recorded rather than summarised.

**What happened.** `logging.config.fileConfig()` defaults to
`disable_existing_loggers=True`, which sets `disabled = True` on every
logger that already exists. Application loggers are created at import, so
any process that ran an Alembic migration lost the rest of its logging —
permanently, for the life of that process, and without a word. This was
present from Module 01.

**What it cost.** Almost nothing, by luck. Before Module 22 the project
had **zero** application loggers, so there was nothing to disable. Module
22 added exactly one — `argus.identity.seam`, which logs a WARNING every
time a request is served through the authentication bypass — and it was
silenced immediately. A deployment that migrated on start-up would have
lost precisely the line saying authentication was being bypassed.

**How it was found.** A `caplog` assertion failed while the code under
test was correct. Nothing else would have noticed: the logger existed,
the call ran, no exception was raised, and the only symptom was silence.

**What was still broken after the first fix.** Module 22 set
`disable_existing_loggers=False`, which stops the disabling. It does not
stop `fileConfig` from **replacing the root handlers and resetting the
root level**, because that is what alembic.ini's `[logger_root]` says to
do. An application with a JSON handler at DEBUG keeps every logger and
loses its formatter, its level, and anything shipping records onward.
That symptom is worse than the original: logging still appears to work.

**How it is closed now.** `configure_logging()` sets a flag;
`infra/db/migrations/env.py` skips `fileConfig` entirely when that flag is
set. A standalone `alembic upgrade head` never calls `configure_logging`,
so it keeps Alembic's console output exactly as before.

**The regression test.** `tests/integration/observability/
test_alembic_logging.py` runs a real `command.upgrade` in-process
alongside a configured application logger and asserts the logger still
emits, that the handler object is still installed, that the level
survived, and that the output is still JSON. It asserts the *property*,
not the fix — so it fails for this `fileConfig` call, a different one, a
library doing the same thing, or a future path that calls
`logging.disable`. Reverting either fix makes four of its six tests fail.

**The lesson, stated plainly.** Every part of this was working as
documented. `fileConfig`'s default is in the standard library docs, the
call was ordinary, and no test failed. Observability breaks silently by
construction — the thing that would have told you is the thing that
broke — which is why the property has a test rather than the fix having a
comment.

## The "latest row wins" audit

Module 17 asked for someone's hour on this. `ordering.py` is that hour,
written down as a registry so it does not have to be spent again: every
place ARGUS derives a current value from row ordering, and what makes it
safe or not.

The trap needs all three of: a timestamp defaulting to `now()` (which is
*transaction start* time, so two rows in one transaction tie), a random
tiebreak (`gen_random_uuid()`), and a decision taken from the winner.

**Four hazards found, none fixed here.** `setup_outcomes`, `signals`,
`historical_similarity_results`, `public_stat_snapshots`. Fixing any of
them means changing what Modules 11/15/17/20/21 decide, which is business
logic this module is told not to touch. So they are registered, and
`probe()` detects the ambiguity actually occurring in a live database —
turning a latent bug into an observable one. That is the trade an
observability module should make: not its place to change the writer,
exactly its place to notice.

Two of the four were found by this module's own completeness test failing
against a first draft of the registry, which is the argument for having
written the test rather than only the list.

## The two flagged anomaly monitors

**`no_state_predicate_matched`** — Module 10 split UNCLASSIFIED into "the
evidence was too thin" and "the evidence was fine and no state claimed
it", called the second a gap in the state machine, and wrote it into
`market_state_transitions.evidence`. Nothing counted it. `state_gaps`
does, from that stored evidence. No tolerance threshold: a threshold here
would be this module inventing an acceptable amount of a thing the module
that produces it calls a defect.

**The explanation revert rate** — Module 17 flagged that "a quietly high
revert rate would look like success". It would: `VerifiedRenderer` reverts
any rephrasing that does not verify, so a renderer producing nothing
usable yields perfectly correct output and every explanation passes.
`observed_renderer` counts it **without touching Module 16** — a recorder
slips between the verifier and the renderer it wraps, and the verdict per
claim is read off the final output. Module 16's `verify` is observed,
never reimplemented.

## Four freshness states, not three

`FRESH` / `DELAYED` / `STALE` / `UNAVAILABLE`. The fourth is not an
extreme of the third: there is no row, so there is no age — `None`, never
`0.0`. A never-ingested feed is a configuration problem and a stopped
feed is an outage, and a dashboard showing both as "stale, ∞ hours" makes
them indistinguishable.

`FeedFreshness.as_freshness()` produces exactly the `Freshness` object
Modules 19-21 already serve, so a response can embed this without
translation. One computation, two audiences — rather than a monitor whose
numbers drift from the ones the API is publishing.

## Health: three states, because two force a false choice

`down` means a dependency ARGUS cannot work without is unreachable, and
returns 503. `degraded` means ARGUS is serving and something wants a
person, and returns **200** — a stale feed is not fixed by having fewer
servers, and taking the instance out of rotation makes the outage worse.

## What this module does not do

- **No alerting or notification of any kind.** Structured status only.
- **No UI or dashboard.**
- **No business logic.** It observes; it changes nothing.
- **No live FMP calls.** Ingestion health reads provenance already on disk.
