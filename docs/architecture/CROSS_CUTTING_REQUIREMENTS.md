# Cross-Cutting Requirements

These are requirements that span multiple modules and would be easy to lose
track of if left implicit in a single module's prompt. Recorded here so
they're carried forward regardless of which module is being built.

## A. Dual-mode operation (Modules 08-16)

Every module from 08 through 16 must be built to run in **two modes sharing
the same code path**:

1. **Live/incremental** — processing current market data on a schedule
   (Module 18's use).
2. **Full historical batch replay** — reprocessing the entire universe
   across the entire available history (Module 17's use, ~15 years,
   one-time per model version).

These must not be separate implementations. If Modules 08-16 are each built
with only the live case in mind, Module 17 will require retrofitting
batch-replay onto nine already-finished modules. This requirement must
appear in each of those modules' prompts when they're written.

## B. Module 09's analogue-count gate (forward dependency resolution)

Module 09's eligibility gate list includes "sufficient historical
analogues," but analogue counting is Module 11's job (Historical Similarity
Engine), built two modules later.

**Resolution:** Module 09 implements a lightweight, self-contained
analogue-count check — enough to enforce the gate, not the full similarity
engine. When Module 11 is built, Module 09's lightweight check is replaced
by a call into the real engine.

Recorded here so this isn't forgotten or solved differently later.
