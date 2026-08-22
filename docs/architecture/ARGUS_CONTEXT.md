# ARGUS — Project Context

This document is the durable context for what ARGUS is and why it's built the
way it's built. It's copied here (from the Module 01 build prompt) so every
future module has it on hand without depending on prior conversation history.

## What ARGUS is

ARGUS is a financial intelligence system for US equities. It continuously
scans a 10,000+ stock universe looking for one specific structural setup:

```
Prior Decline → Stabilization → Consolidation → Awakening → Confirmation → Expansion
```

In plain terms: a stock falls hard for a long time, then selling pressure
fades and it "bases" in a range — sometimes for weeks, sometimes for years,
there's no fixed duration — and eventually wakes up and breaks out into a
strong, sustained rally. ARGUS's entire value is finding candidates **during
the base, before the rally starts** — not after.

ARGUS does this by combining, for every candidate:

- Price / volume / volatility structure
- Relative strength vs. the market and sector
- Liquidity
- **Historical evidence**: has this exact kind of setup happened before, how
  often did it actually lead to a real expansion, and what happened when it
  didn't?

Every setup that fails is kept forever, never deleted, because failures are
how the system learns what a false positive looks like. Every score ARGUS
produces must be explainable — a user can always see *why* a setup scored the
way it did, broken into named components (pattern match quality, volatility
compression, volume structure, historical similarity, liquidity, risk, etc.),
never a single opaque number.

ARGUS is explicitly **not** a signal-spam bot, not a "buy now" system, and not
a guaranteed-profit machine. It surfaces evidence and probabilities; the human
using it makes the final decision. It is also, at this stage, **not** trying
to be profitable or sold to anyone — the plan is to run it for years,
accumulate a genuine, honest track record (including all the times it was
wrong), and only consider commercializing it once that record actually
exists.

## Architectural principles

Correctness and auditability matter far more than speed-to-market right now:

- **Point-in-time correctness is a hard rule.** A signal computed for a date
  in the past must never have access to data that wasn't actually available
  on that date. ARGUS's core value proposition is "this actually would have
  worked historically" — that claim is worthless if the historical
  calculation secretly used future information.
- **Every signal must be reproducible.** Given its recorded configuration
  IDs, re-running it must produce the identical result.
- **Nothing about a failed or successful setup is ever deleted from
  history.**
- **The system is built module-by-module.** Each module is scoped with a
  separate, reviewed prompt covering one piece at a time. A module should
  stay strictly inside its own boundary, even where it's clear how it
  connects to the bigger picture — if something outside a module's scope
  seems necessary to make that module "complete," it gets flagged instead of
  built early.

## Module map (as referenced so far)

This repository is built up module by module. Modules referenced explicitly
by name so far:

- **Module 01** — Architecture & Project Foundation (this skeleton).
- **Module 03** — Database schema & migrations (`infra/db/`).
- **Module 04** — FMP provider adapter / external HTTP calls
  (`data/provider_adapters/fmp/`).
- **Module 05** — Canonical data model (`data/canonical_model/`).
- **Modules 08–13** — Feature computation, pattern/candidate detection, and
  scoring logic (`core/feature_engine/`, `core/candidate_detection/`,
  `core/target_model_matching/`, `core/historical_similarity/`,
  `core/risk_context/`, `core/scoring/`).
- **Module 22** — Authentication (`services/identity/`).
- **Module 25** — Deployment / Docker configuration (Module 24 is Security Hardening).

Folders not yet tied to a specific module number will get one as the
corresponding module prompt is written.
