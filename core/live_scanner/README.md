# Live Scanner (Module 18)

Runs the ARGUS pipeline once per trading day, unattended, and records what
happened well enough that nobody has to be watching.

## This module computes nothing

Module 17's `scan_one_date` is the scan — Modules 06 through 15 over one
universe at one `as_of`. Module 18 calls it. There is no feature
computation, state classification, scoring or lifecycle logic here, and
if any appears it is a bug.

What is here is everything a batch replay does not need because a person
started it and is watching:

| File | What it owns |
|---|---|
| `config.py` | Every number, kind-tagged and versioned |
| `schedule.py` | Which date is due, derived from the market calendar |
| `readiness.py` | Whether that date's data has actually arrived |
| `failures.py` | Which of three kinds of wrong this is |
| `scanner.py` | One date, with retry, quarantine and a durable record |
| `catchup.py` | Missed dates, in order, exactly once |
| `daily.py` | The entry point a scheduler calls |
| `runs.py` | The `live_scan_runs` record |
| `results.py` | Reading a day's output back out of storage |

## The four decisions that shape it

**Nothing reads a wall clock except `run_daily`.** Every `as_of` is
derived from a session close, so a scan of Tuesday produces the same
answer whether it runs at 21:00 Tuesday, at 23:00 after a retry, or next
week during a catch-up. This is Module 07's dual-mode discipline applied
where the temptation to reach for `now()` is strongest.

**"Not ready" is not "failed".** Late market data is routine, expected and
self-resolving; a broken scanner is none of those. Recording both as
FAILED would teach whoever reads these rows to skim past FAILED, which is
the same as having no failure reporting at all.

**One bad security must not stop everything.** A malformed row will still
be malformed tomorrow, so failing the whole day for it stops ARGUS
permanently rather than for a day. On a non-transient failure the scanner
probes each security through Module 08's own batch path, quarantines what
throws, and re-runs the day without it — bounded, because isolating a
third of the universe and reporting success is how a broken feed gets
filed as a normal Tuesday.

**A failure ends in a row, never in a stack trace.** An unattended process
that raises crashes, and a crashed scanner is one nobody hears from.
`run_scan` returns a `ScanOutcome` for every outcome including the bad
ones. The one thing that does propagate is a `BaseException` — a `kill`,
an OOM — and even then the run row was committed before the work started,
so the date is identifiable afterwards as "in flight when something
stopped".

## Reading results

The scanner does not hand its signals to anyone. Module 17's
`ReplayResult` holds them because its evaluation reads them immediately;
nothing downstream of a live scan does. `results.py` reads from `signals`,
`setups` and `live_scan_runs` — the query surface Module 21's Intelligence
API would sit on.

## What this module does not do

No FMP calls: it orchestrates the pipeline over already-ingested data, and
scheduling ingestion is deliberately somebody else's job (see the module
report). No alerting — `live_scan_runs` carries structured status a later
observability module can read, and sending messages is not this module's
concern. No API and no UI.
